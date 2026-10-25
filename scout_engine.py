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
    invocation = dict(pages_read=0, last_attempted_page=None, last_saved_page=None,
                      last_failed_page=None, last_error=None, details_attempted=0,
                      extracted=0, incomplete=0, expired=0, failed=0, interrupted=0)
    state["invocation"] = invocation
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
                invocation["details_attempted"] += 1
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
                    closing = s.parse_date(row.get("fecha_recepcion"))
                    outcome = ("expired" if closing and closing.date() < today else
                               "extracted" if complete else "incomplete")
                    invocation[outcome] += 1
                    failures = 0
                    progress["extraction_failures"] = 0
                    progress.pop("extraction_paused_until", None)
                except s.DeadlineReached:
                    invocation["interrupted"] += 1
                    if historical and s.now() < deadline:
                        break
                    raise
                except Exception as exc:
                    invocation["failed"] += 1
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
            invocation["pages_read"] += 1
            invocation["last_saved_page"] = number
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

        def retry_listing_wait(failure_number, deadline, context):
            status_code = getattr(client, "last_status_code", None)
            retry_after = getattr(client, "last_retry_after_seconds", 0)
            base = settings.get("page_retry_cooldown_seconds", 180)
            cap = settings.get("page_retry_max_cooldown_seconds", 600)
            delay = min(cap, base * (2 ** max(0, failure_number - 1)))
            delay = min(settings.get("retry_after_max_seconds", 1800),
                        max(delay, retry_after))
            progress["page_retries"] = progress.get("page_retries", 0) + 1
            store.persist()
            s.LOG.warning("Pausa de %s segundos antes de reintentar %s (HTTP %s, intento %s/%s)",
                          int(delay), context, status_code or "sin respuesta", failure_number,
                          settings.get("page_retry_attempts", 3))
            s.pause(delay, deadline)

        def listing_search(restart=False):
            invocation["last_attempted_page"] = 1
            return client.restart_search() if restart and hasattr(client, "restart_search") else client.search()

        def listing_advance(page, number):
            invocation["last_attempted_page"] = number
            return client.advance(page, number)

        def new_search():
            return listing_search(restart=True)

        def replay_to_predecessor(target_page):
            """Rebuild fresh ASP.NET state and replay saved pages sequentially."""
            page = new_search()
            capture_first(page)
            after_page(page, 1)
            current_number = 1
            while current_number < target_page - 1 and not progress["discovery_complete"]:
                next_number = current_number + 1
                if not page.has_next:
                    progress.update(discovery_complete=True, stop_reason="fin_resultados_verificado",
                                    coverage="fin_resultados")
                    store.persist()
                    break
                page = listing_advance(page, next_number)
                capture_page(page, next_number)
                after_page(page, next_number)
                current_number = next_number
            return page, current_number

        try:
            if not progress["discovery_complete"] and phase_ready("discovery"):
                remaining = max(0, (deadline - s.now()).total_seconds())
                discovery_deadline = min(deadline, s.now() + timedelta(
                    seconds=min(settings.get("discovery_budget_seconds", 1500), remaining * .6)))
                prior_deadline = getattr(client, "deadline", None)
                if hasattr(client, "deadline"):
                    client.deadline = discovery_deadline
                try:
                    number = progress.get("next_page", 1)
                    failures = 0
                    page = None
                    current_number = 0
                    while not progress["discovery_complete"]:
                        s.check_deadline(discovery_deadline)
                        try:
                            if page is None:
                                if current_number == 0 and failures == 0:
                                    page = listing_search()
                                    capture_first(page)
                                    after_page(page, 1)
                                    current_number = 1
                                else:
                                    target = max(1, progress.get("next_page", number))
                                    if target > 1:
                                        page, current_number = replay_to_predecessor(target)
                                    else:
                                        page = new_search()
                                        capture_first(page)
                                        after_page(page, 1)
                                        current_number = 1
                            if progress["discovery_complete"]:
                                break
                            number = progress["next_page"]
                            # Rehydrate ViewState page by page after an hourly restart.
                            while current_number < number - 1:
                                if not page.has_next:
                                    progress.update(discovery_complete=True,
                                                    stop_reason="fin_resultados_verificado",
                                                    coverage="fin_resultados")
                                    store.persist()
                                    break
                                replay_number = current_number + 1
                                page = listing_advance(page, replay_number)
                                capture_page(page, replay_number)
                                after_page(page, replay_number)
                                current_number = replay_number
                            if progress["discovery_complete"]:
                                break
                            number = progress["next_page"]
                            page = listing_advance(page, number)
                            capture_page(page, number)
                            after_page(page, number)
                            current_number = number
                            failures = 0
                        except s.DeadlineReached:
                            raise
                        except Exception as exc:
                            invocation["last_failed_page"] = invocation["last_attempted_page"]
                            invocation["last_error"] = str(exc)
                            if failures >= settings.get("page_retry_attempts", 3):
                                raise
                            failures += 1
                            progress["session_rebuilds"] = progress.get("session_rebuilds", 0) + 1
                            retry_listing_wait(failures, discovery_deadline,
                                               f"búsqueda/página {number} tras {type(exc).__name__}")
                            # A new search and sequential replay avoid stale ViewState
                            # and unsupported jumps to a distant page.
                            page = None
                            current_number = 0
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
                          "next_page": progress["next_page"], "next_retry": progress.get("discovery_paused_until"),
                          "error": progress.get("discovery_error") if not progress["discovery_complete"] else None},
            "extraction": {"status": "completo" if not pending else "pendiente", "pending": len(pending),
                           "next_retry": progress.get("extraction_paused_until"),
                           "error": progress.get("extraction_error") if pending else None},
        }
        state["coverage"] = {key: progress.get(key) for key in (
            "coverage", "early_stop_disabled", "date_inversions", "unknown_dates", "heuristic_boundary_page",
            "audit_due", "audit_omissions", "audit_omission_examples", "session_rebuilds", "page_retries",
            "listing_changed")}
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
