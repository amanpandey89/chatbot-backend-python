"""
Standalone MySQL connectivity check — run this wherever the app will actually
run (your machine, or Render) before trusting the full app against it, since
this specific host/credentials could not be reached from the dev sandbox that
wrote this migration.

Usage:
    python3 check_mysql_connection.py
Reads the same MYSQL_* env vars as src/services/db.py (loads .env if present).
"""

from dotenv import load_dotenv

load_dotenv()

import os
import sys

host = os.getenv("MYSQL_HOST", "")
port = int(os.getenv("MYSQL_PORT", "3306") or "3306")
database = os.getenv("MYSQL_DATABASE", "")
user = os.getenv("MYSQL_USER", "")
password = os.getenv("MYSQL_PASSWORD", "")
ssl_ca = os.getenv("MYSQL_SSL_CA", "").strip()
ssl_disabled = os.getenv("MYSQL_SSL_DISABLED", "0").strip().lower() in ("1", "true", "yes", "on")

if not (host and database and user):
    print("MYSQL_HOST / MYSQL_DATABASE / MYSQL_USER not all set — nothing to check.")
    sys.exit(1)

print(f"Connecting to {user}@{host}:{port}/{database} (ssl={'off' if ssl_disabled else 'on'}) ...")

import pymysql

ssl_kwargs = {}
if not ssl_disabled:
    ssl_kwargs["ssl"] = {"ca": ssl_ca} if ssl_ca else {}

try:
    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        connect_timeout=10,
        **ssl_kwargs,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION()")
        (version,) = cur.fetchone()
        cur.execute("SHOW TABLES")
        tables = [r[0] for r in cur.fetchall()]
    conn.close()
    print(f"OK — connected. MySQL version: {version}")
    print(f"Tables present ({len(tables)}): {tables}")
except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}")
    print(
        "\nCommon causes:\n"
        "- Azure MySQL firewall: add this machine's outbound IP to the server's "
        "'Networking' allow-list (Azure blocks all IPs by default).\n"
        "- Wrong port/credentials.\n"
        "- If the server requires TLS and this fails with an SSL error, try setting "
        "MYSQL_SSL_CA to a CA bundle path, or MYSQL_SSL_DISABLED=1 only if the host "
        "truly has TLS off."
    )
    sys.exit(1)
