"""Merge raw observations into the shared CSV using a disk-backed work table."""
from __future__ import annotations

import json
import logging

from common import (ANALYSIS_FIELDS, MASTER_FIELDS, RAW_FIELDS, cli, content_hash,
                    data_path, database, file_hash, finish, now, parse_date,
                    read_json, rows, stamp, started, write_csv, write_json)

LOG = logging.getLogger("bastion")


def run(cfg, run_id=None, deadline=None):
    scout = read_json(data_path(cfg, "scout.json"), {})
    run_id = run_id or scout.get("run_id") or now().date().isoformat()
    state = started("combiner", run_id)
    write_json(data_path(cfg, "combiner.json"), state)
    counts = dict(inserted=0, changed=0, unchanged=0, removed_expired=0,
                  removed_retention=0, suppressed_retired=0)
    try:
        raw = data_path(cfg, "raw.csv")
        raw_hash = file_hash(raw)
        if scout.get("run_id") != run_id or scout.get("raw_sha256") != raw_hash:
            raise ValueError("raw.csv no coincide con la ejecución registrada por scout")
        today = now().date()
        current = stamp()
        path = data_path(cfg, "maestro.csv")
        with database(cfg) as db:
            # SQLite temporary storage stays on disk; Python only holds individual rows.
            db.execute("PRAGMA temp_store=FILE")
            db.execute("CREATE TEMP TABLE work (id TEXT PRIMARY KEY, data TEXT NOT NULL)")

            def put(row):
                db.execute("INSERT OR REPLACE INTO work VALUES (?,?)",
                           (row["proceso_id"], json.dumps(row, ensure_ascii=False)))

            def lifecycle(row):
                first = row.get("primera_deteccion") or row.get("extraido_en") or current
                if parse_date(first) is None:
                    raise ValueError(f"Primera detección inválida: {row['proceso_id']}")
                db.execute("INSERT OR IGNORE INTO lifecycle(proceso_id,first_seen) VALUES (?,?)",
                           (row["proceso_id"], first))
                return db.execute("SELECT * FROM lifecycle WHERE proceso_id=?", (row["proceso_id"],)).fetchone()

            def remove_if_needed(row, life):
                d = parse_date(row.get("fecha_recepcion"))
                age = (today - parse_date(life["first_seen"]).date()).days
                reason = "expired" if d and d.date() < today else "retention" if age >= cfg.get("retention_days", 30) else None
                if reason:
                    db.execute("UPDATE lifecycle SET retired_hash=?,retired_reason=? WHERE proceso_id=?",
                               (row["contenido_hash"], reason, row["proceso_id"]))
                    counts["removed_" + reason] += 1
                    return True
                return False

            if path.exists():
                for old in rows(path, MASTER_FIELDS):
                    if not old.get("proceso_id") or not old.get("contenido_hash"):
                        raise ValueError("Maestro inválido; se conserva el archivo sin reemplazarlo")
                    life = lifecycle(old)
                    if not remove_if_needed(old, life):
                        put(old)

            for incoming in rows(raw, RAW_FIELDS):
                if incoming.get("run_id") != run_id or not incoming.get("proceso_id"):
                    raise ValueError("Fila raw sin identidad o perteneciente a otra ejecución")
                found = db.execute("SELECT data FROM work WHERE id=?", (incoming["proceso_id"],)).fetchone()
                old = json.loads(found["data"]) if found else None
                row = dict.fromkeys(MASTER_FIELDS, "")
                if old:
                    row.update(old)
                # Incomplete observations must never erase previously extracted data.
                for key in RAW_FIELDS:
                    if incoming.get(key) or not old:
                        row[key] = incoming.get(key, "")
                h = content_hash(row)
                row["contenido_hash"] = h
                life = lifecycle(row)
                if life["retired_hash"]:
                    if life["retired_hash"] == h:
                        counts["suppressed_retired"] += 1
                        continue
                    db.execute("UPDATE lifecycle SET first_seen=?,retired_hash=NULL,retired_reason=NULL WHERE proceso_id=?",
                               (current, row["proceso_id"]))
                    life = db.execute("SELECT * FROM lifecycle WHERE proceso_id=?", (row["proceso_id"],)).fetchone()
                row["primera_deteccion"] = life["first_seen"]
                row["ultima_deteccion"] = incoming.get("extraido_en") or current
                if remove_if_needed(row, life):
                    continue
                if old and old["contenido_hash"] == h:
                    counts["unchanged"] += 1
                else:
                    counts["changed" if old else "inserted"] += 1
                    row["ultima_modificacion"] = current
                    for key in ANALYSIS_FIELDS:
                        row[key] = ""
                    row["analisis_estado"] = "pendiente"
                put(row)

            db.commit()
            total = db.execute("SELECT count(*) FROM work").fetchone()[0]
            write_csv(path, MASTER_FIELDS, (json.loads(r["data"]) for r in db.execute("SELECT data FROM work ORDER BY id")))
        LOG.info("Combiner: %s procesos, %s", total, counts)
        return finish(cfg, state, "exitoso", "maestro_actualizado", raw_sha256=raw_hash,
                      maestro_sha256=file_hash(path), total=total, **counts)
    except Exception as exc:
        LOG.exception("Combiner falló")
        return finish(cfg, state, "error", "error_combinacion", error=str(exc), **counts)


if __name__ == "__main__":
    raise SystemExit(cli(run))
