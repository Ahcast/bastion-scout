"""Offline workload: parse, journal, combine and classify 600 processes; no API calls."""
from __future__ import annotations

import ctypes
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clasificador
import combiner
import common
import scout


def peak_rss_mb():
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        values = Counters()
        values.cb = ctypes.sizeof(values)
        get_handle = ctypes.windll.kernel32.GetCurrentProcess
        get_handle.restype = wintypes.HANDLE
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        if not get_memory(get_handle(), ctypes.byref(values), values.cb):
            raise ctypes.WinError()
        return round(values.PeakWorkingSetSize / 1024 ** 2, 2)
    import resource
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 2 if sys.platform == "darwin" else 1024), 2)


class LocalAI:
    def __init__(self):
        self.calls = 0

    def classify(self, row):
        self.calls += 1
        return {"puntaje": 80, "rubro": "Instrumentación", "resumen": "Compra de instrumentos",
                "items": [f"Item {i}: instrumento modelo ABC-{i}, cantidad 20" for i in range(20)]}


def main():
    began = time.monotonic()
    tracemalloc.start()
    with tempfile.TemporaryDirectory(dir=common.ROOT / "tests") as directory:
        cfg = json.loads((common.ROOT / "config.example.json").read_text())
        cfg.update(_data=directory, _config_dir=directory)
        run_id = common.now().date().isoformat()
        date_text = (common.now() + timedelta(days=5)).strftime("%d/%m/%Y 01:00:00 p.m.")
        product = "Especificación técnica completa, marca ABC, cantidad solicitada y requisitos. " * 5
        with common.database(cfg) as db:
            for number in range(600):
                items = "".join(f"<tr><td>{i}</td><td>20</td><td>Modelo ABC-{i}: {product}</td><td>Unidad</td></tr>" for i in range(20))
                html = f'''<table id="ctl00_cphCuerpo_dvProceso"><tr><td>Entidad</td><td>Hospital</td></tr>
                <tr><td>Expediente</td><td>EXP-{number}</td></tr><tr><td>Fecha Recepción Ofertas</td><td>{date_text}</td></tr></table>
                <table id="ctl00_cphCuerpo_WebTab1_tmpl0_wdgProductos">{items}</table>'''
                row = scout.parse_detail(html, f"http://example.test/?id={number}", run_id)
                db.execute("INSERT INTO scan VALUES (?,?,?,?)", (run_id, row["url"], "ok", json.dumps(row)))
                db.commit()
            scout.export_raw(cfg, db, run_id, common.now().date())
        common.write_json(common.data_path(cfg, "scout.json"), {
            "run_id": run_id, "status": "exitoso", "raw_sha256": common.file_hash(common.data_path(cfg, "raw.csv"))})
        merged = combiner.run(cfg, run_id)
        assert merged["status"] == "exitoso", merged
        ai = LocalAI()
        classified = clasificador.run(cfg, run_id, client=ai)
        assert classified["status"] == "exitoso", classified
        clasificador.run(cfg, run_id, client=ai)
        assert ai.calls == 600, ai.calls
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        report = {"processes": 600, "items_per_process": 20, "simulated_ai_calls": ai.calls,
                  "calls_on_second_pass": 0, "peak_python_mb": round(peak / 1024 ** 2, 2),
                  "peak_process_working_set_mb": peak_rss_mb(),
                  "seconds": round(time.monotonic() - began, 2)}
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
