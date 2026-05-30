#!/usr/bin/env python3
"""
vas_log.py — Funciones de logging compartidas entre vas.py y database.py.

Uso:
    from vas_log import log, log_debug, setup_logging

    setup_logging(level="normal", log_file="")

    log("[SCOPE] mensaje normal")       # visible con LOG_LEVEL=normal o debug
    log_debug("[SCOPE] detalle")        # solo visible con LOG_LEVEL=debug

Formato de salida:
    log("[SCOPE] msg")       → [VAS] [SCOPE] msg
    log_debug("[SCOPE] msg") → [VAS] [DEBUG] [SCOPE] msg

LOG_LEVEL:
    no     → silencio total (ni stdout ni fichero)
    normal → eventos importantes: arranque, registros, cambios de estado, errores
    debug  → además: detalles de config, heartbeat rutinario, consultas GET

LOG_FILE:
    vacío  → solo stdout (capturado por journald cuando corre como servicio)
    ruta   → además escribe en el archivo con timestamp ISO-8601 UTC como prefijo
"""
import datetime


_level    = "normal"  # "no" | "normal" | "debug"
_log_file = ""        # ruta al fichero adicional, o "" para solo stdout


def _write(msg: str) -> None:
    print(msg, flush=True)
    if _log_file:
        ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with open(_log_file, "a", encoding="utf-8") as f:
                f.write(f"{ts} {msg}\n")
        except OSError:
            pass


def log(msg: str) -> None:
    """Mensaje de nivel normal: visible con LOG_LEVEL=normal o debug."""
    if _level == "no":
        return
    _write(f"[VAS] {msg}")


def log_debug(msg: str) -> None:
    """Mensaje de nivel debug: solo visible con LOG_LEVEL=debug.
    Filtrar con: journalctl -u vas | grep '\\[DEBUG\\]'
    """
    if _level != "debug":
        return
    _write(f"[VAS] [DEBUG] {msg}")


def setup_logging(level: str = "normal", log_file: str = "") -> None:
    """
    Inicializa el nivel de log y el fichero opcional.

    level:    'no' | 'normal' | 'debug' (cualquier otro valor → 'normal')
    log_file: ruta al fichero adicional, o '' para solo stdout/journald.
    """
    global _level, _log_file

    _level    = level if level in {"no", "normal", "debug"} else "normal"
    _log_file = log_file
