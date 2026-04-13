import sqlite3
from pathlib import Path

DB_PATH = Path("vas.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        id TEXT PRIMARY KEY,
        hostname TEXT,
        ip TEXT,
        mac TEXT,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    c.execute("INSERT OR IGNORE INTO metadata (key, value) VALUES ('version', '1')")
    conn.commit()
    conn.close()

def get_version():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM metadata WHERE key='version'")
    version = c.fetchone()[0]
    conn.close()
    return version

def increment_version():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE metadata SET value = value + 1 WHERE key='version'")
    conn.commit()
    conn.close()

def register_client(id, hostname, ip, mac):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO clients (id, hostname, ip, mac)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            hostname=excluded.hostname,
            ip=excluded.ip,
            mac=excluded.mac,
            last_seen=CURRENT_TIMESTAMP
    """, (id, hostname, ip, mac))
    conn.commit()
    conn.close()
