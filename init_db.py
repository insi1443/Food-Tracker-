"""
init_db.py
----------
Run this ONCE to set up the database tables and seed your targets:

    python init_db.py

The actual table definitions live in db.py (so there's a single source of
truth that works for both your local SQLite file and the cloud Postgres
database). This script just calls that setup function. Running it again is
safe — it only creates tables that don't already exist.
"""

from db import ensure_db, engine


def main():
    ensure_db()
    print(f"Database is ready ({engine.dialect.name}).")


if __name__ == "__main__":
    main()
