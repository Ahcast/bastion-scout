"""Durable discovery ledger and detail queue. No ASP.NET form state is persisted."""
from __future__ import annotations

from datetime import timedelta
import json
import common as c

from common import detail_complete, digest, parse_date, stamp


class ScanStore:
    def __init__(self, db, run_id, scope):
        self.db, self.run_id = db, run_id
        saved = db.execute("SELECT * FROM scout_round WHERE run_id=?", (run_id,)).fetchone()
        self.progress = json.loads(saved["data"]) if saved else {}
        if saved and saved["scope"] != digest(scope):
            # Retain observations and queued URLs; replay discovery with the new filters.
            self.progress = {"generation": self.progress.get("generation", 1) + 1}
        self.scope = scope
        self.progress["filters"] = scope
        self.progress.setdefault("generation", 1)
        self.progress.setdefault("next_page", 1)
        self.progress.setdefault("discovery_complete", False)
        self.progress.setdefault("early_stop_disabled", False)
        self.progress.setdefault("session_rebuilds", 0)
        self.progress.setdefault("discovery_failures", 0)
        self.progress.setdefault("extraction_failures", 0)
        self.migrate_journal()
        self.persist()

    def persist(self):
        self.db.execute("INSERT OR REPLACE INTO scout_round VALUES (?,?,?)",
                        (self.run_id, digest(self.scope), json.dumps(self.progress, ensure_ascii=False)))
        self.db.commit()

    def migrate_journal(self):
        """Import old four-column journals without discarding cached detail data."""
        for saved in self.db.execute("SELECT run_id,url,outcome,data FROM scan"):
            row = json.loads(saved["data"]) if saved["data"] else None
            complete = bool(row and detail_complete(row))
            d = parse_date(row.get("fecha_recepcion")) if row else None
            expired = bool(d and d.date() < c.now().date())
            if row and not complete and row.get("extraccion_estado") == "completo":
                row["extraccion_estado"] = "incompleto"
                self.db.execute("UPDATE scan SET outcome='incompleto',data=? WHERE run_id=? AND url=?",
                                (json.dumps(row, ensure_ascii=False), saved["run_id"], saved["url"]))
            self.db.execute("""INSERT OR IGNORE INTO scout_queue
                (run_id,url,state,source,discovered_at,resolved,products_state)
                VALUES (?,?,?,?,?,?,?)""",
                (saved["run_id"], saved["url"], "completo" if complete else "incompleto" if row else "error",
                 "listing", row.get("extraido_en") or stamp() if row else stamp(),
                 int(complete or expired), row.get("productos_estado", "desconocida") if row else None))
        # A later successful observation can resolve a legacy failed copy of the same URL.
        self.db.execute("""UPDATE scout_queue SET resolved=1 WHERE resolved=0 AND EXISTS (
            SELECT 1 FROM scout_queue newer WHERE newer.url=scout_queue.url
            AND newer.run_id>scout_queue.run_id AND newer.state='completo')""")
        self.db.commit()

    def enqueue(self, url, source="known", page=0, position=0):
        self.db.execute("""INSERT OR IGNORE INTO scout_queue
            (run_id,url,state,source,discovered_at,first_page,position)
            VALUES (?,?,'pendiente',?,?,?,?)""",
            (self.run_id, url, source, stamp(), page, position))
        if source == "listing":
            self.db.execute("""UPDATE scout_queue SET source='listing',first_page=?,position=?
                WHERE run_id=? AND url=? AND source<>'listing'""", (page, position, self.run_id, url))

    def capture(self, page, number):
        """Commit EVERY URL and the page checkpoint together, before fetching details."""
        with self.db:
            self.db.execute("""INSERT OR REPLACE INTO scout_page
                (run_id,page,generation,signature,links,has_next,dates,processed_at,pagination)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (self.run_id, number, self.progress["generation"], digest(page.links),
                 json.dumps(page.links), int(page.has_next), json.dumps(page.closing_dates), stamp(),
                 json.dumps(page.pagination, ensure_ascii=False)))
            for position, url in enumerate(page.links):
                closing = parse_date(page.closing_dates.get(url))
                if closing is None or closing.date() >= c.now().date():
                    self.enqueue(url, "listing", number, position)
            self.progress["next_page"] = number + 1
            self.db.execute("INSERT OR REPLACE INTO scout_round VALUES (?,?,?)",
                            (self.run_id, digest(self.scope), json.dumps(self.progress, ensure_ascii=False)))

    def cached(self, url):
        result = self.db.execute("SELECT data FROM scan WHERE run_id=? AND url=?",
                                 (self.run_id, url)).fetchone()
        return json.loads(result["data"]) if result and result["data"] else None

    def save(self, url, row, error, retry_seconds, original_run=None):
        original_run = original_run or self.run_id
        complete = bool(row and detail_complete(row))
        d = parse_date(row.get("fecha_recepcion")) if row else None
        expired = bool(d and d.date() < c.now().date())
        state = "completo" if complete else "incompleto" if row else "error"
        retry = None if complete or expired else (c.now() + timedelta(seconds=retry_seconds)).isoformat()
        with self.db:
            # Failures retain the last observation. They never erase a saved detail.
            previous = self.cached(url)
            data = row or previous
            if data:
                data["run_id"] = self.run_id
            self.db.execute("INSERT OR REPLACE INTO scan VALUES (?,?,?,?)",
                            (self.run_id, url, "ok" if complete else state,
                             json.dumps(data, ensure_ascii=False) if data else None))
            if original_run != self.run_id:
                self.enqueue(url, "historical")
            self.db.execute("""UPDATE scout_queue SET state=?,attempts=attempts+1,
                next_retry=?,last_error=?,resolved=?,products_state=? WHERE run_id=? AND url=?""",
                (state, retry, error, int(complete or expired),
                 row.get("productos_estado", "desconocida") if row else None, self.run_id, url))
            if original_run != self.run_id:
                self.db.execute("""UPDATE scout_queue SET state=?,attempts=attempts+1,
                    next_retry=?,last_error=?,resolved=? WHERE run_id=? AND url=?""",
                    (state, retry, error, int(complete or expired), original_run, url))
            if complete or expired:
                self.db.execute("UPDATE scout_queue SET resolved=1 WHERE url=? AND run_id<>?",
                                (url, self.run_id))

    def due(self, historical=False):
        if historical:
            return self.db.execute("""SELECT * FROM scout_queue q WHERE resolved=0
                AND (run_id<>? OR source='historical') AND (next_retry IS NULL OR next_retry<=?)
                AND NOT EXISTS (SELECT 1 FROM scout_queue c WHERE c.run_id=? AND c.url=q.url
                               AND c.source<>'historical')
                ORDER BY run_id DESC,attempts,first_page,position""", (self.run_id, stamp(), self.run_id))
        return self.db.execute("""SELECT * FROM scout_queue WHERE run_id=? AND resolved=0
            AND source<>'historical' AND (next_retry IS NULL OR next_retry<=?)
            ORDER BY CASE source WHEN 'listing' THEN 0 ELSE 1 END,first_page,position""",
            (self.run_id, stamp()))

    def pending(self, historical=False):
        condition = "(run_id<>? OR source='historical')" if historical else "run_id=? AND source<>'historical'"
        params = [self.run_id]
        if historical:
            condition += " AND NOT EXISTS (SELECT 1 FROM scout_queue c WHERE c.run_id=? AND c.url=scout_queue.url AND c.source<>'historical')"
            params.append(self.run_id)
        return [dict(r) for r in self.db.execute(
            "SELECT run_id,url,state,attempts,next_retry,last_error FROM scout_queue WHERE resolved=0 AND "
            + condition + " ORDER BY run_id DESC,url", params)]

    def page_records(self):
        return self.db.execute("SELECT * FROM scout_page WHERE run_id=? AND generation=? ORDER BY page",
                               (self.run_id, self.progress["generation"]))

    def coverage(self, settings):
        previous, streak, unknown, inversions, boundary, expired = None, 0, 0, 0, None, 0
        omissions, seen = [], set()
        for page in self.page_records():
            dates = json.loads(page["dates"])
            for url in json.loads(page["links"]):
                if url in seen:
                    continue
                seen.add(url)
                d = parse_date(dates.get(url))
                if d is None:
                    unknown += 1
                    streak = 0
                    continue
                day = d.date()
                if previous and day > previous:
                    inversions += 1
                previous = day
                if day < c.now().date():
                    expired += 1
                    streak += 1
                    if streak >= settings.get("expired_streak", 10) and boundary is None:
                        boundary = page["page"]
                else:
                    streak = 0
                    if boundary is not None:
                        omissions.append(url)
        if unknown or inversions or omissions:
            self.progress["early_stop_disabled"] = True
        audit_due = c.now().date().toordinal() % max(1, settings.get("audit_every_days", 1)) == 0
        self.progress.update(unknown_dates=unknown, date_inversions=inversions,
                             audit_omissions=len(omissions), audit_omission_examples=omissions[:10],
                             heuristic_boundary_page=boundary, audit_due=audit_due,
                             consecutive_expired=streak, expired_listed=expired)
        # Full listing coverage is the default. Keep the heuristic only as an
        # explicit emergency override; a run must otherwise reach the final page.
        can_stop = (settings.get("allow_heuristic_early_stop", False)
                    and settings.get("early_stop_enabled", True) and boundary is not None
                    and not self.progress["early_stop_disabled"])
        audit_pages = settings.get("audit_pages", 3) if audit_due else 0
        if can_stop and self.progress.get("coverage") != "fin_resultados" and self.progress["next_page"] > boundary + audit_pages:
            self.progress.update(discovery_complete=True, stop_reason="heuristica_10_vencidos_auditada"
                                 if audit_due else "heuristica_10_vencidos", coverage="heuristica_no_garantizada")
        self.persist()
