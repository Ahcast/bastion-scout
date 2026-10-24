"""Independent, checkpointed discovery and extraction phases for Scout."""
from __future__ import annotations

from datetime import timedelta
import json
import time

import scout as s
from scout_store import ScanStore


def run(cfg, run_id=None, deadline=None, client=None):
    run_id = run_id or s.now().date().isoformat()
    state = s.started("scout", run_id)
    settings = cfg["scout"]
    state.update(scanner_version=s.VERSION, pages=0, extracted=0, expired=0, pending=[], detail_errors={},
                 scope={"modalidad": settings["modalidad"], "tipo": settings["tipo"]})
    s.write_json(s.data_path(cfg, "scout.json"), state)
    invocation_end = s.now() + timedelta(seconds=settings.get("invocation_budget_seconds", 2700))
    deadline = min(deadline, invocation_end) if deadline else invocation_end
    own_client = client is None
    client = client or s.HonduClient(cfg, deadline)
    today = s.now().date()
    state["cutoff_date"] = (today - timedelta(days=1)).isoformat()
    attempted_urls = set()
    error = None
    with s.database(cfg) as db:
        store = ScanStore(db, run_id, state["scope"])
        progress = store.progress
        master = s.data_path(cfg, "maestro.csv")
        if master.exists():
            for known in s.rows(master):
                # An inherited empty record is historical. Useful known records
                # must be refreshed even if pagination fails before reaching them.
                closing = s.parse_date(known.get("fecha_recepcion"))
                if known.get("url") and closing and closing.date() >= today:
                    store.enqueue(s.canonical_url(known["url"]), "known")
        db.commit()

        def phase_ready(phase):
            until = s.parse_date(progress.get(phase + "_paused_until"))
            return until is None or s.now() >= until

        def phase_failed(phase, message):
            key = phase + "_failures"
            progress[key] = progress.get(key, 0) + 1
            seconds = min(settings.get("cooldown_max_seconds", 900),
                          settings.get("cooldown_seconds", 60) * 2 ** min(progress[key] - 1, 8))
            progress[phase + "_paused_until"] = (s.now() + timedelta(seconds=seconds)).isoformat()
            progress[phase + "_error"] = message
            store.persist()

        def extract(historical=False):
            if not phase_ready("extraction"):
                return
            began, history_attempts, failures = time.monotonic(), 0, 0
            budget = settings.get("historical_budget_seconds", 60)
            limit = settings.get("historical_attempts_per_invocation", 2)
            for queued in store.due(historical):
                url = queued["url"]
                if url in attempted_urls:
                    continue
                if historical and (history_attempts >= limit or time.monotonic() - began >= budget):
                    break
                s.check_deadline(deadline)
                attempted_urls.add(url)
                history_attempts += int(historical)
                retry = min(settings.get("detail_retry_max_seconds", 3600),
                            settings.get("detail_retry_seconds", 60) * 2 ** min(queued["attempts"], 6))
                try:
                    detail_deadline = min(deadline, s.now() + timedelta(seconds=max(0, budget - (time.monotonic() - began)))) if historical else deadline
                    prior_deadline = getattr(client, "deadline", None)
                    if hasattr(client, "deadline"):
                        client.deadline = detail_deadline
                    try:
                        row = client.detail(url, run_id)
                    finally:
                        if hasattr(client, "deadline"):
                            client.deadline = prior_deadline
                    complete = s.detail_complete(row)
                    if not complete:
                        row["extraccion_estado"] = "incompleto"
                    message = None if complete else "Detalle sin campos obligatorios o contenido útil; grilla=" + row.get("productos_estado", "desconocida")
                    store.save(url, row, message, retry, queued["run_id"])
                    failures = 0
                    progress["extraction_failures"] = 0
                    progress.pop("extraction_paused_until", None)
                except s.DeadlineReached:
                    if historical and s.now() < deadline:
                        break
                    raise
                except Exception as exc:
                    store.save(url, None, str(exc), retry, queued["run_id"])
                    s.LOG.warning("Detalle pendiente %s: %s", url, type(exc).__name__)
                    failures += 1
                    if failures >= settings.get("consecutive_failures_limit", 3):
                        phase_failed("extraction", "Fallos consecutivos de detalles")
                        break
            store.persist()

        def capture_first(first):
            old = db.execute("SELECT signature FROM scout_page WHERE run_id=? AND page=1 AND generation=?",
                             (run_id, progress["generation"])).fetchone()
            cursor = progress["next_page"]
            if old and old["signature"] != s.digest(first.links):
                progress["generation"] += 1
                progress["early_stop_disabled"] = True
                progress["listing_changed"] = True
                cursor = 2
            store.capture(first, 1)
            progress["next_page"] = max(2, cursor)
            store.persist()

        def capture_page(page, number):
            signature = s.digest(page.links)
            repeated = db.execute("""SELECT 1 FROM scout_page WHERE run_id=? AND generation=?
                AND page<>? AND signature=?""", (run_id, progress["generation"], number, signature)).fetchone()
            if page.links and repeated:
                raise RuntimeError("Paginación repetida: el sitio no avanzó de página")
            store.capture(page, number)

        def after_page(page, number):
            store.coverage(settings)
            if not page.has_next:
                progress.update(discovery_complete=True, stop_reason="fin_resultados_verificado", coverage="fin_resultados")
            store.persist()
            state.update(pages=number, next_page=progress["next_page"],
                         discovery_complete=progress["discovery_complete"])
            s.export_raw(cfg, db, run_id, today)
            s.write_json(s.data_path(cfg, "scout.json"), state)
            s.LOG.info("Scout ronda %s, página %s; cola durable, vencidos %s, inversiones %s",
                       run_id, number, progress.get("consecutive_expired", 0), progress.get("date_inversions", 0))

        try:
            if not progress["discovery_complete"] and phase_ready("discovery"):
                number, rebuilds = 1, 0
                remaining = max(0, (deadline - s.now()).total_seconds())
                discovery_deadline = min(deadline, s.now() + timedelta(
                    seconds=min(settings.get("discovery_budget_seconds", 900), remaining * .4)))
                prior_deadline = getattr(client, "deadline", None)
                if hasattr(client, "deadline"):
                    client.deadline = discovery_deadline
                try:
                    try:
                        page = client.search()
                    except s.DeadlineReached:
                        raise
                    except Exception:
                        if rebuilds >= settings.get("session_rebuilds_per_invocation", 1):
                            raise
                        rebuilds += 1
                        progress["session_rebuilds"] += 1
                        page = client.restart_search() if hasattr(client, "restart_search") else client.search()
                    capture_first(page)
                    after_page(page, 1)
                    while not progress["discovery_complete"]:
                        s.check_deadline(discovery_deadline)
                        number = progress["next_page"]
                        try:
                            page = client.advance(page, number)
                            capture_page(page, number)
                        except s.DeadlineReached:
                            raise
                        except Exception as exc:
                            if rebuilds >= settings.get("session_rebuilds_per_invocation", 1):
                                raise
                            status_code = getattr(client, "last_status_code", None)
                            if status_code in {429, 500, 502, 503, 504}:
                                retry_after = getattr(client, "last_retry_after_seconds", 0)
                                delay = max(settings.get("page_retry_cooldown_seconds", 180), retry_after)
                                delay = min(delay, settings.get("retry_after_max_seconds", 1800))
                                s.LOG.warning("HTTP %s en paginación; espera %s segundos antes de reconstruir",
                                              status_code, int(delay))
                                s.pause(delay, discovery_deadline)
                            rebuilds += 1
                            progress["session_rebuilds"] += 1
                            s.LOG.warning("Reconstruyendo búsqueda en página %s tras %s", number, type(exc).__name__)
                            page = client.restart_search() if hasattr(client, "restart_search") else client.search()
                            capture_first(page)
                            if not page.has_next:
                                after_page(page, 1)
                                continue
                            number = progress["next_page"]
                            page = client.advance(page, number)
                            capture_page(page, number)
                        after_page(page, number)
                    progress["discovery_failures"] = 0
                    progress.pop("discovery_paused_until", None)
                except s.DeadlineReached:
                    error = "presupuesto_descubrimiento"
                    phase_failed("discovery", error)
                except Exception as exc:
                    error = str(exc)
                    phase_failed("discovery", error)
                    s.LOG.warning("Descubrimiento pausado en página %s: %s; se procesa la cola guardada", number, error)
                finally:
                    if hasattr(client, "deadline"):
                        client.deadline = prior_deadline
            # A discovery failure never skips the durable detail queue.
            extract()
            extract(historical=True)
            store.coverage(settings)
        except s.DeadlineReached:
            error = "limite_horario_o_presupuesto_invocacion"
        finally:
            store.persist()
            s.export_raw(cfg, db, run_id, today)
            if own_client:
                client.close()

        pending = store.pending()
        historical = store.pending(historical=True)
        historical = list({entry["url"]: entry for entry in reversed(historical)}.values())
        state["pending"] = pending
        state["detail_errors"] = {entry["url"]: entry["last_error"] for entry in pending if entry["last_error"]}
        state["pendientes_historicos"] = historical
        state["ronda_actual_completa"] = bool(progress["discovery_complete"] and not pending)
        state["phases"] = {
            "discovery": {"status": "completo" if progress["discovery_complete"] else "pausado",
                          "next_page": progress["next_page"], "next_retry": progress.get("discovery_paused_until")},
            "extraction": {"status": "completo" if not pending else "pendiente", "pending": len(pending),
                           "next_retry": progress.get("extraction_paused_until")},
        }
        state["coverage"] = {key: progress.get(key) for key in (
            "coverage", "early_stop_disabled", "date_inversions", "unknown_dates", "heuristic_boundary_page",
            "audit_due", "audit_omissions", "audit_omission_examples", "session_rebuilds", "listing_changed")}
        state["pages"] = db.execute("SELECT count(*) FROM scout_page WHERE run_id=? AND generation=?",
                                    (run_id, progress["generation"])).fetchone()[0]
        state["consecutive_expired"] = progress.get("consecutive_expired", 0)
        state["known_rechecked"] = db.execute("SELECT count(*) FROM scout_queue WHERE run_id=? AND source='known' AND attempts>0",
                                             (run_id,)).fetchone()[0]
        state["products_grid"] = {r[0] or "desconocida": r[1] for r in db.execute(
            "SELECT products_state,count(*) FROM scout_queue WHERE run_id=? GROUP BY products_state", (run_id,))}
        for record in db.execute("SELECT data FROM scan WHERE run_id=? AND data IS NOT NULL", (run_id,)):
            row = json.loads(record["data"])
            d = s.parse_date(row.get("fecha_recepcion"))
            state["expired" if d and d.date() < today else "extracted"] += 1
        state["expired_listed"] = progress.get("expired_listed", 0)
        # Bounded successful history; unresolved URLs are retained until resolved.
        old = (today - timedelta(days=7)).isoformat()
        db.execute("""DELETE FROM scan WHERE run_id<? AND NOT EXISTS (
            SELECT 1 FROM scout_queue q WHERE q.run_id=scan.run_id AND q.url=scan.url AND q.resolved=0)""", (old,))
        db.execute("DELETE FROM scout_page WHERE run_id<?", (old,))
        db.execute("DELETE FROM scout_round WHERE run_id<?", (old,))
        db.execute("DELETE FROM scout_queue WHERE run_id<? AND resolved=1", (old,))
        db.commit()
        state.update(http_requests=getattr(client, "requests", 0), http_retries=getattr(client, "retries", 0))
        good = state["ronda_actual_completa"]
        reason = progress.get("stop_reason", "fases_pendientes") if good else "fases_pendientes"
        return s.finish(cfg, state, "exitoso" if good else "parcial", reason,
                        error=error, raw_sha256=s.file_hash(s.data_path(cfg, "raw.csv")))
