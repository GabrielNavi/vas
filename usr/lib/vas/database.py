#!/usr/bin/env python3
"""
database.py — Capa de persistencia de VAS.

Gestiona la base de datos SQLite de clientes y el fichero de versión.
Las rutas DB_PATH y VERSION_FILE son inyectadas por vas.py antes de
cualquier llamada a las funciones de este módulo.

Ciclo de vida de un cliente:
  active   → registrándose normalmente (last_seen reciente)
  inactive → sin heartbeat desde TTL_INACTIVE_DAYS días
  archived → sin heartbeat desde TTL_ARCHIVE_DAYS días (solo histórico)

Los consumidores normales (VAC, veyon-sync) solo ven clientes 'active'.
El paso a 'inactive' sube versión para que los consumidores lo detecten.
Un cliente inactivo vuelve a 'active' automáticamente al hacer heartbeat.
"""
import json
import sqlite3
import os
import subprocess
import datetime
from datetime import timezone

from vas_log import log, log_debug


def _utcnow() -> datetime.datetime:
    """Devuelve la hora UTC actual como datetime naive (compatible con CURRENT_TIMESTAMP de SQLite)."""
    return datetime.datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt_duration(secs: int) -> str:
    """Formatea segundos como cadena legible (30d, 12h, 90m, 60s)."""
    for unit, factor in (('d', 86400), ('h', 3600), ('m', 60)):
        if secs % factor == 0:
            return f"{secs // factor}{unit}"
    return f"{secs}s"

# Inyectadas por vas.py antes del primer uso
DB_PATH      = None
VERSION_FILE = None
HOOKS_DIR    = None
VAS_BASE_URL = None
HOOKS_LOG    = None


def get_connection():
    """Abre y devuelve una conexión SQLite a DB_PATH."""
    return sqlite3.connect(DB_PATH)


def init_db():
    """
    Inicializa el esquema de la base de datos y el fichero de versión.

    Crea la tabla 'clients' si no existe (idempotente).
    Migra la columna 'status' si la BD viene de una versión anterior.
    Crea VERSION_FILE con valor "0" si no existe.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id                 TEXT PRIMARY KEY,
            hostname           TEXT,
            ip                 TEXT,
            mac                TEXT,
            status             TEXT DEFAULT 'active',
            extra_imperative   TEXT,
            extra_informative  TEXT,
            last_seen          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migraciones: añadir columnas nuevas si la tabla viene de versiones anteriores
    cur.execute("PRAGMA table_info(clients)")
    cols = [row[1] for row in cur.fetchall()]
    for col, definition in [
        ("status",            "TEXT DEFAULT 'active'"),
        ("extra_imperative",  "TEXT"),
        ("extra_informative", "TEXT"),
    ]:
        if col not in cols:
            cur.execute(f"ALTER TABLE clients ADD COLUMN {col} {definition}")
            log(f"[DB] Migración: columna '{col}' añadida.")

    conn.commit()
    conn.close()
    log_debug(f"[DB] Base de datos lista: {DB_PATH}")

    # Inicializar fichero de versión
    version_dir = os.path.dirname(VERSION_FILE)
    if version_dir:
        os.makedirs(version_dir, exist_ok=True)

    if not os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, "w") as f:
            f.write("0")
        log(f"[DB] Fichero de versión inicializado: {VERSION_FILE} → 0")
    else:
        current = get_version()
        log(f"[DB] Versión actual al arrancar: {current}")


_EXTRA_CLEAR = "__clear__"  # Sentinel: borrado explícito del campo en VAS.

def _serialize_extra(extra: dict | None) -> str | None:
    """
    Serializa un dict de extra con semántica de tres estados:

      None  → None       — sin opinión; VAS conserva el valor existente (COALESCE).
      {}    → _EXTRA_CLEAR — borrado explícito; VAS pone el campo a NULL.
      {...} → JSON string  — actualización; VAS sobreescribe el campo.

    Orden de claves determinista para comparación estable entre ciclos.
    """
    if extra is None:
        return None
    if extra == {}:
        return _EXTRA_CLEAR
    return json.dumps(extra, sort_keys=True, separators=(",", ":"))


def client_has_changed(
    client_id: str,
    hostname: str,
    ip: str,
    mac: str,
    extra_imperative: dict | None = None,
) -> bool:
    """
    Comprueba si los datos enviados difieren de los almacenados.

    Dispara cambio (→ sube versión) si:
    - El cliente es nuevo.
    - hostname, ip o mac han cambiado.
    - El cliente estaba inactivo/archivado (reactivación).
    - extra_imperative ha cambiado (comparación de blob JSON normalizado).

    extra_informative nunca dispara versión: es puramente informativo.
    """
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute(
        "SELECT hostname, ip, mac, status, extra_imperative FROM clients WHERE id=?",
        (client_id,),
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        log(f"[DB] Cliente nuevo: {client_id}")
        return True

    old_hostname, old_ip, old_mac, old_status, old_extra_imp = row
    changes = []

    if hostname != old_hostname:
        changes.append(f"hostname: '{old_hostname}' → '{hostname}'")
    if ip != old_ip:
        changes.append(f"ip: '{old_ip}' → '{ip}'")
    if mac != old_mac:
        changes.append(f"mac: '{old_mac}' → '{mac}'")
    if old_status != "active":
        changes.append(f"status: '{old_status}' → 'active' (reactivación)")

    new_extra_imp = _serialize_extra(extra_imperative)
    # None = sin opinión (script no configurado o fallo transitorio): no reportar cambio.
    # _EXTRA_CLEAR = borrado explícito: reportar solo si había valor previo.
    if new_extra_imp is not None:
        effective = None if new_extra_imp == _EXTRA_CLEAR else new_extra_imp
        if effective != old_extra_imp:
            changes.append("extra_imperative cambió")

    if changes:
        log(f"[DB] Cambios en {client_id}: {', '.join(changes)}")
        return True

    return False


def add_or_update_client(
    client_id: str,
    hostname: str,
    ip: str,
    mac: str,
    extra_imperative: dict | None = None,
    extra_informative: dict | None = None,
) -> None:
    """
    Inserta o actualiza un cliente en la base de datos (upsert por id).

    Siempre actualiza last_seen y restaura status a 'active'.
    Esto reactiva automáticamente clientes que estaban inactivos o archivados.

    Semántica de extras (implementada via CASE SQL):
      None        → COALESCE: el campo en BD no se toca (script fallido o delegate).
      __clear__   → NULL en BD: el campo se borra (EXTRAS_ENABLED=false en VAC).
      JSON string → sobreescribe el campo con el nuevo valor.

    extra_imperative y extra_informative se almacenan como JSON compacto con
    claves ordenadas para que la comparación de string en client_has_changed
    sea determinista entre ciclos aunque el emisor varíe el orden de claves.

    El SELECT previo al INSERT es necesario para distinguir "insertado" de
    "actualizado" en el log, ya que lastrowid no es fiable con ON CONFLICT.
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,))
    is_new = cur.fetchone() is None

    ei  = _serialize_extra(extra_imperative)
    einf = _serialize_extra(extra_informative)

    cur.execute("""
        INSERT INTO clients (id, hostname, ip, mac, status, extra_imperative, extra_informative)
        VALUES (?, ?, ?, ?, 'active', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            hostname          = excluded.hostname,
            ip                = excluded.ip,
            mac               = excluded.mac,
            status            = 'active',
            extra_imperative  = CASE
                WHEN excluded.extra_imperative  = '__clear__' THEN NULL        -- borrado explícito ({} en Python)
                WHEN excluded.extra_imperative  IS NULL       THEN clients.extra_imperative  -- COALESCE: conservar valor
                ELSE excluded.extra_imperative  END,
            extra_informative = CASE
                WHEN excluded.extra_informative = '__clear__' THEN NULL
                WHEN excluded.extra_informative IS NULL       THEN clients.extra_informative
                ELSE excluded.extra_informative END,
            last_seen         = CURRENT_TIMESTAMP
    """, (client_id, hostname, ip, mac, ei, einf))

    action = "insertado" if is_new else "actualizado"
    conn.commit()
    conn.close()
    log_debug(f"[DB] Cliente {action}: {client_id} ({hostname}, {ip})")


def touch_client(client_id: str) -> None:
    """
    Actualiza last_seen y restaura status='active' de un cliente conocido.

    No toca ningún campo de datos (hostname, ip, mac, extras).
    Si el cliente estaba inactive o archived, la reactivación sube versión
    para que los consumidores detecten el cambio de inventario.
    Lanza ValueError si el UUID no existe (VAC debe re-registrarse).
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("SELECT status FROM clients WHERE id = ?", (client_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"Cliente no registrado: {client_id}")

    old_status = row[0]

    cur.execute(
        "UPDATE clients SET last_seen = CURRENT_TIMESTAMP, status = 'active' WHERE id = ?",
        (client_id,),
    )
    conn.commit()
    conn.close()

    if old_status != "active":
        version = bump_version()
        log(
            f"[DB] Heartbeat (reactivación {old_status}→active): {client_id} versión={version}"
        )
    else:
        log_debug(f"[DB] Heartbeat: {client_id}")


def get_all_clients(status: str = "active", extra_key: str | None = None) -> list:
    """
    Devuelve clientes filtrados por status y, opcionalmente, por clave de extra.

    status='active'   → solo activos (default, consumidores normales)
    status='inactive' → solo inactivos
    status='archived' → solo archivados
    status='all'      → todos los estados (histórico completo)

    extra_key=None    → sin filtro adicional (comportamiento original)
    extra_key='cups'  → solo clientes que tengan 'cups' en extra_imperative
                        o en extra_informative. El consumidor interpreta el valor.

    Cada entrada incluye: hostname, ip, mac, status, last_seen,
    extra_imperative, extra_informative (campos extra completos).
    Ordenado alfabéticamente por hostname.
    """
    conn = get_connection()
    cur  = conn.cursor()

    if status == "all":
        cur.execute(
            "SELECT hostname, ip, mac, status, last_seen, extra_imperative, extra_informative"
            " FROM clients ORDER BY hostname"
        )
    else:
        cur.execute(
            "SELECT hostname, ip, mac, status, last_seen, extra_imperative, extra_informative"
            " FROM clients WHERE status=? ORDER BY hostname",
            (status,),
        )

    rows = cur.fetchall()
    conn.close()

    # El UUID no se incluye en el listado público: principio de mínima exposición.
    # Solo GET /clients/{id} lo devuelve, y quien lo consulta ya lo conoce.
    def _parse(blob):
        try:
            return json.loads(blob) if blob else None
        except Exception:
            return None

    result = [
        {
            "hostname":          r[0],
            "ip":                r[1],
            "mac":               r[2],
            "status":            r[3],
            "last_seen":         r[4],
            "extra_imperative":  _parse(r[5]),
            "extra_informative": _parse(r[6]),
        }
        for r in rows
    ]

    if extra_key:
        # Predicado adicional: retener solo clientes que tengan la clave en
        # alguno de los dos campos extra. El valor completo se incluye en la
        # respuesta; el consumidor decide qué hacer con él.
        result = [
            c for c in result
            if (c["extra_imperative"]  and extra_key in c["extra_imperative"])
            or (c["extra_informative"] and extra_key in c["extra_informative"])
        ]

    return result


def get_client(client_id: str) -> dict | None:
    """
    Devuelve los datos de un cliente por su UUID, o None si no existe.
    Incluye el campo status para diagnóstico.
    """
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute(
        "SELECT id, hostname, ip, mac, status, last_seen, extra_imperative, extra_informative"
        " FROM clients WHERE id=?",
        (client_id,),
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        return None

    def _parse(blob):
        try:
            return json.loads(blob) if blob else None
        except Exception:
            return None

    return {
        "id":                row[0],
        "hostname":          row[1],
        "ip":                row[2],
        "mac":               row[3],
        "status":            row[4],
        "last_seen":         row[5],
        "extra_imperative":  _parse(row[6]),
        "extra_informative": _parse(row[7]),
    }


def mark_inactive_clients(seconds: int) -> int:
    """
    Marca como 'inactive' los clientes 'active' sin heartbeat en más de `seconds` segundos.

    Operación no destructiva: el registro se conserva, solo cambia status.
    Devuelve el número de clientes marcados.
    """
    cutoff = _utcnow() - datetime.timedelta(seconds=seconds)
    conn   = get_connection()
    cur    = conn.cursor()

    cur.execute("""
        UPDATE clients
        SET status = 'inactive'
        WHERE status = 'active' AND last_seen < ?
    """, (cutoff,))

    marked = cur.rowcount
    conn.commit()
    conn.close()

    if marked > 0:
        log(f"[DB] {marked} cliente(s) marcado(s) como inactive (TTL: {_fmt_duration(seconds)}).")

    return marked


def archive_clients(seconds: int) -> int:
    """
    Mueve a 'archived' los clientes 'inactive' sin heartbeat en más de `seconds` segundos.

    Operación no destructiva: mantiene el histórico completo.
    Devuelve el número de clientes archivados.
    """
    cutoff = _utcnow() - datetime.timedelta(seconds=seconds)
    conn   = get_connection()
    cur    = conn.cursor()

    cur.execute("""
        UPDATE clients
        SET status = 'archived'
        WHERE status = 'inactive' AND last_seen < ?
    """, (cutoff,))

    archived = cur.rowcount
    conn.commit()
    conn.close()

    if archived > 0:
        log(f"[DB] {archived} cliente(s) archivado(s) (TTL: {_fmt_duration(seconds)}).")

    return archived


def purge_clients(seconds: int) -> int:
    """
    Elimina definitivamente los clientes 'archived' sin heartbeat en más de `seconds` segundos.

    Operación destructiva e irreversible. Si seconds=0, no elimina nada.
    Devuelve el número de clientes eliminados.
    """
    if seconds == 0:
        log_debug("[DB] TTL_PURGE=0: borrado permanente desactivado.")
        return 0

    cutoff = _utcnow() - datetime.timedelta(seconds=seconds)
    conn   = get_connection()
    cur    = conn.cursor()

    cur.execute("""
        DELETE FROM clients
        WHERE status = 'archived' AND last_seen < ?
    """, (cutoff,))

    purged = cur.rowcount
    conn.commit()
    conn.close()

    if purged > 0:
        log(f"[DB] {purged} cliente(s) eliminado(s) definitivamente (TTL: {_fmt_duration(seconds)}).")

    return purged


def get_version() -> str:
    """
    Lee la versión actual desde VERSION_FILE.

    Devuelve "0" si el fichero no existe o su contenido es inválido.
    La versión tiene formato YYYYMMDDHHMMSSmmm (timestamp UTC con milisegundos).
    """
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        log(f"[DB] Aviso: fichero de versión no encontrado ({VERSION_FILE}). Usando 0.")
        return "0"
    except IOError as e:
        log(f"[DB] Error leyendo versión ({VERSION_FILE}): {e}. Usando 0.")
        return "0"


def _run_hooks(version: str) -> None:
    """Fire-and-forget: lanza en paralelo cada script ejecutable de HOOKS_DIR.

    Por defecto stdout/stderr heredan de VAS → journald los captura con [VAS].
    Si HOOKS_LOG está definido, redirige stdout/stderr del hook a ese fichero.
    """
    if not HOOKS_DIR or not os.path.isdir(HOOKS_DIR):
        return
    env = {**os.environ, "VAS_VERSION": version}
    if VAS_BASE_URL:
        env["VAS_HOST"] = VAS_BASE_URL
    for name in sorted(os.listdir(HOOKS_DIR)):
        path = os.path.join(HOOKS_DIR, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            try:
                if HOOKS_LOG:
                    lf = open(HOOKS_LOG, "a")
                    subprocess.Popen(
                        [path], env=env, close_fds=True, stdin=subprocess.DEVNULL,
                        stdout=lf, stderr=lf,
                    )
                    lf.close()
                else:
                    subprocess.Popen([path], env=env, close_fds=True, stdin=subprocess.DEVNULL)
                log_debug(f"[HOOKS] Lanzado: {name}")
            except Exception as e:
                log(f"[HOOKS] Error lanzando {name}: {e}")


def bump_version() -> str:
    """
    Genera una nueva versión como timestamp UTC (YYYYMMDDHHMMSSmmm) y la escribe.

    Incluye milisegundos para minimizar colisiones cuando dos cambios ocurren
    en el mismo segundo.
    Usa escritura atómica (fichero temporal + os.replace) para evitar
    que una interrupción deje el fichero de versión corrupto o vacío.
    Tras escribir la versión lanza en paralelo (fire and forget) los hooks
    de HOOKS_DIR para notificación push a consumidores VAL-Aware.
    """
    now = _utcnow()
    version = now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    tmp = VERSION_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(version)
        os.replace(tmp, VERSION_FILE)
        log(f"[DB] Versión actualizada: {version}")
    except IOError as e:
        log(f"[DB] Error escribiendo versión ({VERSION_FILE}): {e}")
    _run_hooks(version)
    return version
