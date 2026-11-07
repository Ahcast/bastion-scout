from contextlib import closing
from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests
from bs4 import BeautifulSoup

import scout_licitaciones as scanner
from common import HN, rows


NOW = datetime(2026, 11, 6, 12, tzinfo=HN)
FORM = f'''<form><input name="__VIEWSTATE" value="search-state">
<select name="{scanner.MODALITY}"><option value="0">(Todas)</option>
<option value="1">Licitación o Concurso</option><option value="2">Compra Menor</option></select>
<select name="{scanner.ACQUISITION}"><option value="0">(Todas)</option>
<option value="1">Suministro de Bienes y/o Servicios</option></select>
<select name="{scanner.STAGE}"><option value="0">(Todas)</option>
<option value="3">Recepción de Ofertas</option></select></form>'''


def listing(results):
    return f'''<form><input name="__VIEWSTATE" value="page-state">
    <table id="ctl00_cphCuerpo_gvResultados"><tr><th>Proceso</th><th>Modalidad</th><th>Perido de Vigencia</th></tr>
    {results}</table></form>'''


def result(identifier, date, modality="Licitación pública nacional"):
    return (f'<tr><td><a id="row{identifier}_hlGoTo" href="ProcesoHistorico.aspx?id={identifier}">Detalle</a></td>'
            f'<td>{modality}</td><td>Fecha Inicio: 01/10/2026 Fecha Cierre: {date}</td></tr>')


def detail(identifier, date, modality="Licitación pública nacional", stage="Recepción de ofertas", extra=None):
    data = {"Expediente": identifier, "Entidad": "Hospital", "Objeto": "Compra de insumos médicos",
            "Modalidad": modality, "Fecha Recepción Ofertas": date, "Etapa": stage}
    data.update(extra or {})
    body = "".join(f"<tr><td>{label}</td><td>{value}</td></tr>" for label, value in data.items())
    return f'<table id="ctl00_cphCuerpo_dvProceso">{body}</table>'


class LicitacionesTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        for patcher in (patch.object(scanner, "now", return_value=NOW),
                        patch("common.now", return_value=NOW), patch.object(scanner, "pause")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.session = Mock(headers={})
        self.session.request.side_effect = self.respond
        self.posts = []
        self.fetched = []
        self.fail_detail = False
        self.repeated = False

    def respond(self, method, url, data, **kwargs):
        if url == scanner.SEARCH_URL:
            if method == "GET":
                html = FORM
            else:
                self.posts.append(data.copy())
                first = listing(result("A", "06/11/2026") + result("old", "01/11/2026") +
                                result("contest", "30/11/2026", "Concurso privado") +
                                '<tr><td><a href="javascript:__doPostBack(\'grid\',\'Page$2\')">2</a></td></tr>')
                if not data.get("__EVENTARGUMENT") or self.repeated:
                    html = first
                else:
                    html = listing(result("B", "06/11/2026") + result("C", "10/11/2026") +
                                   result("D", "10/11/2026", "Licitación o Concurso"))
        else:
            identifier = url.rsplit("=", 1)[1]
            self.fetched.append(identifier)
            if identifier == "C" and self.fail_detail:
                raise requests.ConnectionError("detalle temporalmente inaccesible")
            html = {"A": detail("A", "06/11/2026 03:00 p.m.", extra={
                        "Unidad de Compra": "Unidad Coordinadora de Proyectos (UCP)",
                        "Lugar Recepción Ofertas": "Oficina UCP, segundo piso, Tegucigalpa",
                        "Tipo Fuente": "Recursos Externos", "Fuente": "Banco Interamericano de Desarrollo"}),
                    "B": detail("B", "06/11/2026 09:00 a.m."),
                    "C": detail("C", "10/11/2026 11:00 a.m.", stage="Cancelado"),
                    "D": detail("D", "10/11/2026 11:00 a.m.", modality="Concurso público")}[identifier]
        response = requests.Response()
        response.status_code = 200
        response.raw = BytesIO(html.encode("utf-8"))
        return response

    def scan(self):
        return scanner.scan(self.output, scanner.Client(self.session, NOW + timedelta(hours=1)))

    def test_scans_modalities_and_pages_and_filters_by_detail(self):
        status = self.scan()
        self.assertEqual((status["status"], status["pages"]), ("exitoso", 2))
        self.assertEqual([row["expediente"] for row in rows(self.output / status["csv"])], ["A"])
        self.assertEqual(self.fetched, ["A", "B", "C", "D"])
        self.assertEqual(self.posts[0][scanner.MODALITY], "1")
        self.assertEqual(self.posts[0][scanner.ACQUISITION], "0")
        self.assertEqual(self.posts[0][scanner.STAGE], "3")
        self.assertEqual(self.posts[1], {"__VIEWSTATE": "page-state", "__EVENTTARGET": "grid",
                                         "__EVENTARGUMENT": "Page$2"})
        exported = list(rows(self.output / status["csv_interes"]))
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["coincidencias_interes"], "BID | UCP")
        self.assertEqual(exported[0]["unidad_compra"], "Unidad Coordinadora de Proyectos (UCP)")
        self.assertEqual(exported[0]["lugar_recepcion"], "Oficina UCP, segundo piso, Tegucigalpa")
        self.assertIn("BID (fuente) Banco Interamericano de Desarrollo", exported[0]["evidencia_interes"])

    def test_partial_preserves_last_complete_and_retries_only_missing_details(self):
        last = self.output / "licitaciones.csv"
        last.write_bytes(b"ultimo completo")
        self.fail_detail = True
        first = self.scan()
        self.assertEqual(first["status"], "parcial")
        self.assertEqual(last.read_bytes(), b"ultimo completo")
        self.assertEqual(first["pendientes"][0]["error"], "detalle temporalmente inaccesible")
        self.assertEqual([row["expediente"] for row in rows(self.output / first["csv"])], ["A"])
        self.fail_detail = False
        self.fetched.clear()
        self.assertEqual(self.scan()["status"], "exitoso")
        self.assertEqual(self.fetched, ["C"])

    def test_repeated_page_is_partial(self):
        self.repeated = True
        status = self.scan()
        self.assertEqual(status["status"], "parcial")
        self.assertIn("repitió", status["pendientes"][0]["error"])

    def test_japan_is_possible_jica_and_acronyms_need_boundaries(self):
        for source, expected, possible in (
            ("Japón", "", "JICA"),
            ("Agencia de Cooperación Internacional del Japón (JICA)", "JICA", ""),
            ("Gobierno de Japón / JICA", "JICA", ""),
            ("Préstamo BID5831-BL-HO", "BID", ""),
            ("Entidad", "", ""),
        ):
            with self.subTest(source=source):
                soup = BeautifulSoup(detail("LPN-ABID-UCPI-JICAX", "07/11/2026 10:00 a.m.",
                                             extra={"Fuente": source}), "html.parser")
                row = scanner.parse_detail(soup, "http://example.test", scanner.TENDER_GROUP)
                self.assertEqual(row["coincidencias_interes"], expected)
                self.assertEqual(row["posibles_interes"], possible)

    def test_partial_cache_from_previous_schema_is_downloaded_again(self):
        self.fail_detail = True
        self.scan()
        with closing(sqlite3.connect(self.output / "licitaciones.sqlite3")) as db:
            with db:
                url, data = db.execute("SELECT url,data FROM fichas WHERE data LIKE '%Hospital%' LIMIT 1").fetchone()
                old = json.loads(data)
                del old["unidad_compra"]
                db.execute("UPDATE fichas SET data=? WHERE url=?", (json.dumps(old), url))
        self.fail_detail = False
        self.fetched.clear()
        self.assertEqual(self.scan()["status"], "exitoso")
        self.assertEqual(self.fetched, ["A", "C"])

    def test_shortlist_preserves_partial_results_and_alert_versions(self):
        status = self.scan()
        feed_path = self.output / "lista_corta.json"
        first = json.loads(feed_path.read_text(encoding="utf-8"))["items"][0]
        active = list(rows(self.output / status["csv"]))
        active[0]["extraido_en"] = "2026-11-06T12:05:00-06:00"
        ordinary = dict(active[0], expediente="Sin prioridad", url=active[0]["url"].replace("id=A", "id=ordinary"),
                        fecha_recepcion="2026-11-06T13:00:00-06:00", coincidencias_interes="", evidencia_interes="")
        with closing(sqlite3.connect(self.output / "licitaciones.sqlite3")) as db:
            scanner.publish_shortlist(db, self.output, [ordinary, *active], [active[0]["url"]], {"status": "parcial"})
            items = json.loads(feed_path.read_text(encoding="utf-8"))["items"]
            self.assertEqual([item["expediente"] for item in items], ["A", "Sin prioridad"])
            same = items[0]
            self.assertEqual(same["version"], first["version"])
            scanner.publish_shortlist(db, self.output, [], [], {"status": "parcial"})
            retained = json.loads(feed_path.read_text(encoding="utf-8"))
            self.assertEqual(len(retained["items"]), 2)
            active[0]["lugar_recepcion"] = "Nueva oficina de la UCP"
            scanner.publish_shortlist(db, self.output, active, [active[0]["url"]], {"status": "exitoso"})
            changed = json.loads(feed_path.read_text(encoding="utf-8"))["items"][0]
            self.assertNotEqual(changed["version"], first["version"])
            self.assertEqual(changed["first_seen_at"], first["first_seen_at"])
            scanner.publish_shortlist(db, self.output, [], [], {"status": "exitoso"})
            self.assertEqual(json.loads(feed_path.read_text(encoding="utf-8"))["items"], [])


if __name__ == "__main__":
    unittest.main()
