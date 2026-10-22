"""Hourly entry point: one daily cycle, starting 19:00 and retrying until 13:00."""
from __future__ import annotations

import argparse
from datetime import timedelta
import logging

import clasificador
import combiner
import scout
from common import (BusyError, check_deadline, configure_logging, current_master_schema, data_path,
                    file_hash, finish, load_config, now, pipeline_lock, read_json,
                    started, write_json)

LOG = logging.getLogger("bastion")


def daily_window(moment, start_hour=19, end_hour=13):
    start = moment.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if end_hour < start_hour:
        if moment.hour < end_hour:
            start -= timedelta(days=1)
        elif moment.hour < start_hour:
            return None
        end = (start + timedelta(days=1)).replace(hour=end_hour)
    else:
        if not start_hour <= moment.hour < end_hour:
            return None
        end = start.replace(hour=end_hour)
    return start.date().isoformat(), end


def recorded(cfg, name, run_id):
    result = read_json(data_path(cfg, name + ".json"), {})
    return result if result.get("run_id") == run_id else {}


def matching_file(cfg, name, expected):
    path = data_path(cfg, name)
    return bool(expected and path.exists() and file_hash(path) == expected)


def run(cfg):
    window = daily_window(now(), cfg["start_hour"], cfg["end_hour"])
    if window is None:
        LOG.info("Fuera de ventana; próxima extracción a las %02d:00 de Honduras", cfg["start_hour"])
        return {"status": "exitoso", "reason": "fuera_de_ventana", "skipped": True}
    run_id, deadline = window
    state = started("main", run_id)
    state.update(deadline=deadline.isoformat(), output_ready=False, stages={}, retry_stages=[])
    write_json(data_path(cfg, "main.json"), state)
    try:
        previous = read_json(data_path(cfg, "scout.json"), {})
        previous_combined = read_json(data_path(cfg, "combiner.json"), {})
        if previous.get("run_id") and previous["run_id"] != run_id and (
                previous.get("status") == "ejecutando"
                or previous_combined.get("status") != "exitoso"
                or previous_combined.get("run_id") != previous["run_id"]
                or previous_combined.get("raw_sha256") != previous.get("raw_sha256")
                or not data_path(cfg, "maestro.csv").exists()):
            LOG.info("Recuperando observaciones del ciclo anterior antes de crear el nuevo raw")
            scout.recover_raw(cfg, previous["run_id"])
            recovered = combiner.run(cfg, previous["run_id"])
            if recovered["status"] != "exitoso":
                state["retry_stages"].append("combiner")
                return finish(cfg, state, "error", "recuperacion_anterior_pendiente")
        scan = recorded(cfg, "scout", run_id)
        scope = {key: cfg["scout"][key] for key in ("modalidad", "tipo")}
        if not (scan.get("status") == "exitoso" and scan.get("scope") == scope
                and matching_file(cfg, "raw.csv", scan.get("raw_sha256"))):
            check_deadline(deadline)
            scan = scout.run(cfg, run_id, deadline)
        state["stages"]["scout"] = scan.get("status", "error")
        if scan.get("status") != "exitoso":
            state["retry_stages"].append("scout")
        write_json(data_path(cfg, "main.json"), state)

        combined = recorded(cfg, "combiner", run_id)
        master = data_path(cfg, "maestro.csv")
        if not (combined.get("status") == "exitoso" and master.exists() and current_master_schema(master)
                and combined.get("raw_sha256") == scan.get("raw_sha256")):
            # Local merge is allowed at cutoff: save every observation already collected.
            combined = combiner.run(cfg, run_id)
        state["stages"]["combiner"] = combined.get("status", "error")
        if combined.get("status") != "exitoso":
            state["retry_stages"].append("combiner")
            return finish(cfg, state, "parcial", "combinacion_pendiente")

        classified = recorded(cfg, "clasificador", run_id)
        if not (classified.get("status") == "exitoso"
                and matching_file(cfg, "maestro.csv", classified.get("maestro_sha256"))):
            classified = clasificador.run(cfg, run_id, deadline)
        state["stages"]["clasificador"] = classified.get("status", "error")
        if classified.get("status") != "exitoso":
            state["retry_stages"].append("clasificador")
        ready = not state["retry_stages"]
        state["output_ready"] = ready
        return finish(cfg, state, "exitoso" if ready else "parcial",
                      "ciclo_diario_completo" if ready else "etapas_pendientes",
                      maestro_sha256=file_hash(master), total=combined.get("total", 0))
    except Exception as exc:
        LOG.exception("Pipeline interrumpido; se conservan los archivos y checkpoints")
        return finish(cfg, state, "error", "error_pipeline", error=str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--check", action="store_true", help="Validar configuración sin consultar sitios ni IA")
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        configure_logging(cfg)
        if args.check:
            print("Configuración válida; horario Honduras %02d:00 a %02d:00; skill: %s" %
                  (cfg["start_hour"], cfg["end_hour"], cfg["ai"].get("skill_path") or "PENDIENTE"))
            return 0
        with pipeline_lock(cfg):
            result = run(cfg)
        return 0 if result["status"] == "exitoso" else 1
    except BusyError:
        LOG.info("Otra ejecución mantiene el bloqueo; no se inicia una segunda")
        return 75
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
