"""
init_db.py
----------
Run this ONCE to create the SQLite database file (foodtracker.db) and its
three tables. Running it again is safe: it uses "CREATE TABLE IF NOT EXISTS",
so it will not wipe data you already have.

In R terms: think of this like a script that sets up an empty data frame
structure (column names + types) before you start adding rows.

Run it from the terminal with:   python init_db.py
"""

import sqlite3  # built into Python — no install needed. This talks to SQLite.

# The name of the database file we want to create / open.
DB_NAME = "foodtracker.db"


def main():
    # sqlite3.connect() opens the file. If it doesn't exist yet, it is created.
    conn = sqlite3.connect(DB_NAME)
    # A "cursor" is the object you use to run SQL commands.
    cur = conn.cursor()

    # --- Table 1: foods -------------------------------------------------
    # A reference list of foods and their nutrition PER 100 GRAMS.
    # "INTEGER PRIMARY KEY" makes `id` an auto-numbering unique row id.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS foods (
            id                  INTEGER PRIMARY KEY,
            name                TEXT NOT NULL,
            calories_per_100g   REAL,
            protein_g           REAL,
            carbs_g             REAL,
            fat_g               REAL,
            source              TEXT
        )
        """
    )

    # --- Table 2: logs --------------------------------------------------
    # Every time you eat something, one row gets added here.
    # The calorie/macro columns store the ALREADY-SCALED values for the
    # quantity you actually ate (not the per-100g values).
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id          INTEGER PRIMARY KEY,
            date        TEXT NOT NULL,
            food_id     INTEGER,
            food_name   TEXT,
            quantity_g  REAL,
            meal_type   TEXT,
            calories    REAL,
            protein_g   REAL,
            carbs_g     REAL,
            fat_g       REAL,
            FOREIGN KEY (food_id) REFERENCES foods(id)
        )
        """
    )

    # --- Table 3: targets -----------------------------------------------
    # Your daily goals. We keep just ONE row in this table.
    # Calories and protein are stored as a RANGE (min + max) because you're
    # cutting: calories have a ceiling to stay under, protein a floor to hit.
    # Carbs and fat stay as a single optional target (empty for now).
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS targets (
            id           INTEGER PRIMARY KEY,
            calorie_min  REAL,
            calorie_max  REAL,
            protein_min  REAL,
            protein_max  REAL,
            carb_target  REAL,
            fat_target   REAL
        )
        """
    )

    # Pre-fill your targets, but ONLY if the table is currently empty.
    # (Otherwise re-running this script would keep adding duplicate rows.)
    cur.execute("SELECT COUNT(*) FROM targets")
    if cur.fetchone()[0] == 0:
        cur.execute(
            """
            INSERT INTO targets
                (calorie_min, calorie_max, protein_min, protein_max, carb_target, fat_target)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (1600, 2000, 120, 140, None, None),  # cutting targets; carbs/fat empty
        )
        print("Inserted cutting targets: 1600–2000 kcal, 120–140 g protein.")

    # conn.commit() saves your changes to disk. Without it, nothing is kept.
    conn.commit()
    conn.close()
    print(f"Database '{DB_NAME}' is ready.")


if __name__ == "__main__":
    # This block runs only when you execute the file directly.
    main()
