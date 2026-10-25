from __future__ import annotations

from datetime import timedelta
import json
import unittest
from unittest.mock import patch

import test_pipeline as f
import clasificador
import combiner
import common
import main
import scout
from scout_store import ScanStore


class RecoveryTests(unittest.TestCase):
    setUp = f.PipelineTests.setUp
    source = f.PipelineTests.source
    master = f.PipelineTests.master
    scan = f.PipelineTests.scan

    def test_page_three_failure_resumes_with_fresh_form_and_cached_details(self):
        values = [f.detail(i) for i in range(1, 5)]
        pages = [[row["url"]] for row in values]

        class Site(f.FakeSite):
            broken = True
            advances = []

            def advance(self, page, number):
                self.advances.append(number)
                if self.broken and number == 3:
                    raise RuntimeError("sesión caducada")
                return super().advance(page, number)

        site = Site(pages, {row["url"]: row for row in values})
        first = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertEqual(first["status"], "parcial")
        self.assertEqual(first["phases"]["discovery"]["next_page"], 3)
        self.assertEqual(site.calls, [values[0]["url"], values[1]["url"]])
        site.broken, site.advances = False, []
        second = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertTrue(second["ronda_actual_completa"])
        self.assertEqual(site.advances, [2, 3, 4])  # Rebuild ViewState by replaying from page 1.
        self.assertEqual(len(site.calls), 4)
        self.assertEqual(second["pages"], 4)
        with common.database(self.cfg) as db:
            journal = db.execute("SELECT data FROM scout_round").fetchone()[0]
            self.assertNotIn("VIEWSTATE", journal)

    def test_http_500_waits_before_rebuilding_the_live_pager(self):
        values = [f.detail(i) for i in range(1, 3)]

        class Site(f.FakeSite):
            last_status_code = None
            last_retry_after_seconds = 0
            failed = False

            def advance(self, page, number):
                if number == 2 and not self.failed:
                    self.failed = True
                    self.last_status_code = 500
                    raise RuntimeError("HonduCompras no respondió: HTTP 500")
                self.last_status_code = None
                return super().advance(page, number)

            def restart_search(self):
                return self.search()

        site = Site([[values[0]["url"]], [values[1]["url"]]],
                    {row["url"]: row for row in values})
        self.cfg["scout"].update(page_retry_cooldown_seconds=180,
                                 page_retry_max_cooldown_seconds=600)
        with patch.object(scout, "pause") as wait:
            result = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertTrue(result["ronda_actual_completa"])
        wait.assert_called_once()
        self.assertEqual(wait.call_args.args[0], 180)

    def test_pagination_http_500_retries_with_growing_delays(self):
        values = [f.detail(i) for i in range(1, 3)]

        class Site(f.FakeSite):
            last_status_code = None
            last_retry_after_seconds = 0
            failures_left = 3

            def advance(self, page, number):
                if number == 2 and self.failures_left:
                    self.failures_left -= 1
                    self.last_status_code = 500
                    raise RuntimeError("HonduCompras no respondió: HTTP 500")
                self.last_status_code = None
                return super().advance(page, number)

        self.cfg["scout"].update(page_retry_cooldown_seconds=180,
                                 page_retry_max_cooldown_seconds=600,
                                 page_retry_attempts=3)
        site = Site([[values[0]["url"]], [values[1]["url"]]],
                    {row["url"]: row for row in values})
        with patch.object(scout, "pause") as wait:
            result = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertTrue(result["ronda_actual_completa"])
        self.assertEqual([call.args[0] for call in wait.call_args_list], [180, 360, 600])
        self.assertEqual(result["coverage"]["page_retries"], 3)

    def test_initial_search_http_500_also_waits_before_rebuilding(self):
        row = f.detail(1)

        class Site(f.FakeSite):
            last_status_code = None
            last_retry_after_seconds = 0
            failed = False

            def search(self):
                if not self.failed:
                    self.failed = True
                    self.last_status_code = 500
                    raise RuntimeError("HonduCompras no respondió: HTTP 500")
                self.last_status_code = None
                return super().search()

        self.cfg["scout"].update(page_retry_cooldown_seconds=180,
                                 page_retry_max_cooldown_seconds=600)
        site = Site([[row["url"]]], {row["url"]: row})
        with patch.object(scout, "pause") as wait:
            result = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertTrue(result["ronda_actual_completa"])
        wait.assert_called_once()
        self.assertEqual(wait.call_args.args[0], 180)

    def test_all_links_committed_before_first_detail_and_survive_process_death(self):
        values = [f.detail(i) for i in range(1, 4)]

        class Crash(BaseException):
            pass

        class Site(f.FakeSite):
            def detail(self, url, run_id):
                if self.calls:
                    raise Crash()
                # Every link must be visible to a separate connection BEFORE opening detail 1.
                with common.database(self_cfg) as db:
                    self_test.assertEqual(db.execute("SELECT count(*) FROM scout_queue").fetchone()[0], 3)
                return super().detail(url, run_id)

        self_cfg, self_test = self.cfg, self
        site = Site([[row["url"] for row in values]], {row["url"]: row for row in values})
        with self.assertRaises(Crash):
            scout.run(self.cfg, "2026-10-21", client=site)
        with common.database(self.cfg) as db:
            states = [r[0] for r in db.execute("SELECT state FROM scout_queue ORDER BY position")]
        self.assertEqual(states, ["completo", "pendiente", "pendiente"])
        resumed = f.FakeSite(site.pages, site.details)
        result = scout.run(self.cfg, "2026-10-21", client=resumed)
        self.assertTrue(result["ronda_actual_completa"])
        self.assertEqual(resumed.calls, [row["url"] for row in values[1:]])

    def test_discovery_failure_still_drains_an_existing_queue(self):
        values = [f.detail(i) for i in range(1, 3)]
        with common.database(self.cfg) as db:
            store = ScanStore(db, "2026-10-21", {"modalidad": "2", "tipo": "1"})
            store.capture(scout.Page([r["url"] for r in values], {}, True,
                                     {r["url"]: r["fecha_recepcion"] for r in values}), 1)

        class Site(f.FakeSite):
            def search(self):
                raise RuntimeError("HTTP 500 en búsqueda")

        site = Site([], {row["url"]: row for row in values})
        result = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertEqual(result["status"], "parcial")
        self.assertEqual(result["phases"]["extraction"]["pending"], 0)
        self.assertEqual(set(site.calls), {row["url"] for row in values})

    def test_pagination_actions_are_persisted_with_the_page_checkpoint(self):
        url = "http://example.test/Proceso.aspx?id=1"
        action = {"page": 2, "target": "form$results", "argument": "Page$2",
                  "href": "javascript:__doPostBack('form$results','Page$2')"}
        with common.database(self.cfg) as db:
            store = ScanStore(db, "2026-10-21", {"modalidad": "2", "tipo": "1"})
            store.capture(scout.Page([url], {}, True, {url: "24/10/2026"}, [action]), 1)
            saved = db.execute("SELECT pagination FROM scout_page WHERE run_id=? AND page=1",
                               ("2026-10-21",)).fetchone()[0]
        self.assertEqual(json.loads(saved), [action])

    def test_discovery_budget_expiry_leaves_time_to_drain_queue(self):
        values = [f.detail(i) for i in range(1, 3)]

        class Site(f.FakeSite):
            def advance(self, page, number):
                raise common.DeadlineReached("presupuesto de descubrimiento")

        site = Site([[values[0]["url"]], [values[1]["url"]]], {r["url"]: r for r in values})
        result = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertEqual(site.calls, [values[0]["url"]])
        self.assertEqual(result["phases"]["extraction"]["pending"], 0)

    def test_historical_broken_url_does_not_block_current_round_or_hide_backlog(self):
        current, old = f.detail(1), [f.detail(i) for i in range(90, 94)]
        with common.database(self.cfg) as db:
            for row in old:
                db.execute("INSERT INTO scan VALUES (?,?,?,NULL)", ("2026-10-20", row["url"], "error"))
            db.commit()
        site = f.FakeSite([[current["url"]]], {r["url"]: r for r in [current] + old},
                          fail_once=[r["url"] for r in old])
        result = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertTrue(result["ronda_actual_completa"])
        self.assertEqual(result["status"], "exitoso")
        self.assertEqual(len(result["pendientes_historicos"]), 4)
        self.assertEqual(len(site.calls), 3)  # current + historical budget of two attempts
        self.assertEqual(site.calls[0], current["url"])

    def test_listing_audit_finds_active_after_ten_expired_without_fetching_expired(self):
        expired = [f.detail(i, f.FIXED.date() - timedelta(days=1)) for i in range(10)]
        active = f.detail(100)
        result, site = self.scan(expired + [active], [[r["url"] for r in expired], [active["url"]]])
        self.assertTrue(result["coverage"]["early_stop_disabled"])
        self.assertEqual(result["coverage"]["audit_omissions"], 1)
        self.assertEqual(site.calls, [active["url"]])
        self.assertEqual(result["expired_listed"], 10)
        with common.database(self.cfg) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM scout_queue").fetchone()[0], 1)

    def test_full_listing_continues_after_ten_expired_even_with_legacy_setting(self):
        expired = [f.detail(i, f.FIXED.date() - timedelta(days=1)) for i in range(15)]
        pages = [[r["url"] for r in expired[:10]]] + [[r["url"]] for r in expired[10:]]
        self.cfg["scout"]["early_stop_enabled"] = True  # Existing AWS configs may contain this.
        result, site = self.scan(expired, pages)
        self.assertEqual(result["pages"], 6)
        self.assertEqual(result["reason"], "fin_resultados_verificado")
        self.assertEqual(result["coverage"]["coverage"], "fin_resultados")
        self.assertEqual(site.calls, [])

    def test_unknown_listing_date_is_queued_and_disables_early_stop(self):
        active = f.detail(1)
        expired = [f.detail(i, f.FIXED.date() - timedelta(days=1)) for i in range(2, 13)]

        class Site(f.FakeSite):
            def search(self):
                page = super().search()
                page.closing_dates[active["url"]] = "fecha por confirmar"
                return page

        values = [active] + expired
        site = Site([[r["url"] for r in values]], {r["url"]: r for r in values})
        result = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertEqual(site.calls, [active["url"]])
        self.assertEqual(result["coverage"]["unknown_dates"], 1)
        self.assertTrue(result["coverage"]["early_stop_disabled"])

    def test_current_failures_have_persistent_retry_and_circuit_pause(self):
        row = f.detail(1)
        self.cfg["scout"].update(detail_retry_seconds=60, cooldown_seconds=60, consecutive_failures_limit=1)
        site = f.FakeSite([[row["url"]]], {row["url"]: row}, fail_once=[row["url"]])
        first = scout.run(self.cfg, "2026-10-21", client=site)
        second = scout.run(self.cfg, "2026-10-21", client=site)
        self.assertEqual(len(site.calls), 1)
        self.assertEqual(first["pending"][0]["attempts"], 1)
        self.assertIsNotNone(second["pending"][0]["next_retry"])

    def test_migration_reopens_generic_false_complete_without_losing_record(self):
        row = f.detail(1, description="Compra Menor")
        with common.database(self.cfg) as db:
            db.execute("INSERT INTO scan VALUES (?,?,?,?)",
                       ("2026-10-20", row["url"], "ok", json.dumps(row)))
            db.commit()
            store = ScanStore(db, "2026-10-21", {"modalidad": "2", "tipo": "1"})
            self.assertEqual(len(store.pending(historical=True)), 1)
            saved = db.execute("SELECT data FROM scan").fetchone()[0]
            self.assertEqual(json.loads(saved)["extraccion_estado"], "incompleto")

    def test_incomplete_row_before_valid_row_does_not_block_lazy_ai_client(self):
        bad, good = f.detail(1, description="Compra Menor"), f.detail(2)
        self.source([bad, good])
        combiner.run(self.cfg)
        # Force the invalid record first, independent of SHA ordering.
        current = {r["proceso_id"]: r for r in self.master()}
        common.write_csv(common.data_path(self.cfg, "maestro.csv"), common.MASTER_FIELDS,
                         [current[bad["proceso_id"]], current[good["proceso_id"]]])
        ai = f.FakeAI()
        ai.close = lambda: None
        with patch.object(clasificador, "OpenAIClient", return_value=ai), patch.object(clasificador, "load_instructions", return_value="synthetic"):
            result = clasificador.run(self.cfg)
        self.assertEqual(ai.calls, [good["proceso_id"]])
        self.assertEqual(result["analyzed"], 1)
        self.assertEqual(result["errors"], 1)

    def test_historical_empty_master_does_not_block_current_delivery(self):
        bad, good = f.detail(1, description=""), f.detail(2)
        self.source([bad], run_id="2026-10-20")
        combiner.run(self.cfg)
        self.source([good])
        combiner.run(self.cfg)
        result = clasificador.run(self.cfg, client=f.FakeAI())
        self.assertEqual(result["status"], "exitoso")
        self.assertEqual(result["current_errors"], 0)
        self.assertEqual(len(result["pendientes_historicos"]), 1)

    def test_incomplete_observation_preserves_old_specs_and_cached_analysis(self):
        good = f.detail(1)
        self.source([good]); combiner.run(self.cfg)
        ai = f.FakeAI()
        clasificador.run(self.cfg, client=ai)
        bad = f.detail(1, description="Compra Menor", complete=False)
        self.source([bad]); combiner.run(self.cfg)
        self.assertEqual(self.master()[0]["descripcion"], good["descripcion"])
        self.source([good]); combiner.run(self.cfg)
        clasificador.run(self.cfg, client=ai)
        self.assertEqual(len(ai.calls), 1)


class ListingParsingTests(unittest.TestCase):
    def test_real_pager_postback_is_parsed_and_used_for_the_next_page(self):
        html = '''<form><input type="hidden" name="__VIEWSTATE" value="fresh-viewstate">
        <table id="ctl00_cphCuerpo_gvResultados"><tr><th>Proceso</th></tr>
        <tr><td><a id="ctl02_hlGoTo" href="Proceso.aspx?id=1">Detalle</a></td></tr>
        <tr><td><a href="javascript:__doPostBack('ctl00$cphCuerpo$realPager','Page$2')">2</a>
        <a href="javascript:__doPostBack('ctl00$cphCuerpo$realPager','Page$Next')">Siguiente</a></td></tr>
        </table></form>'''
        page = scout.parse_results(html, "http://example.test/Busqueda.aspx", 1)
        self.assertTrue(page.has_next)
        self.assertEqual(page.pagination[0]["target"], "ctl00$cphCuerpo$realPager")
        self.assertEqual(page.pagination[0]["argument"], "Page$2")

        client = scout.HonduClient({"scout": {"search_url": "http://example.test/Busqueda.aspx"}})
        with patch.object(client, "html", return_value=html) as request:
            client.advance(page, 2)
        posted = request.call_args.args[1]
        self.assertEqual(posted["__EVENTTARGET"], "ctl00$cphCuerpo$realPager")
        self.assertEqual(posted["__EVENTARGUMENT"], "Page$2")
        self.assertEqual(posted["__VIEWSTATE"], "fresh-viewstate")
        client.close()

    def test_live_period_cell_uses_fecha_cierre_and_keeps_unknown_safe(self):
        html = '''<form><table id="ctl00_cphCuerpo_gvResultados"><tr><th>Proceso</th>
        <th>Perido de Vigencia</th></tr><tr><td><a id="ctl02_hlGoTo" href="Proceso.aspx?id=1">Detalle</a></td>
        <td>Fecha Inicio: 01/06/2026 Fecha Cierre: 31/12/2026</td></tr>
        <tr><td><a id="ctl03_hlGoTo" href="Proceso.aspx?id=2">Detalle</a></td>
        <td>Fecha Inicio: 01/06/2026 Fecha Cierre: por confirmar</td></tr></table></form>'''
        page = scout.parse_results(html, "http://example.test/Busqueda.aspx", 1)
        self.assertEqual(common.parse_date(page.closing_dates[page.links[0]]).month, 12)
        self.assertEqual(page.closing_dates[page.links[1]], "")

    def test_closing_column_is_used_instead_of_publication_date(self):
        html = '''<form><table id="ctl00_cphCuerpo_gvResultados"><tr><th>Proceso</th>
        <th>Fecha Publicación</th><th>Fecha Recepción Ofertas</th></tr>
        <tr><td><a id="ctl02_hlGoTo" href="Proceso.aspx?id=1">Detalle</a></td>
        <td>01/10/2026</td><td>24/10/2026 08:00 a.m.</td></tr></table></form>'''
        page = scout.parse_results(html, "http://example.test/Busqueda.aspx", 1)
        self.assertEqual(common.parse_date(page.closing_dates[page.links[0]]).day, 24)

    def test_generic_description_is_incomplete_and_grid_presence_is_explicit(self):
        base = '''<table id="ctl00_cphCuerpo_dvProceso">
        <tr><td>Entidad</td><td>Hospital</td></tr><tr><td>Expediente</td><td>ABC</td></tr>
        <tr><td>Fecha Recepción Ofertas</td><td>24/10/2026</td></tr>
        <tr><td>Descripción Modalidad</td><td>Compra Menor</td></tr></table>'''
        absent = scout.parse_detail(base, "http://example.test/1", "test")
        self.assertEqual(absent["productos_estado"], "ausente")
        self.assertEqual(absent["extraccion_estado"], "incompleto")
        empty = scout.parse_detail(base + '<table id="ctl00_cphCuerpo_WebTab1_tmpl0_wdgProductos"><tr><td>No hay registros</td></tr></table>', "http://example.test/1", "test")
        self.assertEqual(empty["productos_estado"], "vacia")
        self.assertEqual(empty["extraccion_estado"], "incompleto")


if __name__ == "__main__":
    unittest.main()
