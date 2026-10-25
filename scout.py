"""Sequential, resumable HonduCompras scanner; never discard an incomplete detail."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from email.utils import parsedate_to_datetime
import json
import logging
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from common import (RAW_FIELDS, DeadlineReached, check_deadline, cli, data_path,
                    database, detail_complete, digest, file_hash, finish, normalize, now, parse_date,
                    pause, plain, read_json, rows, stamp, started, useful_description, write_csv, write_json)
from scout_store import ScanStore

TABLE = "ctl00_cphCuerpo_gvResultados"
LOG = logging.getLogger("bastion")
VERSION = 2


def canonical_url(value):
    u = urlsplit(value)
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path,
                       urlencode(sorted(parse_qsl(u.query, keep_blank_values=True))), ""))


def process_id(url):
    return "hc:" + digest(canonical_url(url))


def form_data(soup):
    form = soup.find("form")
    if form is None:
        raise ValueError("No se encontró el formulario ASP.NET")
    result = {}
    for field in form.find_all(["input", "select", "textarea"]):
        name = field.get("name")
        if not name:
            continue
        if field.name == "select":
            option = field.find("option", selected=True) or field.find("option")
            result[name] = option.get("value", "") if option else ""
        elif field.name == "textarea":
            result[name] = field.get_text()
        else:
            kind = field.get("type", "text").lower()
            if kind in {"submit", "button", "image", "file", "reset"}:
                continue
            if kind in {"radio", "checkbox"} and not field.has_attr("checked"):
                continue
            result[name] = field.get("value", "")
    return result


@dataclass
class Page:
    links: list
    form: dict
    has_next: bool
    closing_dates: dict = field(default_factory=dict)
    pagination: list = field(default_factory=list)


def parse_results(html, search_url, page_number):
    soup = BeautifulSoup(html, "html.parser")
    try:
        table = soup.find(id=TABLE)
        if table is None:
            raise ValueError("La respuesta no contiene la tabla de resultados")
        links = list(dict.fromkeys(canonical_url(urljoin(search_url, a["href"]))
                                  for a in table.select('a[id$="hlGoTo"][href]')))
        closing_dates = {}
        headers = [plain(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        closing_index = next((i for i, title in enumerate(headers)
                              if "cierre" in title or ("recepcion" in title and "oferta" in title)), None)
        period_index = next((i for i, title in enumerate(headers) if "vigencia" in title), None)
        for tr in table.find_all("tr"):
            anchor = tr.select_one('a[id$="hlGoTo"][href]')
            if anchor is None:
                continue
            url = canonical_url(urljoin(search_url, anchor["href"]))
            cells = tr.find_all("td", recursive=False)
            value = cells[closing_index].get_text(" ", strip=True) if closing_index is not None and closing_index < len(cells) else ""
            if not value:
                date_field = tr.find(id=re.compile(r"fecha.*(cierre|recepcion)|(cierre|recepcion).*fecha", re.I))
                value = date_field.get_text(" ", strip=True) if date_field else ""
            if not value:
                # The live grid has ONE 'Perido de Vigencia' cell containing
                # both Fecha Inicio and Fecha Cierre. Never take its first date.
                closing = re.search(r"fecha\s*(?:de\s*)?cierre\s*:\s*(\d{1,2}/\d{1,2}/\d{4})",
                                    plain(cells[period_index].get_text(" ", strip=True)
                                          if period_index is not None and period_index < len(cells)
                                          else tr.get_text(" ", strip=True)))
                value = closing[1] if closing else ""
            closing_dates[url] = normalize(value)
        if not links and not any(word in plain(table.get_text(" ", strip=True)) for word in
                                 ("no se encontraron registros", "no se encontraron resultados",
                                  "no se encontraron procesos", "no se han encontrado registros",
                                  "no existen registros", "sin resultados", "no hay registros")):
            raise ValueError("Tabla sin enlaces ni mensaje explícito de resultados vacíos")
        pagination = []
        for a in table.find_all("a", href=True):
            href = a.get("href", "")
            onclick = a.get("onclick", "")
            source = href + " " + onclick
            postback = re.search(
                r"__doPostBack\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
                source, re.I)
            if postback:
                target, argument = postback.groups()
                match = re.fullmatch(r"Page\$([A-Za-z]+|\d+)", argument, re.I)
                if match:
                    token = match[1]
                    pagination.append({"page": int(token) if token.isdigit() else token.lower(),
                                       "target": target, "argument": argument, "href": href})
                continue
            match = re.search(r"Page\$([A-Za-z]+|\d+)", source, re.I)
            if match:
                token = match[1]
                pagination.append({"page": int(token) if token.isdigit() else token.lower(),
                                   "target": "", "argument": "Page$" + token, "href": href})
        has_next = any((isinstance(action["page"], int) and action["page"] > page_number)
                       or action["page"] in {"next", "last"} for action in pagination)
        return Page(links, form_data(soup), has_next, closing_dates, pagination)
    finally:
        soup.decompose()


def parse_detail(html, url, run_id):
    soup = BeautifulSoup(html, "html.parser")
    try:
        table = soup.find(id="ctl00_cphCuerpo_dvProceso")
        if table is None:
            raise ValueError("La respuesta no contiene el detalle del proceso")
        row = dict.fromkeys(RAW_FIELDS, "")
        row.update(run_id=run_id, proceso_id=process_id(url), url=canonical_url(url),
                   extraido_en=stamp())
        descriptions = []
        for tr in table.find_all("tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) != 2:
                continue
            label, value = [normalize(td.get_text(" ", strip=True)) for td in cells]
            label = plain(label)
            if "entidad" in label:
                row["entidad"] = value
            elif "expediente" in label:
                row["expediente"] = value
            elif "fecha" in label and "recepcion" in label and "oferta" in label:
                row["fecha_recepcion_original"] = value
            elif any(word in label for word in ("descripcion", "objeto", "denominacion")):
                if value:
                    descriptions.append(value)
        products = soup.find(id="ctl00_cphCuerpo_WebTab1_tmpl0_wdgProductos")
        items = []
        if products is not None:
            for tr in products.find_all("tr"):
                cells = tr.find_all("td", recursive=False)
                if len(cells) >= 3:
                    values = [normalize(td.get_text(" ", strip=True)) for td in cells]
                    if (not any(values) or plain(values[2]) == "especificaciones"
                            or not (useful_description(values[2]) or useful_description(values[1]))):
                        continue
                    # Preserve quantities, units, references and the complete description.
                    items.append(values)
                    descriptions.append(" | ".join(values))
        row["descripcion"] = "\n".join(descriptions)
        row["items_fuente"] = json.dumps(items, ensure_ascii=False)
        # Metadata stays in the journal/queue; the public CSV schema is unchanged.
        row["productos_estado"] = "ausente" if products is None else "presente" if items else "vacia"
        date_value = parse_date(row["fecha_recepcion_original"])
        row["fecha_recepcion"] = date_value.isoformat() if date_value else ""
        row["extraccion_estado"] = "completo" if (
            date_value and useful_description(row["descripcion"]) and row["entidad"]
            and row["expediente"] and products is not None) else "incompleto"
        return row
    finally:
        soup.decompose()


class HonduClient:
    def __init__(self, cfg, deadline=None):
        self.cfg = cfg["scout"]
        self.url = self.cfg["search_url"]
        self.deadline = deadline
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-HN,es;q=0.9,en;q=0.8", "Connection": "close"})
        self.last_finished = {"listing": 0.0, "detail": 0.0}
        self.interval = {
            "listing": self.cfg.get("listing_min_interval_seconds", 5.0),
            "detail": self.cfg.get("detail_min_interval_seconds", 2.0),
        }
        self.requests = 0
        self.retries = 0
        self.last_status_code = None
        self.last_retry_after_seconds = 0

    def close(self):
        self.session.close()

    def restart_search(self):
        headers = dict(self.session.headers)
        self.session.close()
        self.session = requests.Session()
        self.session.headers.update(headers)
        return self.search()

    def html(self, url, data=None, attempts=None, request_kind="detail"):
        error = None
        request_kind = "listing" if request_kind == "listing" else "detail"
        attempts = attempts or self.cfg.get("request_attempts", 3)
        self.last_status_code = None
        self.last_retry_after_seconds = 0
        for attempt in range(attempts):
            check_deadline(self.deadline)
            pause(max(0, self.last_finished[request_kind] + self.interval[request_kind]
                     - time.monotonic()), self.deadline)
            wait = self.cfg.get("backoff_seconds", 5) * (2 ** attempt)
            began = time.monotonic()
            remaining = ((self.deadline - now()).total_seconds() if self.deadline else 3600)
            timeout = (min(self.cfg.get("connect_timeout", 10), max(.1, remaining)),
                       min(self.cfg.get("read_timeout", 90), max(.1, remaining)))
            try:
                self.requests += 1
                with self.session.request("POST" if data is not None else "GET", url,
                                          data=data, timeout=timeout, stream=True) as response:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = max(wait, float(retry_after))
                            self.last_retry_after_seconds = max(0, float(retry_after))
                        except ValueError:
                            try:
                                self.last_retry_after_seconds = max(
                                    0, (parsedate_to_datetime(retry_after) - now()).total_seconds())
                                wait = max(wait, self.last_retry_after_seconds)
                            except (ValueError, TypeError):
                                pass
                    response.raise_for_status()
                    limit = int(self.cfg.get("max_response_mb", 16) * 1024 * 1024)
                    size = 0
                    chunks = []
                    for chunk in response.iter_content(65536):
                        check_deadline(self.deadline)
                        size += len(chunk)
                        if size > limit:
                            raise ValueError("Respuesta supera max_response_mb; revisar, no se descarta silenciosamente")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    # ASP.NET often declares the charset in HTML, not in HTTP.
                    declared = re.search(br'charset\s*=\s*["\x27]?([a-zA-Z0-9_-]+)', body[:4096])
                    if declared:
                        encoding = declared[1].decode("ascii")
                    result = body.decode(encoding, errors="replace")
                elapsed = time.monotonic() - began
                self.interval[request_kind] = max(
                    self.cfg.get(request_kind + "_min_interval_seconds", 5.0 if request_kind == "listing" else 2.0),
                    min(self.cfg.get(request_kind + "_max_interval_seconds", 8.0 if request_kind == "listing" else 4.0),
                        elapsed * .1))
                return result
            except requests.RequestException as exc:
                error = exc
                self.interval[request_kind] = self.cfg.get(
                    request_kind + "_max_interval_seconds", 8.0 if request_kind == "listing" else 4.0)
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                self.last_status_code = status_code
                LOG.warning("Solicitud fallida (%s, HTTP=%s), intento %s",
                            type(exc).__name__, status_code or "sin respuesta", attempt + 1)
                if status_code and 400 <= status_code < 500 and status_code not in {408, 429}:
                    raise RuntimeError(f"HonduCompras HTTP {status_code}; detalle pendiente de revisión") from exc
                if attempt + 1 < attempts:
                    self.retries += 1
                    pause(wait, self.deadline)
            finally:
                self.last_finished[request_kind] = time.monotonic()
        status_code = self.last_status_code
        reason = f"HTTP {status_code}" if status_code else type(error).__name__
        raise RuntimeError(f"HonduCompras no respondió: {reason}") from error

    def search(self):
        soup = BeautifulSoup(self.html(self.url, request_kind="listing"), "html.parser")
        try:
            data = form_data(soup)
        finally:
            soup.decompose()
        data["ctl00$cphCuerpo$wpParametros$ddlModalidad"] = self.cfg["modalidad"]
        data["ctl00$cphCuerpo$wpParametros$ddlTipoAdquisicion"] = self.cfg["tipo"]
        data["ctl00$cphCuerpo$wpParametros$btnBuscar"] = "Buscar"
        return parse_results(self.html(self.url, data, attempts=1, request_kind="listing"), self.url, 1)

    def advance(self, page, number):
        data = dict(page.form)
        data.pop("ctl00$cphCuerpo$wpParametros$btnBuscar", None)
        action = next((item for item in page.pagination if item["page"] == number), None)
        if action is None:
            action = next((item for item in page.pagination
                           if item["page"] == "next"), None)
        if action is None:
            # Some WebForms pagers expose only a target or a nonnumeric Last link.
            action = next((item for item in page.pagination if item.get("target")), None)
        if action is None:
            raise RuntimeError(f"El formulario no contiene una acción de paginación para la página {number}")
        target = action.get("target")
        argument = action.get("argument")
        if target and action["page"] not in {number, "next"}:
            argument = f"Page${number}"
        if not target:
            raise RuntimeError(f"El enlace de paginación Page${number} no contiene un postback ASP.NET utilizable")
        data.update(__EVENTTARGET=target, __EVENTARGUMENT=argument)
        # A failed postback may have consumed its form state: rebuild the search
        # instead of repeatedly posting the same stale ViewState.
        return parse_results(self.html(self.url, data, attempts=1, request_kind="listing"), self.url, number)

    def detail(self, url, run_id):
        return parse_detail(self.html(url), url, run_id)


def export_raw(cfg, db, run_id, today):
    def values():
        for record in db.execute("SELECT data FROM scan WHERE run_id=? AND data IS NOT NULL ORDER BY rowid", (run_id,)):
            row = json.loads(record["data"])
            d = parse_date(row["fecha_recepcion"])
            if d is None or d.date() >= today:
                yield row
    write_csv(data_path(cfg, "raw.csv"), RAW_FIELDS, values())


def recover_raw(cfg, run_id):
    """Recover a previous cycle before its public raw file can be replaced."""
    with database(cfg) as db:
        export_raw(cfg, db, run_id, now().date())
    state = read_json(data_path(cfg, "scout.json"), {})
    state.update(stage="scout", run_id=run_id, status="parcial",
                 reason="recuperado_desde_journal", raw_sha256=file_hash(data_path(cfg, "raw.csv")))
    write_json(data_path(cfg, "scout.json"), state)


def run(cfg, run_id=None, deadline=None, client=None):
    from scout_engine import run as scan
    return scan(cfg, run_id, deadline, client)


if __name__ == "__main__":
    raise SystemExit(cli(run))
