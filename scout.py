"""Sequential, resumable HonduCompras scanner; never discard an incomplete detail."""
from __future__ import annotations

from dataclasses import dataclass
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
                    database, digest, file_hash, finish, normalize, now, parse_date,
                    pause, plain, read_json, rows, stamp, started, write_csv, write_json)

TABLE = "ctl00_cphCuerpo_gvResultados"
TARGET = "ctl00$cphCuerpo$gvResultados"
LOG = logging.getLogger("bastion")


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


def parse_results(html, search_url, page_number):
    soup = BeautifulSoup(html, "html.parser")
    try:
        table = soup.find(id=TABLE)
        if table is None:
            raise ValueError("La respuesta no contiene la tabla de resultados")
        links = list(dict.fromkeys(canonical_url(urljoin(search_url, a["href"]))
                                  for a in table.select('a[id$="hlGoTo"][href]')))
        if not links and not any(word in plain(table.get_text(" ", strip=True)) for word in
                                 ("no se encontraron registros", "no se encontraron resultados",
                                  "no se encontraron procesos", "no se han encontrado registros",
                                  "no existen registros", "sin resultados", "no hay registros")):
            raise ValueError("Tabla sin enlaces ni mensaje explícito de resultados vacíos")
        arguments = []
        for a in table.find_all("a", href=True):
            arguments.extend(re.findall(r"Page\$([A-Za-z]+|\d+)", a["href"]))
        has_next = any((a.isdigit() and int(a) > page_number) or a.lower() in {"next", "last"}
                       for a in arguments)
        return Page(links, form_data(soup), has_next)
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
                    if not any(values) or plain(values[2]) == "especificaciones":
                        continue
                    # Preserve quantities, units, references and the complete description.
                    items.append(values)
                    descriptions.append(" | ".join(values))
        row["descripcion"] = "\n".join(descriptions)
        row["items_fuente"] = json.dumps(items, ensure_ascii=False)
        date_value = parse_date(row["fecha_recepcion_original"])
        row["fecha_recepcion"] = date_value.isoformat() if date_value else ""
        row["extraccion_estado"] = "completo" if (
            date_value and row["descripcion"] and row["entidad"] and row["expediente"]) else "incompleto"
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
        self.last_finished = 0.0
        self.interval = self.cfg.get("min_interval_seconds", 1.0)
        self.requests = 0
        self.retries = 0

    def close(self):
        self.session.close()

    def html(self, url, data=None):
        error = None
        for attempt in range(self.cfg.get("request_attempts", 3)):
            check_deadline(self.deadline)
            pause(max(0, self.last_finished + self.interval - time.monotonic()), self.deadline)
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
                        except ValueError:
                            try:
                                wait = max(wait, (parsedate_to_datetime(retry_after) - now()).total_seconds())
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
                self.interval = max(self.cfg.get("min_interval_seconds", 1.0),
                                    min(self.cfg.get("max_interval_seconds", 4.0), elapsed * .1))
                return result
            except requests.RequestException as exc:
                error = exc
                self.interval = self.cfg.get("max_interval_seconds", 4.0)
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                LOG.warning("Solicitud fallida (%s, HTTP=%s), intento %s",
                            type(exc).__name__, status_code or "sin respuesta", attempt + 1)
                if status_code and 400 <= status_code < 500 and status_code not in {408, 429}:
                    raise RuntimeError(f"HonduCompras HTTP {status_code}; detalle pendiente de revisión") from exc
                if attempt + 1 < self.cfg.get("request_attempts", 3):
                    self.retries += 1
                    pause(wait, self.deadline)
            finally:
                self.last_finished = time.monotonic()
        raise RuntimeError(f"HonduCompras no respondió: {type(error).__name__}") from error

    def search(self):
        soup = BeautifulSoup(self.html(self.url), "html.parser")
        try:
            data = form_data(soup)
        finally:
            soup.decompose()
        data["ctl00$cphCuerpo$wpParametros$ddlModalidad"] = self.cfg["modalidad"]
        data["ctl00$cphCuerpo$wpParametros$ddlTipoAdquisicion"] = self.cfg["tipo"]
        data["ctl00$cphCuerpo$wpParametros$btnBuscar"] = "Buscar"
        return parse_results(self.html(self.url, data), self.url, 1)

    def advance(self, page, number):
        data = dict(page.form)
        data.pop("ctl00$cphCuerpo$wpParametros$btnBuscar", None)
        data.update(__EVENTTARGET=TARGET, __EVENTARGUMENT=f"Page${number}")
        return parse_results(self.html(self.url, data), self.url, number)

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
    run_id = run_id or now().date().isoformat()
    state = started("scout", run_id)
    state.update(pages=0, extracted=0, expired=0, consecutive_expired=0, pending=[], detail_errors={},
                 scope={"modalidad": cfg["scout"]["modalidad"], "tipo": cfg["scout"]["tipo"]})
    write_json(data_path(cfg, "scout.json"), state)
    own_client = client is None
    client = client or HonduClient(cfg, deadline)
    today = now().date()
    cutoff = today - timedelta(days=1)
    state["cutoff_date"] = cutoff.isoformat()
    reason, error = "", None
    failures = 0
    streak = 0
    visited = set()
    seen_urls = set()
    attempted_urls = set()
    with database(cfg) as db:
        # Bounded disk history for successful scans; unresolved details survive daily cycles.
        db.execute("DELETE FROM scan WHERE run_id < ? AND outcome='ok'",
                   ((today - timedelta(days=7)).isoformat(),))
        db.commit()

        def save(url, row=None, failure=None):
            previous = db.execute("SELECT data FROM scan WHERE run_id=? AND url=?", (run_id, url)).fetchone()
            encoded = json.dumps(row, ensure_ascii=False) if row else (previous["data"] if previous else None)
            date_value = parse_date(row["fecha_recepcion"]) if row else None
            known_expired = date_value is not None and date_value.date() < today
            outcome = "ok" if row and (row["extraccion_estado"] == "completo" or known_expired) else "incompleto" if row else "error"
            if outcome == "ok":
                state["detail_errors"].pop(url, None)
            elif failure:
                state["detail_errors"][url] = failure
            elif row:
                missing = [key for key in ("fecha_recepcion", "entidad", "expediente", "descripcion") if not row.get(key)]
                state["detail_errors"][url] = "Campos pendientes: " + ", ".join(missing)
            db.execute("INSERT OR REPLACE INTO scan VALUES (?,?,?,?)", (run_id, url, outcome, encoded))
            if outcome == "ok":
                db.execute("DELETE FROM scan WHERE url=? AND run_id<>? AND outcome<>'ok'", (url, run_id))
            db.commit()

        def obtain(url):
            nonlocal failures
            check_deadline(deadline)
            cached = db.execute("SELECT outcome,data FROM scan WHERE run_id=? AND url=?", (run_id, url)).fetchone()
            if cached and cached["outcome"] == "ok":
                return json.loads(cached["data"])
            if url in attempted_urls:
                return json.loads(cached["data"]) if cached and cached["data"] else None
            attempted_urls.add(url)
            try:
                row = client.detail(url, run_id)
                save(url, row)
                failures = 0
                return row
            except DeadlineReached:
                save(url, failure="horario")
                raise
            except Exception as exc:
                LOG.warning("Detalle pendiente %s: %s", url, type(exc).__name__)
                save(url, failure=str(exc))
                failures += 1
                if failures >= cfg["scout"].get("consecutive_failures_limit", 3):
                    raise RuntimeError("Circuito abierto por fallos consecutivos; se reintentará sin aumentar carga") from exc
                return None

        try:
            # Preserve the backlog, but fetch the fresh listing first: stale broken links
            # must never prevent today's valuable processes from being extracted.
            pending = [r["url"] for r in db.execute("SELECT DISTINCT url FROM scan WHERE outcome<>'ok'")]
            page = client.search()
            number = 1
            while True:
                check_deadline(deadline)
                signature = digest(page.links)
                if page.links and signature in visited:
                    raise RuntimeError("Paginación repetida: el sitio no avanzó de página")
                visited.add(signature)
                state["pages"] = number
                for url in page.links:
                    if url in seen_urls:
                        streak = 0
                        continue
                    seen_urls.add(url)
                    row = obtain(url)
                    d = parse_date(row["fecha_recepcion"]) if row else None
                    if row and d and d.date() <= cutoff:
                        streak += 1
                    else:
                        streak = 0
                    state["consecutive_expired"] = streak
                    if streak >= cfg["scout"].get("expired_streak", 10):
                        reason = "10_vencidos_consecutivos"
                        break
                export_raw(cfg, db, run_id, today)
                write_json(data_path(cfg, "scout.json"), state)
                LOG.info("Scout página %s, secuencia vencidos %s", number, streak)
                if reason:
                    break
                if not page.has_next:
                    reason = "fin_resultados_verificado"
                    break
                number += 1
                page = client.advance(page, number)
            # A known process can be extended without moving to the top of the listing.
            # Revisit master URLs hidden behind the early stop; never skip such changes.
            state["known_rechecked"] = 0
            master = data_path(cfg, "maestro.csv")
            if master.exists():
                for known in rows(master):
                    url = known.get("url")
                    if url and not db.execute("SELECT 1 FROM scan WHERE run_id=? AND url=?", (run_id, url)).fetchone():
                        obtain(url)
                        state["known_rechecked"] += 1
            # Retry unresolved historical details even if they are beyond today's cutoff.
            for url in pending:
                obtain(url)
        except DeadlineReached:
            reason = "limite_horario"
        except Exception as exc:
            reason, error = "error_escaneo", str(exc)
            LOG.warning("Scout interrumpido: %s", error)
        finally:
            export_raw(cfg, db, run_id, today)
            if own_client:
                client.close()
        for record in db.execute("SELECT url,outcome,data FROM scan WHERE run_id=?", (run_id,)):
            if record["outcome"] != "ok":
                state["pending"].append({"url": record["url"],
                                         "reason": state["detail_errors"].get(record["url"], record["outcome"])})
            if record["data"]:
                row = json.loads(record["data"])
                d = parse_date(row["fecha_recepcion"])
                if d and d.date() < today:
                    state["expired"] += 1
                else:
                    state["extracted"] += 1
        good = reason in {"10_vencidos_consecutivos", "fin_resultados_verificado"} and not state["pending"]
        state.update(http_requests=getattr(client, "requests", 0), http_retries=getattr(client, "retries", 0))
        return finish(cfg, state, "exitoso" if good else "parcial" if state["extracted"] else "error",
                      reason, error=error, raw_sha256=file_hash(data_path(cfg, "raw.csv")))


if __name__ == "__main__":
    raise SystemExit(cli(run))
