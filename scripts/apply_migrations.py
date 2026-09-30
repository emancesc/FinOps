"""Apply migrations/*.sql in order, tracking what was applied in schema_migrations.

Usage: python scripts/apply_migrations.py [--database-url postgresql://...]
Default DATABASE_URL: environment variable or the repo .env file.
001_init.sql is recorded as applied without running it when the jobs table already exists.
"""
import argparse
import glob
import os
import sys

import psycopg2

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def database_url():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = os.path.join(REPO, ".env")
    if os.path.exists(env):
        for line in open(env, encoding="utf-8"):
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("DATABASE_URL non trovato (variabile d'ambiente o .env)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply SQL migrations")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    conn = psycopg2.connect(args.database_url or database_url())
    conn.autocommit = False
    with conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        cur.execute("SELECT name FROM schema_migrations")
        applied = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT to_regclass('public.jobs') IS NOT NULL")
        has_base_schema = cur.fetchone()[0]

    for path in sorted(glob.glob(os.path.join(REPO, "migrations", "[0-9][0-9][0-9]_*.sql"))):
        name = os.path.basename(path)
        if name in applied or name.endswith("_native.sql"):
            continue
        with conn, conn.cursor() as cur:
            if name.startswith("001_") and has_base_schema:
                print(f"{name}: schema di base già presente, registrata senza eseguirla")
            else:
                cur.execute(open(path, encoding="utf-8").read())
                print(f"{name}: applicata")
            cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    conn.close()
    print("Migrazioni aggiornate")


if __name__ == "__main__":
    main()
