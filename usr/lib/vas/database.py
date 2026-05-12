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
import datetime
from datetime import timezone


def _utcnow() -> datetime.datetime:
    """Devuelve la hora UTC actual como datetime naive (compatible con CURRENT_TIMESTAMP de SQLite)."""
    return datetime.datetime.now(timezone.utc).replace(tzinfo=None)

# Inyectadas por vas.py antes del primer uso
DB_PATH      = None
VERSION_FILE = None


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
            print(f"[VAS-DB] Migración: columna '{col}' añadida.", flush=True)

    conn.commit()
    conn.close()
    print(f"[VAS-DB] Base de datos lista: {DB_PATH}", flush=True)

    # Inicializar fichero de versión
    version_dir = os.path.dirname(VERSION_FILE)
    if version_dir:
        os.makedirs(version_dir, exist_ok=True)

    if not os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, "w") as f:
            f.write("0")
        print(f"[VAS-DB] Fichero de versión inicializado: {VERSION_FILE} → 0", flush=True)
    else:
        current = get_version()
        print(f"[VAS-DB] Versión actual al arrancar: {current}", flush=True)


def _serialize_extra(extra: dict | None) -> str | None:
    """
    Serializa un dict de extra a JSON compacto con claves ordenadas.

    Orden determinista para que la comparación de string funcione correctamente
    aunque el emisor varíe el orden de las claves entre ciclos.
    Devuelve None si extra es None o vacío.
    """
    if not extra:
        return None
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
        print(f"[VAS-DB] Cliente nuevo: {client_id}", flush=True)
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
    if new_extra_imp != old_extra_imp:
        changes.append("extra_imperative cambió")

    if changes:
        print(f"[VAS-DB] Cambios en {client_id}: {', '.join(changes)}", flush=True)
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
    extra_imperative y extra_informative se almacenan como JSON compacto con
    claves ordenadas para que la comparación de string en client_has_changed
    sea determinista.
    """
    conn = get_connection()
    cur  = conn.cursor()

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
            extra_imperative  = excluded.extra_imperative,
            extra_informative = excluded.extra_informative,
            last_seen         = CURRENT_TIMESTAMP
    """, (client_id, hostname, ip, mac, ei, einf))

    action = "insertado" if cur.lastrowid and cur.rowcount == 1 else "actualizado"
    conn.commit()
    conn.close()
    print(f"[VAS-DB] Cliente {action}: {client_id} ({hostname}, {ip})", flush=True)


def get_all_clients(status: str = "active") -> list:
    """
    Devuelve clientes filtrados por status.

    status='active'   → solo activos (default, consumidores normales)
    status='inactive' → solo inactivos
    status='archived' → solo archivados
    status='all'      → todos los estados (histórico completo)

    Cada entrada incluye: id, hostname, ip, mac, status, last_seen.
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

    return [
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


def mark_inactive_clients(days: int) -> int:
    """
    Marca como 'inactive' los clientes 'active' sin heartbeat en más de `days` días.

    Operación no destructiva: el registro se conserva, solo cambia status.
    Devuelve el número de clientes marcados.
    """
    cutoff = _utcnow() - datetime.timedelta(days=days)
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
        print(f"[VAS-DB] {marked} cliente(s) marcado(s) como inactive (TTL: {days}d).", flush=True)

    return marked


def archive_clients(days: int) -> int:
    """
    Mueve a 'archived' los clientes 'inactive' sin heartbeat en más de `days` días.

    Operación no destructiva: mantiene el histórico completo.
    Devuelve el número de clientes archivados.
    """
    cutoff = _utcnow() - datetime.timedelta(days=days)
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
        print(f"[VAS-DB] {archived} cliente(s) archivado(s) (TTL: {days}d).", flush=True)

    return archived


def purge_clients(days: int) -> int:
    """
    Elimina definitivamente los clientes 'archived' sin heartbeat en más de `days` días.

    Operación destructiva e irreversible. Si days=0, no elimina nada.
    Devuelve el número de clientes eliminados.
    """
    if days == 0:
        print("[VAS-DB] TTL_PURGE_DAYS=0: borrado permanente desactivado.", flush=True)
        return 0

    cutoff = _utcnow() - datetime.timedelta(days=days)
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
        print(f"[VAS-DB] {purged} cliente(s) eliminado(s) definitivamente (TTL: {days}d).", flush=True)

    return purged


def get_version() -> str:
    """
    Lee la versión actual desde VERSION_FILE.

    Devuelve "0" si el fichero no existe o su contenido es inválido.
    La versión tiene formato YYYYMMDDHHMMSS (timestamp UTC).
    """
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"[VAS-DB] Aviso: fichero de versión no encontrado ({VERSION_FILE}). Usando 0.", flush=True)
        return "0"
    except IOError as e:
        print(f"[VAS-DB] Error leyendo versión ({VERSION_FILE}): {e}. Usando 0.", flush=True)
        return "0"


def bump_version() -> str:
    """
    Genera una nueva versión como timestamp UTC (YYYYMMDDHHMMSS) y la escribe.

    Usa escritura atómica (fichero temporal + os.replace) para evitar
    que una interrupción deje el fichero de versión corrupto o vacío.
    """
    version = _utcnow().strftime("%Y%m%d%H%M%S")
    tmp = VERSION_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(version)
        os.replace(tmp, VERSION_FILE)
        print(f"[VAS-DB] Versión actualizada: {version}", flush=True)
    except IOError as e:
        print(f"[VAS-DB] Error escribiendo versión ({VERSION_FILE}): {e}", flush=True)
    return version
