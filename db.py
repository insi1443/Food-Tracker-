"""
db.py
-----
One place that knows HOW to reach the database.

- On your Mac (local development): a SQLite file, foodtracker.db.
- In the cloud (Streamlit): a Postgres database, whose address comes from a
  secret called DATABASE_URL. Streamlit Cloud's filesystem gets wiped on every
  restart, so a real cloud database is what keeps your history.

The rest of the app just imports `engine` and `ensure_db` from here and never
has to care which database is behind it. We use SQLAlchemy, a library that
speaks to both SQLite and Postgres with the same code.
"""

import os
import threading

from sqlalchemy import create_engine, text


def _database_url():
    """Work out which database to connect to, in order of preference."""
    # 1) Streamlit Secrets — set when deployed to Streamlit Cloud.
    try:
        import streamlit as st
        if "DATABASE_URL" in st.secrets:
            url = st.secrets["DATABASE_URL"]
            return _normalise(url)
    except Exception:
        pass
    # 2) An environment variable (e.g. from your .env file), else
    # 3) a local SQLite file for development on your Mac.
    return _normalise(os.getenv("DATABASE_URL", "sqlite:///foodtracker.db"))


def _normalise(url):
    # Some providers hand out "postgres://..."; SQLAlchemy needs "postgresql://".
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


_URL = _database_url()
# SQLite needs one extra setting to be usable across Streamlit's threads;
# Postgres does not take that argument.
_connect_args = {"check_same_thread": False} if _URL.startswith("sqlite") else {}

# The engine is the shared connection pool the whole app uses.
engine = create_engine(_URL, connect_args=_connect_args, pool_pre_ping=True)


# Guards so the schema is built only once per process, even when Streamlit
# calls ensure_db() from several threads at the same moment (which on Postgres
# would otherwise race and collide creating the tables' hidden id sequences).
_init_lock = threading.Lock()
_db_ready = False


def ensure_db():
    """Create the tables + seed your targets once. Safe to call on every run."""
    global _db_ready
    if _db_ready:
        return
    with _init_lock:
        if _db_ready:  # another thread finished while we waited for the lock
            return
        _create_schema()
        _db_ready = True


def _create_schema():
    """Build the tables. Works on both SQLite and Postgres — the only line that
    differs is how an auto-numbering id is declared."""
    if engine.dialect.name == "postgresql":
        pk = "id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"
    else:  # sqlite
        pk = "id INTEGER PRIMARY KEY"

    with engine.begin() as conn:
        # On Postgres, a transaction-level advisory lock serialises this DDL
        # across every connection/container, so concurrent first-runs can't
        # collide. (Harmless no-op concept on SQLite, so we skip it there.)
        if engine.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(727274)"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS foods (
                {pk},
                name              TEXT NOT NULL,
                calories_per_100g REAL,
                protein_g         REAL,
                carbs_g           REAL,
                fat_g             REAL,
                source            TEXT
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS logs (
                {pk},
                date       TEXT NOT NULL,
                food_id    INTEGER,
                food_name  TEXT,
                quantity_g REAL,
                meal_type  TEXT,
                calories   REAL,
                protein_g  REAL,
                carbs_g    REAL,
                fat_g      REAL
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS targets (
                {pk},
                calorie_min REAL,
                calorie_max REAL,
                protein_min REAL,
                protein_max REAL,
                carb_target REAL,
                fat_target  REAL
            )
        """))
        # Your body stats + goals (one row), used to compute dynamic targets.
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS profile (
                {pk},
                height_cm       REAL,
                age             INTEGER,
                sex             TEXT,
                activity_factor REAL,
                goal_weight_kg  REAL,
                goal_bodyfat_pct REAL,
                trip_date       TEXT,
                deficit_kcal    REAL
            )
        """))
        # Every weigh-in.
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS weight_log (
                {pk},
                date         TEXT NOT NULL,
                weight_kg    REAL,
                body_fat_pct REAL
            )
        """))
        # Daily steps + exercise (one row per day).
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS activity_log (
                {pk},
                date          TEXT NOT NULL UNIQUE,
                steps         INTEGER,
                exercise_kcal REAL,
                note          TEXT
            )
        """))
        # Seed your cutting targets, but only if the table is empty.
        count = conn.execute(text("SELECT COUNT(*) FROM targets")).scalar()
        if count == 0:
            conn.execute(text("""
                INSERT INTO targets
                    (calorie_min, calorie_max, protein_min, protein_max, carb_target, fat_target)
                VALUES (1600, 2000, 120, 140, NULL, NULL)
            """))
