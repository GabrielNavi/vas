#!/usr/bin/env python3
"""
database.py — Capa de persistencia de VAS.

Gestiona la base de datos SQLite de clientes y el fichero de versión.
Las rutas DB_PATH y VERSION_FILE son inyectadas por vas.py antes de
cualquier llamada a las funciones de este módulo.
"""
import sqlite3
import os
import datetime

# Inyectadas por vas.py antes del primer uso
DB_PATH      = None
VERSION_FILE = None


def get_connection():
    """Abre y devuelve una conexión SQLite a DB_PATH."""
    return sqlite3.connect(DB_PATH)


def init_db():
    """
    Inicializa el esquema de la base de datos y el fichero de versión.

    Crea el directorio de DB_PATH si no existe.
    Crea la tabla 'clients' si no existe (idempotente).
    Crea VERSION_FILE con valor "0" si no existe.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id        TEXT PRIMARY KEY,
            hostname  TEXT,
            ip        TEXT,
            mac       TEXT,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

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


def client_has_changed(client_id: str, hostname: str, ip: str, mac: str) -> bool:
    """
    Comprueba si los datos enviados difieren de los almacenados.

    Devuelve True si el cliente es nuevo o si hostname, ip o mac han cambiado.
    Devuelve False si todos los campos coinciden (heartbeat sin cambios).
    """
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT hostname, ip, mac FROM clients WHERE id=?", (client_id,))
    row = cur.fetchone()
    conn.close()

    if row is None:
        print(f"[VAS-DB] Cliente nuevo: {client_id}", flush=True)
        return True

    old_hostname, old_ip, old_mac = row
    changes = []

    if hostname != old_hostname:
        changes.append(f"hostname: '{old_hostname}' → '{hostname}'")
    if ip != old_ip:
        changes.append(f"ip: '{old_ip}' → '{ip}'")
    if mac != old_mac:
        changes.append(f"mac: '{old_mac}' → '{mac}'")

    if changes:
        print(f"[VAS-DB] Cambios en {client_id}: {', '.join(changes)}", flush=True)
        return True

    return False


def add_or_update_client(client_id: str, hostname: str, ip: str, mac: str) -> None:
    """
    Inserta o actualiza un cliente en la base de datos (upsert por id).

    Siempre actualiza last_seen, tanto en inserciones como en actualizaciones.
    Esto mantiene el TTL activo aunque los datos no hayan cambiado.
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        INSERT INTO clients (id, hostname, ip, mac)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            hostname  = excluded.hostname,
            ip        = excluded.ip,
            mac       = excluded.mac,
            last_seen = CURRENT_TIMESTAMP
    """, (client_id, hostname, ip, mac))

    action = "insertado" if cur.lastrowid and cur.rowcount == 1 else "actualizado"
    conn.commit()
    conn.close()
    print(f"[VAS-DB] Cliente {action}: {client_id} ({hostname}, {ip})", flush=True)


def get_all_clients() -> list:
    """
    Devuelve la lista completa de clientes registrados.

    Cada entrada incluye: id, hostname, ip, mac, last_seen.
    """
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT id, hostname, ip, mac, last_seen FROM clients ORDER BY hostname")
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id":        r[0],
            "hostname":  r[1],
            "ip":        r[2],
            "mac":       r[3],
            "last_seen": r[4],
        }
        for r in rows
    ]


def get_client(client_id: str) -> dict | None:
    """
    Devuelve los datos de un cliente por su UUID, o None si no existe.
    """
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute(
        "SELECT id, hostname, ip, mac, last_seen FROM clients WHERE id=?",
        (client_id,),
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        return None

    return {
        "id":        row[0],
        "hostname":  row[1],
        "ip":        row[2],
        "mac":       row[3],
        "last_seen": row[4],
    }


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

    Devuelve la nueva versión como string.
    """
    version = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    tmp = VERSION_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(version)
        os.replace(tmp, VERSION_FILE)
        print(f"[VAS-DB] Versión actualizada: {version}", flush=True)
    except IOError as e:
        print(f"[VAS-DB] Error escribiendo versión ({VERSION_FILE}): {e}", flush=True)
    return version
