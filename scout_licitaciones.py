"""Escanea licitaciones de HonduCompras y exporta las que reciben ofertas."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from common import (ROOT, BusyError, DeadlineReached, check_deadline, normalize, now,
                    digest, parse_date, pause, pipeline_lock, plain, read_json, stamp,
                    useful_description, write_csv, write_json)


SEARCH_URL = "http://sicc.honducompras.gob.hn/HC/Procesos/BusquedaHistorico.aspx"
MODALITY = "ctl00$cphCuerpo$wpParametros$ddlModalidad"
ACQUISITION = "ctl00$cphCuerpo$wpParametros$ddlTipoAdquisicion"
STAGE = "ctl00$cphCuerpo$wpParametros$ddlEtapas"
SEARCH_BUTTON = "ctl00$cphCuerpo$wpParametros$btnBuscar"
TENDER_GROUP = "licitacion o concurso"
OPEN_STAGE = "recepcion de ofertas"
FIELDS = ["expediente", "entidad", "unidad_compra", "modalidad", "tipo_adquisicion", "etapa", "objeto",
          "tipo_fuente", "fuente", "fecha_recepcion", "fecha_recepcion_original", "lugar_recepcion",
          "coincidencias_interes", "posibles_interes", "evidencia_interes", "url", "extraido_en"]
DETAIL_FIELDS = {
    "expediente": "expediente", "entidad": "entidad", "modalidad": "modalidad",
    "unidad de compra": "unidad_compra", "unidad compra": "unidad_compra",
    "tipo fuente": "tipo_fuente", "tipo de fuente": "tipo_fuente", "fuente": "fuente",
    "fuente de financiamiento": "fuente", "fuente financiamiento": "fuente",
    "tipo adquisicion": "tipo_adquisicion", "tipo de adquisicion": "tipo_adquisicion",
    "etapa": "etapa", "estado": "etapa", "objeto": "objeto", "descripcion": "objeto",
    "lugar recepcion ofertas": "lugar_recepcion", "lugar de recepcion de ofertas": "lugar_recepcion",
}
INTEREST_FIELDS = ("fuente", "tipo_fuente", "unidad_compra", "entidad", "objeto",
                   "lugar_recepcion", "expediente")
INTEREST_PATTERNS = {
    "BID": re.compile(r"(?<![a-z0-9])(?:bid|idb)(?![a-z])|banco interamericano de desarrollo|"
                      r"inter[ -]american development bank"),
    "UCP": re.compile(r"(?<![a-z0-9])ucp(?![a-z0-9])|"
                      r"unidad coordinadora (?:de |del )?(?:proyectos?|programas?)\b"),
    "JICA": re.compile(r"(?<![a-z0-9])jica(?![a-z0-9])|japan international cooperation agency|"
                       r"agencia japonesa de cooperacion internacional|"
                       r"agencia de cooperacion internacional (?:de |del )?japon"),
}
POSTBACK = re.compile(r"__doPostBack\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"](Page\$\w+)['\"]", re.I)


def form_data(soup):
    form = soup.find("form")
    if form is None:
        raise ValueError("Falta el formulario de HonduCompras")
    data = {}
    for field in form.select("input[name], select[name], textarea[name]"):
        if field.has_attr("disabled"):
            continue
        kind = field.get("type", "text").lower()
        if kind in {"button", "submit", "image", "file", "reset"}:
            continue
        if kind in {"radio", "checkbox"} and not field.has_attr("checked"):
            continue
        value = field
        if field.name == "select":
            value = field.find("option", selected=True) or field.find("option")
            if value is None:
                continue
        default = value.get_text() if field.name != "input" else ""
        data[field["name"]] = value.get("value", default)
    return data


def tender_kind(label):
    match = re.match(r"licitacion (publica|privada)\b(?: (nacional|internacional)\b)?", plain(label))
    return match.groups() if match else None


def modalities(soup):
    selector = soup.find("select", attrs={"name": MODALITY})
    options = {} if selector is None else {
        option["value"]: label for option in selector.select("option[value]:not([disabled])")
        if option["value"] and (
            plain(label := option.get_text(" ", strip=True)) == TENDER_GROUP or tender_kind(label))
    }
    if not options:
        raise ValueError("El formulario no ofrece modalidades de licitación pública o privada")
    return options


def option_value(soup, name, labels):
    selector = soup.find("select", attrs={"name": name})
    if selector is not None:
        for option in selector.select("option[value]:not([disabled])"):
            if plain(option.get_text(" ", strip=True)).strip("() ") in labels:
                return option["value"]
    raise ValueError(f"No se encontró la opción del filtro: {', '.join(labels)}")


def result_page(soup, number):
    table = soup.find(id="ctl00_cphCuerpo_gvResultados")
    if table is None:
        raise ValueError("Falta la tabla de resultados")
    headers = [plain(th.get_text(" ", strip=True)) for th in table.find_all("th")]
    period = next((index for index, label in enumerate(headers) if "vigencia" in label), None)
    modality = next((index for index, label in enumerate(headers) if label == "modalidad"), None)
    candidates = {}
    for link in table.select('a[id$="hlGoTo"][href]'):
        url = urlsplit(urljoin(SEARCH_URL, link["href"]))
        if (url.scheme not in {"http", "https"} or url.netloc != urlsplit(SEARCH_URL).netloc
                or url.path.lower().rsplit("/", 1)[-1] not in {"procesohistorico.aspx", "proceso.aspx"}
                or not url.query):
            raise ValueError("Enlace de detalle inesperado en el listado")
        detail_url = urlunsplit((url.scheme, url.netloc, url.path,
                                 urlencode(sorted(parse_qsl(url.query, keep_blank_values=True))), ""))
        cells = link.find_parent("tr").find_all("td", recursive=False)
        if modality is not None and modality < len(cells) and plain(
                cells[modality].get_text(" ", strip=True)).startswith("concurso "):
            candidates[detail_url] = (None, False)
            continue
        line = plain(cells[period].get_text(" ", strip=True)) if period is not None and period < len(cells) else ""
        match = re.search(r"fecha\s+(?:de\s+)?cierre\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", line)
        candidates[detail_url] = (parse_date(match[1]) if match else None, True)
    if not candidates and not re.search(
            r"no (?:se encontraron|se han encontrado|existen|hay) (?:registros|resultados|procesos)|sin resultados",
            plain(table.get_text(" ", strip=True))):
        raise ValueError("La tabla no confirma que la búsqueda esté vacía")

    actions = {}
    for link in table.find_all("a"):
        match = POSTBACK.search(link.get("href", "") + " " + link.get("onclick", ""))
        if match:
            target, argument = match.groups()
            actions[argument.split("$")[1].lower()] = target, argument
    next_page = actions.get(str(number + 1)) or actions.get("next")
    if next_page is None:
        # A numeric postback on a later page can also be represented by «última».
        later = next((action for key, action in actions.items()
                      if key == "last" or (key.isdigit() and int(key) > number)), None)
        if later:
            next_page = later[0], f"Page${number + 1}"
    return candidates, next_page


def detect_interest(row):
    matches = []
    evidence = []
    for label, pattern in INTEREST_PATTERNS.items():
        fields = [field for field in INTEREST_FIELDS if pattern.search(plain(row.get(field, "")))]
        if fields:
            matches.append(label)
            evidence.extend(f"{label} ({field}) {row[field]}" for field in fields)
    possible_jica = "JICA" not in matches and re.search(r"\b(?:japon|japan)\b", plain(row.get("fuente", "")))
    row["posibles_interes"] = "JICA" if possible_jica else ""
    if possible_jica:
        evidence.append(f"Posible JICA, por verificar (fuente) {row['fuente']}")
    row["coincidencias_interes"] = " | ".join(matches)
    row["evidencia_interes"] = "\n".join(evidence)


def parse_detail(soup, url, expected):
    table = soup.find(id="ctl00_cphCuerpo_dvProceso")
    if table is None:
        raise ValueError("Falta la ficha del proceso")
    row = dict.fromkeys(FIELDS, "")
    row.update(url=url, extraido_en=stamp())
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) != 2:
            continue
        label = plain(cells[0].get_text(" ", strip=True)).rstrip(": ")
        value = normalize(cells[1].get_text(" ", strip=True))
        if label.startswith("fecha") and "recepcion" in label and "oferta" in label:
            row["fecha_recepcion_original"] = value
        elif value and label in DETAIL_FIELDS:
            row[DETAIL_FIELDS[label]] = value
    kind = tender_kind(row["modalidad"])
    expected_kind = tender_kind(expected)
    if plain(expected) == TENDER_GROUP and plain(row["modalidad"]).startswith("concurso "):
        return None
    if kind is None or (expected_kind and (kind[0] != expected_kind[0]
                                         or (expected_kind[1] and kind[1] != expected_kind[1]))):
        raise ValueError("La modalidad de la ficha no coincide con la búsqueda")
    if not row["etapa"]:
        raise ValueError("Ficha sin etapa publicada")
    if plain(row["etapa"]) != OPEN_STAGE:
        return None
    date = parse_date(row["fecha_recepcion_original"])
    if date is None:
        raise ValueError("Fecha de recepción ausente o ilegible")
    if not row["expediente"] or not row["entidad"] or not useful_description(row["objeto"]):
        raise ValueError("Ficha sin expediente, entidad u objeto")
    original = row["fecha_recepcion_original"]
    row["fecha_recepcion"] = date.isoformat() if re.search(r"\d:\d", original) else date.date().isoformat()
    detect_interest(row)
    return row


def interest_priority(row):
    tags = row["coincidencias_interes"].split(" | ")
    return next((index for index, tag in enumerate(("JICA", "BID", "UCP")) if tag in tags),
                3 if row["posibles_interes"] else 4)


class Client:
    def __init__(self, session, deadline):
        self.session = session
        self.deadline = deadline
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "es-HN,es;q=0.9"})
        self.interval = 0

    def html(self, url, data=None):
        pause(self.interval, self.deadline)
        self.interval = 5 if url == SEARCH_URL else 2
        remaining = max(.1, (self.deadline - now()).total_seconds())
        timeout = (min(10, remaining), min(45, remaining))
        with self.session.request("GET" if data is None else "POST", url, data=data,
                                  timeout=timeout, stream=True) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_content(65536):
                check_deadline(self.deadline)
                body.extend(chunk)
                if len(body) > 16 * 1024 * 1024:
                    raise ValueError("Respuesta mayor a 16 MiB")
        return BeautifulSoup(bytes(body), "html.parser")

    def pages(self, code):
        soup = self.html(SEARCH_URL)
        data = form_data(soup)
        data.update({MODALITY: code, ACQUISITION: option_value(soup, ACQUISITION, ("todas", "todos")),
                     STAGE: option_value(soup, STAGE, (OPEN_STAGE,)), SEARCH_BUTTON: "Buscar"})
        number = 1
        signatures = set()
        while True:
            soup = self.html(SEARCH_URL, data)
            candidates, action = result_page(soup, number)
            signature = frozenset(candidates)
            if signature in signatures:
                raise ValueError(f"HonduCompras repitió el listado al solicitar la página {number}")
            signatures.add(signature)
            yield candidates
            if action is None:
                return
            data = form_data(soup)
            data["__EVENTTARGET"], data["__EVENTARGUMENT"] = action
            number += 1


def publish_shortlist(db, output, active, observed, status):
    """Preserve unseen opportunities during partial scans and stable alert versions."""
    timestamp = stamp()
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS lista_corta (
            id TEXT PRIMARY KEY, data TEXT NOT NULL, version TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, active INTEGER NOT NULL)""")
        if status["status"] == "exitoso":
            db.execute("UPDATE lista_corta SET active=0")
        else:
            db.executemany("UPDATE lista_corta SET active=0 WHERE id=?", ((digest(url),) for url in observed))
        for row in active:
            identifier = digest(row["url"])
            content = {key: row[key] for key in FIELDS if key != "extraido_en"}
            version = digest(content)
            db.execute("""INSERT INTO lista_corta VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET data=excluded.data,
                updated_at=CASE WHEN version<>excluded.version THEN excluded.updated_at ELSE updated_at END,
                version=excluded.version, active=1""",
                       (identifier, json.dumps(row, ensure_ascii=False), version, timestamp, timestamp))
    items = []
    for identifier, data, version, first_seen, changed in db.execute(
            "SELECT id,data,version,first_seen_at,updated_at FROM lista_corta WHERE active=1"):
        row = json.loads(data)
        deadline = parse_date(row["fecha_recepcion"])
        if deadline and deadline > now():
            items.append(dict(row, id=identifier, version=version, first_seen_at=first_seen,
                              updated_at=changed, fecha_recepcion=deadline.isoformat()))
    items.sort(key=lambda row: (interest_priority(row), row["fecha_recepcion"], row["id"]))
    feed = {"schema_version": 2, "generated_at": timestamp, "scan_status": status["status"],
            "items": items}
    if len(items) > 5000 or len((json.dumps(feed, ensure_ascii=False, indent=2) + "\n").encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("La lista corta supera 5000 fichas u 8 MiB")
    write_json(output / "lista_corta.json", feed, mode=0o640)


def scan(output, client):
    status_path = output / "licitaciones.json"
    previous = read_json(status_path, {})
    run_id = now().date().isoformat()
    status = {"status": "ejecutando", "run_id": run_id, "pid": os.getpid(),
              "started_at": stamp(), "pages": 0, "pendientes": []}
    pending = status["pendientes"]
    seen = set()
    with closing(sqlite3.connect(output / "licitaciones.sqlite3")) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS fichas (url TEXT PRIMARY KEY, data TEXT NOT NULL)")
            if previous.get("run_id") != run_id or previous.get("status") == "exitoso":
                db.execute("DELETE FROM fichas")
        write_json(status_path, status)
        try:
            for code, label in modalities(client.html(SEARCH_URL)).items():
                try:
                    for candidates in client.pages(code):
                        status["pages"] += 1
                        for url, (closing_date, eligible) in candidates.items():
                            check_deadline(client.deadline)
                            if not eligible or url in seen or (closing_date and closing_date.date() < now().date()):
                                continue
                            seen.add(url)
                            cached = db.execute("SELECT data FROM fichas WHERE url=?", (url,)).fetchone()
                            if cached:
                                cached_row = json.loads(cached[0])
                                if cached_row is None or all(field in cached_row for field in FIELDS):
                                    continue
                            try:
                                row = parse_detail(client.html(url), url, label)
                                if row and row["fecha_recepcion"] == now().date().isoformat():
                                    raise ValueError("Cierra hoy con hora sin publicar")
                                with db:
                                    db.execute("INSERT OR REPLACE INTO fichas VALUES (?, ?)",
                                               (url, json.dumps(row, ensure_ascii=False)))
                            except (requests.RequestException, ValueError) as exc:
                                pending.append({"url": url, "error": str(exc)})
                except (requests.RequestException, ValueError) as exc:
                    pending.append({"modalidad": label, "error": str(exc)})
        except (requests.RequestException, ValueError, DeadlineReached) as exc:
            pending.append({"error": str(exc)})

        moment = now()
        active = []
        observed = []
        records = db.execute("SELECT url, data FROM fichas")
        for url, data in records:
            if url not in seen:
                continue
            row = json.loads(data)
            if row is None or all(field in row for field in FIELDS):
                observed.append(url)
            if row is not None and all(field in row for field in FIELDS) and parse_date(row["fecha_recepcion"]) > moment:
                detect_interest(row)
                active.append(row)
        active.sort(key=lambda row: (row["fecha_recepcion"], row["expediente"], row["url"]))
        csv_name = "licitaciones_parcial.csv" if pending else "licitaciones.csv"
        interest_csv = "licitaciones_interes_parcial.csv" if pending else "licitaciones_interes.csv"
        interest = [row for row in active if row["coincidencias_interes"] or row["posibles_interes"]]
        write_csv(output / csv_name, FIELDS, active)
        write_csv(output / interest_csv, FIELDS, interest)
        status.update(status="parcial" if pending else "exitoso", csv=csv_name,
                      vigentes=len(active), coincidencias_interes=sum(bool(row["coincidencias_interes"]) for row in interest),
                      posibles_interes=sum(bool(row["posibles_interes"]) for row in interest), vigentes_interes=len(interest),
                      csv_interes=interest_csv, finished_at=stamp())
        publish_shortlist(db, output, active, observed, status)
        write_json(status_path, status)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "licitaciones")
    parser.add_argument("--timeout-minutes", type=int, default=45)
    args = parser.parse_args()
    if args.timeout_minutes < 1:
        parser.error("--timeout-minutes debe ser positivo")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with pipeline_lock({"_data": args.output_dir}), requests.Session() as session:
            status = scan(args.output_dir, Client(session, now() + timedelta(minutes=args.timeout_minutes)))
    except (BusyError, OSError, sqlite3.Error, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{status['status']} | {status['pages']} páginas | {status['vigentes']} vigentes | "
          f"{status['vigentes_interes']} BID/UCP/JICA para revisar | "
          f"{len(status['pendientes'])} pendientes | {args.output_dir / status['csv']}")
    if status["pendientes"]:
        print(status["pendientes"][0]["error"], file=sys.stderr)
    return 2 if status["pendientes"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
