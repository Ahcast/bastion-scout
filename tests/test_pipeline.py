from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clasificador
import combiner
import common
import main
import scout

FIXED = datetime(2026, 10, 21, 20, tzinfo=common.HN)


def config(directory):
    cfg = json.loads((common.ROOT / "config.example.json").read_text())
    cfg.update(_data=str(directory), _config_dir=str(directory))
    cfg["scout"].update(detail_retry_seconds=0, cooldown_seconds=0)
    return cfg


def detail(number, day=None, description="Taladro industrial", complete=True):
    url = f"http://example.test/Proceso.aspx?id={number}"
    date_value = day if day is not None else FIXED.date()
    date_text = datetime.combine(date_value, datetime.min.time(), common.HN).isoformat() if date_value else ""
    row = dict.fromkeys(common.RAW_FIELDS, "")
    row.update(run_id="2026-10-21", proceso_id=scout.process_id(url), url=url,
               entidad="Entidad", expediente=f"EXP-{number}", fecha_recepcion=date_text,
               fecha_recepcion_original=date_text, descripcion=description, items_fuente="[]",
               extraido_en=FIXED.isoformat(), extraccion_estado="completo" if complete else "incompleto")
    return row


class FakeSite:
    def __init__(self, pages, details, fail_once=(), repeat=False):
        self.pages = pages
        self.details = details
        self.fail_once = set(fail_once)
        self.calls = []
        self.repeat = repeat

    def search(self):
        return scout.Page(self.pages[0], {}, len(self.pages) > 1,
                          {url: self.details[url].get("fecha_recepcion", "") for url in self.pages[0]})

    def advance(self, page, number):
        if self.repeat:
            return self.search()
        return scout.Page(self.pages[number - 1], {}, number < len(self.pages),
                          {url: self.details[url].get("fecha_recepcion", "") for url in self.pages[number - 1]})

    def detail(self, url, run_id):
        self.calls.append(url)
        if url in self.fail_once:
            self.fail_once.remove(url)
            raise RuntimeError("sitio temporalmente caído")
        result = dict(self.details[url])
        result["run_id"] = run_id
        return result


class FakeAI:
    def __init__(self):
        self.calls = []

    def classify(self, row):
        self.calls.append(row["proceso_id"])
        return {"puntaje": 85, "rubro": "Herramientas", "resumen": "Taladro industrial",
                "items": [row["descripcion"]]}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=common.ROOT / "tests")
        self.addCleanup(self.temp.cleanup)
        self.cfg = config(Path(self.temp.name))
        for module in (common, scout, combiner, clasificador, main):
            mocked = patch.object(module, "now", return_value=FIXED)
            mocked.start()
            self.addCleanup(mocked.stop)
        logging = patch("logging.Logger.exception")
        logging.start()
        self.addCleanup(logging.stop)

    def source(self, values, run_id="2026-10-21", status="exitoso"):
        for row in values:
            row["run_id"] = run_id
        common.write_csv(common.data_path(self.cfg, "raw.csv"), common.RAW_FIELDS, values)
        state = {"run_id": run_id, "status": status, "scanner_version": scout.VERSION,
                 "raw_sha256": common.file_hash(common.data_path(self.cfg, "raw.csv")),
                 "scope": {"modalidad": "2", "tipo": "1"}}
        common.write_json(common.data_path(self.cfg, "scout.json"), state)
        return state

    def master(self):
        return list(common.rows(common.data_path(self.cfg, "maestro.csv")))

    def scan(self, values, pages=None, **kwargs):
        mapping = {r["url"]: r for r in values}
        client = FakeSite(pages or [list(mapping)], mapping, **kwargs)
        result = scout.run(self.cfg, "2026-10-21", client=client)
        return result, client

    def test_day_only_includes_today_even_if_time_passed(self):
        yesterday = FIXED.date() - timedelta(days=1)
        values = [detail(1)] + [detail(i, yesterday) for i in range(2, 12)] + [detail(12)]
        result, client = self.scan(values, [list(r["url"] for r in values[:6]), list(r["url"] for r in values[6:])])
        self.assertEqual(result["status"], "exitoso")
        self.assertEqual(result["reason"], "fin_resultados_verificado")
        self.assertEqual(result["extracted"], 2)
        self.assertIn(values[-1]["url"], client.calls)
        self.assertEqual(result["coverage"]["audit_omissions"], 1)

    def test_missing_description_preserved_and_marked_pending(self):
        row = detail(1, description="", complete=False)
        result, _ = self.scan([row])
        self.assertEqual(result["status"], "parcial")
        self.assertEqual(len(list(common.rows(common.data_path(self.cfg, "raw.csv")))), 1)
        self.assertEqual(len(result["pending"]), 1)

    def test_resume_retries_failed_detail_without_refetching_success(self):
        values = [detail(1), detail(2)]
        client = FakeSite([[r["url"] for r in values]], {r["url"]: r for r in values}, [values[1]["url"]])
        first = scout.run(self.cfg, "2026-10-21", client=client)
        self.assertEqual(first["status"], "parcial")
        second = scout.run(self.cfg, "2026-10-21", client=client)
        self.assertEqual(second["status"], "exitoso")
        self.assertEqual(client.calls.count(values[0]["url"]), 1)
        self.assertEqual(client.calls.count(values[1]["url"]), 2)
        self.assertEqual(second["extracted"], 2)

    def test_repeated_page_is_not_success(self):
        row = detail(1)
        result, _ = self.scan([row], [[row["url"]], [row["url"]]], repeat=True)
        self.assertEqual(result["status"], "parcial")
        self.assertIn("repetida", result["error"])

    def test_known_process_behind_cutoff_is_revisited(self):
        known = detail(99, FIXED.date() + timedelta(days=3))
        self.source([known])
        combiner.run(self.cfg)
        expired = [detail(i, FIXED.date() - timedelta(days=1)) for i in range(10)]
        values = expired + [known]
        result, client = self.scan(values, [[r["url"] for r in expired]])
        self.assertEqual(result["status"], "exitoso")
        self.assertIn(known["url"], client.calls)
        self.assertEqual(result["known_rechecked"], 1)

    def test_previous_daily_pending_is_recovered(self):
        row = detail(99)
        with common.database(self.cfg) as db:
            db.execute("INSERT INTO scan VALUES (?,?,?,NULL)", ("2026-10-20", row["url"], "error"))
            db.commit()
        expired = [detail(i, FIXED.date() - timedelta(days=1)) for i in range(10)]
        result, client = self.scan(expired + [row], [[r["url"] for r in expired]])
        self.assertIn(row["url"], client.calls)
        self.assertEqual(result["status"], "exitoso")
        self.assertEqual(result["extracted"], 1)

    def test_stale_broken_backlog_does_not_block_fresh_listing(self):
        fresh = detail(1)
        stale = [detail(i) for i in range(90, 93)]
        with common.database(self.cfg) as db:
            for row in stale:
                db.execute("INSERT INTO scan VALUES (?,?,?,NULL)", ("2026-10-20", row["url"], "error"))
            db.commit()
        site = FakeSite([[fresh["url"]]], {fresh["url"]: fresh}, fail_once=[r["url"] for r in stale])
        result = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertEqual(site.calls[0], fresh["url"])
        self.assertEqual(result["extracted"], 1)
        self.assertEqual(result["status"], "exitoso")
        self.assertTrue(result["pendientes_historicos"])

    def test_partial_raw_accumulates_without_removing_absent_processes(self):
        self.source([detail(1), detail(2)])
        self.assertEqual(combiner.run(self.cfg)["status"], "exitoso")
        self.source([detail(3)], status="parcial")
        result = combiner.run(self.cfg)
        self.assertEqual(result["status"], "exitoso")
        self.assertEqual(len(self.master()), 3)

    def test_unchanged_preserves_analysis_and_change_invalidates_it(self):
        self.source([detail(1)])
        combiner.run(self.cfg)
        ai = FakeAI()
        self.assertEqual(clasificador.run(self.cfg, client=ai)["status"], "exitoso")
        combiner.run(self.cfg)
        self.assertEqual(self.master()[0]["analisis_estado"], "analizado")
        self.source([detail(1, description="Otro equipo")])
        combiner.run(self.cfg)
        self.assertEqual(self.master()[0]["analisis_estado"], "pendiente")
        self.assertEqual(self.master()[0]["puntaje"], "")
        clasificador.run(self.cfg, client=ai)
        self.assertEqual(len(ai.calls), 2)

    def test_classifier_never_reanalyses_for_model_or_skill_change(self):
        self.source([detail(1)])
        combiner.run(self.cfg)
        ai = FakeAI()
        clasificador.run(self.cfg, client=ai)
        self.cfg["ai"]["model"] = "another-model"
        self.cfg["ai"]["skill_path"] = "not-present.md"
        result = clasificador.run(self.cfg, client=ai)
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(result["unchanged"], 1)

    def test_paid_result_survives_crash_before_csv_replace(self):
        self.source([detail(1)])
        combiner.run(self.cfg)
        ai = FakeAI()

        def interrupt(path, fields, values):
            for _ in values:
                pass
            raise OSError("Simulación de interrupción antes de publicar CSV")

        with patch.object(clasificador, "write_csv", side_effect=interrupt):
            self.assertEqual(clasificador.run(self.cfg, client=ai)["status"], "error")
        self.assertEqual(self.master()[0]["analisis_estado"], "pendiente")
        retry = clasificador.run(self.cfg, client=ai)
        self.assertEqual(retry["reused"], 1)
        self.assertEqual(len(ai.calls), 1)

    def test_items_are_numbered_multiline_and_original_data_is_preserved(self):
        original = detail(1, description='6 contactores, modelo "ABB-001"\n18 fusibles 25 A')
        self.source([original])
        combiner.run(self.cfg)

        class ItemsAI:
            def classify(self, row):
                return {"puntaje": 90, "rubro": "Automatización y control industrial",
                        "resumen": "Suministro de contactores y fusibles para un sistema de control industrial.",
                        "items": ['6 contactores, modelo "ABB-001"', "18 fusibles, 25 A"]}

        self.assertEqual(clasificador.run(self.cfg, client=ItemsAI())["status"], "exitoso")
        row = self.master()[0]
        self.assertEqual(row["items"], '1. 6 contactores, modelo "ABB-001"\n2. 18 fusibles, 25 A')
        for key in common.RAW_FIELDS:
            self.assertEqual(row[key], original[key], key)

    def test_legacy_master_is_migrated_without_another_ai_call(self):
        self.source([detail(1)])
        combiner.run(self.cfg)
        clasificador.run(self.cfg, client=FakeAI())
        row = self.master()[0]
        row["resumen_corto"] = row.pop("resumen")
        row["items"] = json.dumps(["6 contactores", "18 fusibles"])
        path = common.data_path(self.cfg, "maestro.csv")
        common.write_csv(path, common.LEGACY_MASTER_FIELDS, [row])
        self.assertFalse(common.current_master_schema(path))
        ai = FakeAI()
        result = clasificador.run(self.cfg, client=ai)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(ai.calls, [])
        self.assertTrue(common.current_master_schema(path))
        self.assertEqual(self.master()[0]["items"], "1. 6 contactores\n2. 18 fusibles")
        self.assertEqual(self.master()[0]["resumen"], row["resumen_corto"])

    def test_legacy_cached_result_is_reused_without_another_ai_call(self):
        self.source([detail(1)])
        combiner.run(self.cfg)
        row = self.master()[0]
        legacy = {"puntaje": 85, "rubro": "Herramientas", "resumen_corto": "Compra de taladros industriales.",
                  "items": ["2 taladros industriales"]}
        with common.database(self.cfg) as db:
            db.execute("INSERT INTO analysis VALUES (?,?,?,?,?)", (row["proceso_id"], row["contenido_hash"],
                       json.dumps(legacy), FIXED.isoformat(), "old-model"))
            db.commit()
        ai = FakeAI()
        result = clasificador.run(self.cfg, client=ai)
        self.assertEqual(result["reused"], 1)
        self.assertEqual(ai.calls, [])
        self.assertEqual(self.master()[0]["resumen"], legacy["resumen_corto"])
        self.assertEqual(self.master()[0]["items"], "1. 2 taladros industriales")

    def test_main_migrates_completed_legacy_master_without_scanning_or_spending_tokens(self):
        self.source([detail(1)])
        combiner.run(self.cfg)
        clasificador.run(self.cfg, client=FakeAI())
        row = self.master()[0]
        row["resumen_corto"] = row.pop("resumen")
        row["items"] = json.dumps(["Taladro industrial"])
        path = common.data_path(self.cfg, "maestro.csv")
        common.write_csv(path, common.LEGACY_MASTER_FIELDS, [row])
        ai = FakeAI()
        original = clasificador.run
        with patch.object(main.scout, "run") as scanner:
            with patch.object(main.clasificador, "run", side_effect=lambda cfg, rid, dl: original(cfg, rid, dl, client=ai)):
                result = main.run(self.cfg)
        self.assertTrue(result["output_ready"])
        self.assertFalse(scanner.called)
        self.assertEqual(ai.calls, [])
        self.assertTrue(common.current_master_schema(path))

    def test_indeterminable_object_keeps_row_and_empty_items(self):
        self.source([detail(1, description="Adquisición para necesidades institucionales sin especificaciones adjuntas")])
        combiner.run(self.cfg)

        class UnknownAI:
            def classify(self, row):
                return {"puntaje": 42, "rubro": "Categoría de prueba",
                        "resumen": "Resultado sintético para comprobar una lista de items vacía.", "items": []}

        result = clasificador.run(self.cfg, client=UnknownAI())
        self.assertEqual(result["status"], "exitoso")
        self.assertEqual(len(self.master()), 1)
        self.assertEqual(self.master()[0]["puntaje"], "42")
        self.assertEqual(self.master()[0]["items"], "")

    def test_retention_tombstone_prevents_reentry_and_new_hash_can_return(self):
        old = detail(1, FIXED.date() + timedelta(days=20))
        old["extraido_en"] = (FIXED - timedelta(days=31)).isoformat()
        self.source([old])
        result = combiner.run(self.cfg)
        self.assertEqual(result["removed_retention"], 1)
        self.assertEqual(self.master(), [])
        self.source([detail(1, FIXED.date() + timedelta(days=20))])
        result = combiner.run(self.cfg)
        self.assertEqual(result["suppressed_retired"], 1)
        self.assertEqual(self.master(), [])
        self.source([detail(1, FIXED.date() + timedelta(days=21))])
        combiner.run(self.cfg)
        self.assertEqual(len(self.master()), 1)

    def test_incomplete_observation_does_not_erase_good_data(self):
        self.source([detail(1)])
        combiner.run(self.cfg)
        self.source([detail(1, description="", complete=False)])
        combiner.run(self.cfg)
        self.assertEqual(self.master()[0]["descripcion"], "Taladro industrial")

    def test_invalid_raw_does_not_replace_master(self):
        self.source([detail(1)])
        combiner.run(self.cfg)
        before = common.file_hash(common.data_path(self.cfg, "maestro.csv"))
        row = detail(2)
        row["proceso_id"] = ""
        self.source([row])
        self.assertEqual(combiner.run(self.cfg)["status"], "error")
        self.assertEqual(common.file_hash(common.data_path(self.cfg, "maestro.csv")), before)

    def test_wrong_header_is_rejected_even_when_empty(self):
        self.source([detail(1)])
        combiner.run(self.cfg)
        before = common.file_hash(common.data_path(self.cfg, "maestro.csv"))
        raw = common.data_path(self.cfg, "raw.csv")
        raw.write_text("incorrecto\n", encoding="utf-8")
        state = common.read_json(common.data_path(self.cfg, "scout.json"))
        state["raw_sha256"] = common.file_hash(raw)
        common.write_json(common.data_path(self.cfg, "scout.json"), state)
        self.assertEqual(combiner.run(self.cfg)["status"], "error")
        self.assertEqual(common.file_hash(common.data_path(self.cfg, "maestro.csv")), before)

    def test_recover_previous_cycle_before_new_raw_overwrites_it(self):
        row = detail(99, FIXED.date() + timedelta(days=2))
        row["run_id"] = "2026-10-20"
        with common.database(self.cfg) as db:
            db.execute("INSERT INTO scan VALUES (?,?,?,?)", (row["run_id"], row["url"], "ok", json.dumps(row)))
            db.commit()
        common.write_json(common.data_path(self.cfg, "scout.json"), {
            "run_id": row["run_id"], "status": "ejecutando"})
        original_scanner = scout.run
        original_classifier = clasificador.run
        new = detail(1)
        site = FakeSite([[new["url"]]], {new["url"]: new, row["url"]: row})
        with patch.object(main.scout, "run", side_effect=lambda cfg, rid, dl: original_scanner(cfg, rid, dl, client=site)):
            with patch.object(main.clasificador, "run", side_effect=lambda cfg, rid, dl: original_classifier(cfg, rid, dl, client=FakeAI())):
                result = main.run(self.cfg)
        self.assertTrue(result["output_ready"])
        self.assertEqual(len(self.master()), 2)

    def test_dates_and_normalized_hash_preserve_meaning(self):
        self.assertEqual(common.parse_date("21/10/2026 01:05:00 p. m.").hour, 13)
        self.assertEqual(common.parse_date("21/10/2026").date(), FIXED.date())
        self.assertEqual(common.parse_date("2026-10-22T01:00:00+00:00").day, 21)
        a = detail(1, description="Taladro\nindustrial")
        b = detail(1, description="Taladro industrial")
        self.assertEqual(common.content_hash(a), common.content_hash(b))
        b["fecha_recepcion"] = b["fecha_recepcion"].replace("00:00:00", "01:00:00")
        self.assertNotEqual(common.content_hash(a), common.content_hash(b))

    def test_main_retries_classifier_only_and_then_stays_done(self):
        self.source([detail(1)])
        combiner.run(self.cfg)
        ai = FakeAI()

        original = clasificador.run
        with patch.object(main.scout, "run") as scan_mock, patch.object(main.combiner, "run") as combine_mock:
            with patch.object(main.clasificador, "run", side_effect=lambda cfg, rid, dl: original(cfg, rid, dl, client=ai)) as classify_mock:
                result = main.run(self.cfg)
                self.assertTrue(result["output_ready"])
                self.assertFalse(scan_mock.called)
                self.assertFalse(combine_mock.called)
                self.assertEqual(classify_mock.call_count, 1)
                main.run(self.cfg)
                self.assertEqual(classify_mock.call_count, 1)

    def test_lock_excludes_another_writer(self):
        with common.pipeline_lock(self.cfg):
            with self.assertRaises(common.BusyError):
                with common.pipeline_lock(self.cfg):
                    pass
        with common.pipeline_lock(self.cfg):
            pass


class ParsingTests(unittest.TestCase):
    def test_private_skill_loads_only_explicit_references(self):
        with tempfile.TemporaryDirectory(dir=common.ROOT / "tests") as directory:
            root = Path(directory)
            (root / "skill.md").write_text("Instrucciones sintéticas de prueba.", encoding="utf-8")
            (root / "referencia.md").write_text("Referencia sintética seleccionada.", encoding="utf-8")
            (root / "calibracion.md").write_text("EJEMPLO_NO_INCLUIR", encoding="utf-8")
            cfg = config(root)
            cfg["ai"]["skill_path"] = "skill.md"
            cfg["ai"]["reference_paths"] = ["referencia.md"]
            instructions = clasificador.load_instructions(cfg)
            self.assertIn("Instrucciones sintéticas de prueba.", instructions)
            self.assertIn("Referencia sintética seleccionada.", instructions)
            self.assertNotIn("EJEMPLO_NO_INCLUIR", instructions)
            self.assertLess(len(instructions.encode("utf-8")), cfg["ai"]["max_context_bytes"])

    def test_openai_schema_and_response_without_network(self):
        cfg = config(Path("."))
        result = {"puntaje": 80, "rubro": "Instrumentos", "resumen": "Compra de equipo", "items": ["Equipo ABC"]}
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.json.return_value = {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}],
            "usage": {"input_tokens": 100, "output_tokens": 30}}
        with patch.dict("os.environ", {"BASTION_AI_API_KEY": "test-not-a-real-key"}):
            client = clasificador.OpenAIClient(cfg, "Criterios de prueba")
        with patch.object(client.session, "post", return_value=response) as post, patch.object(clasificador, "pause"):
            self.assertEqual(client.classify(detail(1)), result)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["model"], "gpt-6-luna")
        self.assertTrue(sent["text"]["format"]["strict"])
        self.assertFalse(sent["store"])
        self.assertEqual(client.usage["input_tokens"], 100)
        client.close()

    def test_http_retry_honors_server_wait_without_parallel_requests(self):
        cfg = config(Path("."))
        client = scout.HonduClient(cfg)
        bad = MagicMock()
        bad.__enter__.return_value = bad
        bad.headers = {"Retry-After": "60"}
        bad.raise_for_status.side_effect = scout.requests.HTTPError("503")
        good = MagicMock()
        good.__enter__.return_value = good
        good.headers = {}
        good.encoding = "utf-8"
        good.iter_content.return_value = [b"<html>ok</html>"]
        with patch.object(client.session, "request", side_effect=[bad, good]) as request, patch.object(scout, "pause") as wait:
            self.assertEqual(client.html("http://example.test"), "<html>ok</html>")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(client.retries, 1)
        self.assertTrue(any(call.args[0] >= 60 for call in wait.call_args_list))
        client.close()

    def test_explicit_empty_table_only(self):
        html = '<form><table id="ctl00_cphCuerpo_gvResultados"><tr><td>No se encontraron registros</td></tr></table></form>'
        self.assertEqual(scout.parse_results(html, "http://example.test", 1).links, [])
        with self.assertRaises(ValueError):
            scout.parse_results(html.replace("No se encontraron registros", "Error"), "http://example.test", 1)

    def test_detail_keeps_quantity_and_full_specification(self):
        html = '''<html><table id="ctl00_cphCuerpo_dvProceso">
        <tr><td>Entidad</td><td>Hospital</td></tr><tr><td>Expediente</td><td>ABC</td></tr>
        <tr><td>Fecha Recepción Ofertas</td><td>21/10/2026 01:00:00 p.m.</td></tr></table>
        <table id="ctl00_cphCuerpo_WebTab1_tmpl0_wdgProductos"><tr><td>1</td><td>20</td>
        <td>Taladro ABB XYZ-001, 220V</td><td>unidades</td></tr></table></html>'''
        row = scout.parse_detail(html, "http://example.test/?id=1", "test")
        self.assertEqual(row["extraccion_estado"], "completo")
        self.assertIn("20", row["descripcion"])
        self.assertIn("XYZ-001", row["descripcion"])

    def test_daily_overnight_window(self):
        self.assertEqual(main.daily_window(FIXED)[0], "2026-10-21")
        morning = FIXED.replace(hour=12)
        rid, deadline = main.daily_window(morning)
        self.assertEqual(rid, "2026-10-20")
        self.assertEqual(deadline, FIXED.replace(hour=13))
        self.assertIsNone(main.daily_window(FIXED.replace(hour=13)))
        self.assertIsNone(main.daily_window(FIXED.replace(hour=18)))

    def test_schema_rejects_bool_score_and_nonstring_items(self):
        with self.assertRaises(ValueError):
            clasificador.validate_result({"puntaje": True, "rubro": "A", "resumen": "B", "items": ["C"]})
        with self.assertRaises(ValueError):
            clasificador.validate_result({"puntaje": 80, "rubro": "A", "resumen": "B", "items": [1]})


if __name__ == "__main__":
    unittest.main()
