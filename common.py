"""Shared persistence and configuration. CSV is the public source of process data."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent
HN = timezone(timedelta(hours=-6), "America/Tegucigalpa")
RAW_FIELDS = ["run_id", "proceso_id", "url", "entidad", "expediente",
              "fecha_recepcion", "fecha_recepcion_original", "descripcion",
              "items_fuente", "extraido_en", "extraccion_estado"]
ANALYSIS_FIELDS = ["puntaje", "rubro", "resumen", "items", "hash_analizado",
                   "analisis_estado", "analizado_en", "analisis_modelo", "analisis_error"]
MASTER_FIELDS = RAW_FIELDS + ["contenido_hash", "primera_deteccion", "ultima_deteccion",
                             "ultima_modificacion"] + ANALYSIS_FIELDS
LEGACY_MASTER_FIELDS = ["resumen_corto" if k == "resumen" else k for k in MASTER_FIELDS]


def now():
    return datetime.now(HN)


def stamp():
    return now().isoformat(timespec="seconds")


def normalize(value):
    return " ".join(unicodedata.normalize("NFC", value or "").split())


def plain(value):
    return "".join(c for c in unicodedata.normalize("NFD", normalize(value).lower())
                   if unicodedata.category(c) != "Mn")


def parse_date(value):
    value = normalize(value)
    if not value:
        return None
    try:
        if re.match(r"^\d{1,2}/", value):
            return _local_date(value)
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=HN) if result.tzinfo is None else result.astimezone(HN)
    except ValueError:
        return None


def _local_date(value):
    value = re.sub(r"([ap])\s*\.?\s*m\s*\.?", lambda m: m[1].upper() + "M",
                   value, flags=re.I)
    for fmt in ("%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M %p",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=HN)
        except ValueError:
            pass
    raise ValueError("Fecha desconocida")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def content_hash(row):
    # Keep the time in the version hash, although expiry uses only the local day.
    date_value = parse_date(row.get("fecha_recepcion") or row.get("fecha_recepcion_original"))
    return digest([date_value.isoformat() if date_value else normalize(row.get("fecha_recepcion_original")),
                   normalize(row.get("descripcion"))])


GENERIC_DESCRIPTIONS = {
    "compra menor", "licitacion publica", "licitacion privada", "contratacion directa",
    "suministro de bienes y/o servicios", "suministro de bienes y servicios",
    "sin descripcion", "no aplica", "n/a", "ninguna", "ninguno", "descripcion",
    "especificaciones", "no existen registros", "no hay registros", "sin productos",
}


def useful_description(value):
    return any(plain(line).strip(" .:-") not in GENERIC_DESCRIPTIONS
               and any(char.isalpha() for char in line)
               for line in (value or "").splitlines() if normalize(line))


def detail_complete(row):
    return bool(row.get("extraccion_estado") == "completo"
                and all(normalize(row.get(k)) for k in ("entidad", "expediente"))
                and parse_date(row.get("fecha_recepcion"))
                and useful_description(row.get("descripcion")))


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


@contextmanager
def atomic_text(path, encoding="utf-8"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    with atomic_text(path) as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def read_json(path, default=None):
    try:
        with Path(path).open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def numbered_items(items):
    return "\n".join(f"{index}. {normalize(item)}" for index, item in enumerate(items, 1))


def current_master_schema(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return next(csv.reader(f), None) == MASTER_FIELDS


def rows(path, expected=None):
    csv.field_size_limit(32 * 1024 * 1024)
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV sin encabezado: {path}")
        legacy = expected == MASTER_FIELDS and reader.fieldnames == LEGACY_MASTER_FIELDS
        if expected is not None and reader.fieldnames != expected and not legacy:
            raise ValueError(f"Columnas inesperadas en {path}")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Fila CSV incompleta o mal formada en {path}")
            if legacy:
                row["resumen"] = row.pop("resumen_corto")
                if row.get("items"):
                    items = json.loads(row["items"])
                    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                        raise ValueError("Items del maestro anterior inválidos")
                    row["items"] = numbered_items(items)
            yield row


def write_csv(path, fields, values):
    with atomic_text(path, "utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        for row in values:
            writer.writerow({k: row.get(k, "") for k in fields})


def load_config(path=None):
    path = Path(path or ROOT / "config.json").resolve()
    cfg = read_json(path)
    if cfg is None:
        raise ValueError(f"Falta configuración: {path}. Copia config.example.json a config.json.")
    cfg["_config_dir"] = str(path.parent)
    data = Path(cfg.get("data_dir", "data"))
    cfg["_data"] = str(data if data.is_absolute() else path.parent / data)
    if cfg.get("start_hour") is None:
        raise ValueError("Configura start_hour antes de programar la ejecución.")
    if not (0 <= cfg["start_hour"] < 24 and 0 <= cfg.get("end_hour", 13) < 24
            and cfg["start_hour"] != cfg.get("end_hour", 13)):
        raise ValueError("Ventana horaria inválida")
    s = cfg["scout"]
    if s.get("modalidad") is None or s.get("tipo") is None:
        raise ValueError("Confirma modalidad y tipo en config.json; una cadena vacía incluye todos.")
    if s.get("request_attempts", 3) < 1 or s.get("expired_streak", 10) < 1:
        raise ValueError("request_attempts y expired_streak deben ser positivos")
    for key in ("invocation_budget_seconds", "discovery_budget_seconds", "detail_retry_max_seconds",
                "cooldown_max_seconds", "audit_every_days"):
        if key in s and s[key] <= 0:
            raise ValueError(key + " debe ser positivo")
    for key in ("historical_budget_seconds", "historical_attempts_per_invocation", "detail_retry_seconds",
                "cooldown_seconds", "session_rebuilds_per_invocation", "audit_pages"):
        if key in s and s[key] < 0:
            raise ValueError(key + " no puede ser negativo")
    if cfg.get("retention_days", 30) < 1:
        raise ValueError("retention_days debe ser positivo")
    Path(cfg["_data"]).mkdir(parents=True, exist_ok=True)
    return cfg


def data_path(cfg, name):
    return Path(cfg["_data"]) / name


def configure_logging(cfg):
    logger = logging.getLogger("bastion")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        class HondurasFormatter(logging.Formatter):
            def formatTime(self, record, datefmt=None):
                return datetime.fromtimestamp(record.created, HN).isoformat(timespec="seconds")

        formatter = HondurasFormatter("%(asctime)s %(levelname)s %(message)s")
        handler = RotatingFileHandler(data_path(cfg, "bastion.log"), maxBytes=2_000_000,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)
    return logger


class BusyError(RuntimeError):
    pass


@contextmanager
def pipeline_lock(cfg):
    # An OS lock is released even after SIGKILL; never delete the lock file.
    with data_path(cfg, "pipeline.lock").open("a+b") as f:
        if os.fstat(f.fileno()).st_size == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BusyError("Ya hay una ejecución activa") from exc
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def database(cfg):
    db = sqlite3.connect(data_path(cfg, "control.sqlite3"))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA cache_size=-2048")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS lifecycle (
            proceso_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL,
            retired_hash TEXT, retired_reason TEXT);
        CREATE TABLE IF NOT EXISTS analysis (
            proceso_id TEXT NOT NULL, content_hash TEXT NOT NULL,
            result TEXT NOT NULL, analyzed_at TEXT NOT NULL, model TEXT NOT NULL,
            PRIMARY KEY (proceso_id, content_hash));
        CREATE TABLE IF NOT EXISTS scan (
            run_id TEXT NOT NULL, url TEXT NOT NULL, outcome TEXT NOT NULL,
            data TEXT, PRIMARY KEY (run_id, url));
        CREATE TABLE IF NOT EXISTS scout_round (
            run_id TEXT PRIMARY KEY, scope TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scout_page (
            run_id TEXT NOT NULL, page INTEGER NOT NULL, generation INTEGER NOT NULL,
            signature TEXT NOT NULL, links TEXT NOT NULL, has_next INTEGER NOT NULL, dates TEXT NOT NULL,
            processed_at TEXT NOT NULL, pagination TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (run_id,page));
        CREATE TABLE IF NOT EXISTS scout_queue (
            run_id TEXT NOT NULL, url TEXT NOT NULL, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_retry TEXT, last_error TEXT,
            source TEXT NOT NULL, discovered_at TEXT NOT NULL, first_page INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0, resolved INTEGER NOT NULL DEFAULT 0,
            products_state TEXT, PRIMARY KEY (run_id,url));
        CREATE INDEX IF NOT EXISTS scout_queue_due ON scout_queue(run_id,resolved,next_retry);
    """)
    page_columns = {row[1] for row in db.execute("PRAGMA table_info(scout_page)")}
    if "pagination" not in page_columns:
        db.execute("ALTER TABLE scout_page ADD COLUMN pagination TEXT NOT NULL DEFAULT '[]'")
    db.commit()
    try:
        yield db
    finally:
        db.close()


def daily_counts(cfg, day=None):
    """Count distinct processes extracted and analyzed on a Honduras calendar day."""
    day = day or now().date().isoformat()
    extracted = set()
    complete = set()
    incomplete = set()
    analyzed = set()
    with database(cfg) as db:
        for record in db.execute("SELECT data FROM scan WHERE data IS NOT NULL"):
            try:
                row = json.loads(record["data"])
            except (TypeError, json.JSONDecodeError):
                continue
            process_id = row.get("proceso_id")
            if process_id and str(row.get("extraido_en", ""))[:10] == day:
                extracted.add(process_id)
                target = complete if row.get("extraccion_estado") == "completo" else incomplete
                target.add(process_id)
        for record in db.execute("SELECT proceso_id FROM analysis WHERE substr(analyzed_at,1,10)=?", (day,)):
            analyzed.add(record["proceso_id"])
    return {
        "fecha_honduras": day,
        "procesos_extraidos": len(extracted),
        "detalles_completos": len(complete),
        "detalles_incompletos": len(incomplete - complete),
        "procesos_analizados": len(analyzed),
    }


class DeadlineReached(RuntimeError):
    pass


def check_deadline(deadline):
    if deadline is not None and now() >= deadline:
        raise DeadlineReached("Se alcanzó el cierre de la ventana diaria")


def pause(seconds, deadline=None):
    check_deadline(deadline)
    if deadline is not None:
        seconds = min(seconds, max(0, (deadline - now()).total_seconds()))
    time.sleep(max(0, seconds))
    check_deadline(deadline)


def started(stage, run_id):
    return {"stage": stage, "run_id": run_id, "status": "ejecutando",
            "started_at": stamp(), "finished_at": None, "reason": None}


def finish(cfg, state, status, reason, **extra):
    state.update(status=status, reason=reason, finished_at=stamp(), **extra)
    write_json(data_path(cfg, state["stage"] + ".json"), state)
    return state


def cli(stage):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        configure_logging(cfg)
        with pipeline_lock(cfg):
            result = stage(cfg)
        return 0 if result["status"] == "exitoso" else 1
    except BusyError as exc:
        print(exc)
        return 75
    except Exception as exc:
        print(f"Error: {exc}")
        return 1
