"""Explicit manual smoke test: a search, one detail and optionally a second page."""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import configure_logging, data_path, load_config, now, pipeline_lock, stamp, write_json
from scout import HonduClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--second-page", action="store_true")
    parser.add_argument("--timeout-minutes", type=float, default=5)
    args = parser.parse_args()
    cfg = load_config(args.config)
    configure_logging(cfg)
    with pipeline_lock(cfg):
        client = HonduClient(cfg, now() + timedelta(minutes=args.timeout_minutes))
        result = {"started_at": stamp(), "status": "ejecutando", "step": "busqueda"}
        try:
            page = client.search()
            result.update(first_page_links=len(page.links), has_next=page.has_next, step="detalle")
            if page.links:
                row = client.detail(page.links[0], "manual_probe")
                result.update(expediente=row["expediente"], fecha_recepcion=row["fecha_recepcion"],
                              extraccion_estado=row["extraccion_estado"], description_chars=len(row["descripcion"]),
                              items=len(json.loads(row["items_fuente"])))
                print("Búsqueda y detalle recibidos; estado del detalle: " + row["extraccion_estado"], flush=True)
            if args.second_page and page.has_next:
                result["step"] = "segunda_pagina"
                second = client.advance(page, 2)
                if second.links == page.links:
                    raise ValueError("El sitio repitió la página al intentar avanzar")
                result["second_page_links"] = len(second.links)
            result.update(status="exitoso", step="completo")
        except Exception as exc:
            result.update(status="error", error=str(exc))
        finally:
            client.close()
            result["finished_at"] = stamp()
            write_json(data_path(cfg, "check_site.json"), result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "exitoso" else 1


if __name__ == "__main__":
    raise SystemExit(main())
