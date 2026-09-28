"""Wait for SQL Server and create the database DB_NAME if it doesn't exist (devcontainer and CI).

Uses DB_HOST, DB_PORT, DB_USER, DB_PASSWORD and DB_NAME. Production databases are created by their admins.
"""
import os
import time

from mssql_python import connect


def main():
    conn_str = (f"Server={os.environ['DB_HOST']},{os.environ.get('DB_PORT', '1433')};Database=master;"
                f"UID={os.environ['DB_USER']};PWD={os.environ['DB_PASSWORD']};TrustServerCertificate=yes;")
    for _ in range(60):
        try:
            conn = connect(conn_str, autocommit=True, timeout=10)
            break
        except Exception as e:
            print(f"SQL Server not ready: {e}")
            time.sleep(2)
    else:
        raise SystemExit("SQL Server did not start")
    name = os.environ.get("DB_NAME", "santa")
    if not name.replace("_", "").isalnum():
        raise SystemExit(f"Invalid database name {name!r}")
    cursor = conn.cursor()
    cursor.execute(f"IF DB_ID('{name}') IS NULL CREATE DATABASE [{name}]")
    conn.close()
    print(f"Database {name} ready")


if __name__ == "__main__":
    main()
