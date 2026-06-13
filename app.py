"""
app.py
------
The Streamlit web app. Streamlit turns a normal Python script into a web page:
every time you interact with a widget (type in a box, click a button), Streamlit
re-runs this whole file top-to-bottom and redraws the page. That feels strange
at first, but it means you mostly just write straight-line Python.

Run it with:   streamlit run app.py
Then open the URL it prints (usually http://localhost:8501).
"""

import base64
import json
import os
from datetime import date, timedelta

import anthropic         # the Claude SDK — analyses food photos
import pandas as pd      # used for the weight trend chart
import requests          # for calling the USDA web API
import streamlit as st   # the web-app framework
from dotenv import load_dotenv  # reads your API keys out of the .env file
from sqlalchemy import text  # lets us run SQL through the shared engine

import health            # calorie / protein / projection maths
from db import engine, ensure_db  # the database connection (SQLite or Postgres)

# Load variables from the .env file into the environment so we can read them.
load_dotenv()


def get_secret(name, default=None):
    """Read a secret from Streamlit's Secrets box (when deployed) or from the
    environment / .env file (when running on your Mac). Keeps keys out of code."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.getenv(name, default)


USDA_API_KEY = get_secret("USDA_API_KEY", "DEMO_KEY")  # falls back to DEMO_KEY

# The USDA FoodData Central search endpoint.
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

# The Claude model we use to look at photos. Opus 4.8 is Anthropic's most
# capable model and supports vision (reading images).
CLAUDE_MODEL = "claude-opus-4-8"


# ----------------------------------------------------------------------
# DATABASE HELPERS
# Small functions so the page code below stays readable.
# ----------------------------------------------------------------------
def get_targets():
    """Return the single targets row as a dict (or None if missing)."""
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT calorie_min, calorie_max, protein_min, protein_max, carb_target, fat_target
            FROM targets LIMIT 1
        """)).fetchone()
    if row is None:
        return None
    return {
        "calorie_min": row[0],
        "calorie_max": row[1],
        "protein_min": row[2],
        "protein_max": row[3],
        "carb_target": row[4],
        "fat_target": row[5],
    }


def get_totals_for(day_iso):
    """Sum up calories/macros for everything logged on one day (e.g. '2026-06-13').
    COALESCE(..., 0) turns a NULL sum (no rows yet) into 0."""
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT
                COALESCE(SUM(calories), 0),
                COALESCE(SUM(protein_g), 0),
                COALESCE(SUM(carbs_g), 0),
                COALESCE(SUM(fat_g), 0)
            FROM logs
            WHERE date = :day
        """), {"day": day_iso}).fetchone()
    return {
        "calories": row[0],
        "protein_g": row[1],
        "carbs_g": row[2],
        "fat_g": row[3],
    }


def get_today_totals():
    """Convenience wrapper: today's totals."""
    return get_totals_for(date.today().isoformat())


def get_totals_in_range(start_iso, end_iso):
    """
    Return a dict {date_string: totals_dict} for every day in [start, end]
    that has at least one logged entry. Days with nothing logged are omitted.
    """
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT
                date,
                COALESCE(SUM(calories), 0),
                COALESCE(SUM(protein_g), 0),
                COALESCE(SUM(carbs_g), 0),
                COALESCE(SUM(fat_g), 0)
            FROM logs
            WHERE date BETWEEN :start AND :end
            GROUP BY date
        """), {"start": start_iso, "end": end_iso}).fetchall()
    return {
        r[0]: {"calories": r[1], "protein_g": r[2], "carbs_g": r[3], "fat_g": r[4]}
        for r in rows
    }


def get_logs_for(day_iso):
    """Return one day's individual log rows, newest first.
    The first column is the row `id`, which we need in order to delete it."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, meal_type, food_name, quantity_g, calories, protein_g, carbs_g, fat_g
            FROM logs
            WHERE date = :day
            ORDER BY id DESC
        """), {"day": day_iso}).fetchall()
    # Return plain tuples so the page code can unpack them simply.
    return [tuple(r) for r in rows]


def get_today_logs():
    """Convenience wrapper: today's log rows."""
    return get_logs_for(date.today().isoformat())


def delete_log(log_id):
    """Delete a single log entry by its row id."""
    with engine.begin() as conn:  # begin() = run inside a transaction and commit
        conn.execute(text("DELETE FROM logs WHERE id = :id"), {"id": log_id})


def save_food_and_log(food, quantity_g, meal_type):
    """
    Save the chosen food into `foods` (if not already there) and add a row to
    `logs` with the macros scaled to the quantity actually eaten.
    `food` is a dict we build from the USDA result.
    """
    with engine.begin() as conn:
        # Reuse an existing foods row if we already stored this exact food+source.
        existing = conn.execute(
            text("SELECT id FROM foods WHERE name = :n AND source = :s"),
            {"n": food["name"], "s": food["source"]},
        ).fetchone()
        if existing:
            food_id = existing[0]
        else:
            # RETURNING id hands back the new row's id (works on SQLite & Postgres).
            food_id = conn.execute(text("""
                INSERT INTO foods (name, calories_per_100g, protein_g, carbs_g, fat_g, source)
                VALUES (:n, :cal, :p, :c, :f, :s)
                RETURNING id
            """), {
                "n": food["name"], "cal": food["calories_per_100g"],
                "p": food["protein_g"], "c": food["carbs_g"],
                "f": food["fat_g"], "s": food["source"],
            }).scalar()

        # Scale per-100g values to the amount eaten. factor = grams / 100.
        factor = quantity_g / 100.0
        conn.execute(text("""
            INSERT INTO logs
                (date, food_id, food_name, quantity_g, meal_type,
                 calories, protein_g, carbs_g, fat_g)
            VALUES (:date, :fid, :name, :qty, :meal, :cal, :p, :c, :f)
        """), {
            "date": date.today().isoformat(),
            "fid": food_id,
            "name": food["name"],
            "qty": quantity_g,
            "meal": meal_type,
            "cal": round(food["calories_per_100g"] * factor, 1),
            "p": round(food["protein_g"] * factor, 1),
            "c": round(food["carbs_g"] * factor, 1),
            "f": round(food["fat_g"] * factor, 1),
        })


# ----------------------------------------------------------------------
# PROFILE / WEIGHT / ACTIVITY  (the body-composition side)
# ----------------------------------------------------------------------
def get_profile():
    """Return the single profile row as a dict (or None if not set up yet)."""
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT height_cm, age, sex, activity_factor, goal_weight_kg,
                   goal_bodyfat_pct, trip_date, deficit_kcal
            FROM profile LIMIT 1
        """)).fetchone()
    if row is None:
        return None
    keys = ["height_cm", "age", "sex", "activity_factor", "goal_weight_kg",
            "goal_bodyfat_pct", "trip_date", "deficit_kcal"]
    return dict(zip(keys, row))


def save_profile(p):
    """Insert or update the single profile row."""
    with engine.begin() as conn:
        existing = conn.execute(text("SELECT id FROM profile LIMIT 1")).fetchone()
        if existing:
            conn.execute(text("""
                UPDATE profile SET
                    height_cm = :height_cm, age = :age, sex = :sex,
                    activity_factor = :activity_factor, goal_weight_kg = :goal_weight_kg,
                    goal_bodyfat_pct = :goal_bodyfat_pct, trip_date = :trip_date,
                    deficit_kcal = :deficit_kcal
                WHERE id = :id
            """), {**p, "id": existing[0]})
        else:
            conn.execute(text("""
                INSERT INTO profile
                    (height_cm, age, sex, activity_factor, goal_weight_kg,
                     goal_bodyfat_pct, trip_date, deficit_kcal)
                VALUES (:height_cm, :age, :sex, :activity_factor, :goal_weight_kg,
                        :goal_bodyfat_pct, :trip_date, :deficit_kcal)
            """), p)


def add_weight(date_iso, weight_kg, body_fat_pct):
    """Record a weigh-in."""
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO weight_log (date, weight_kg, body_fat_pct)
            VALUES (:d, :w, :b)
        """), {"d": date_iso, "w": weight_kg, "b": body_fat_pct})


def get_weights():
    """All weigh-ins, oldest first, as (date, weight_kg, body_fat_pct) tuples."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT date, weight_kg, body_fat_pct FROM weight_log ORDER BY date, id
        """)).fetchall()
    return [tuple(r) for r in rows]


def get_latest_weight():
    """Most recent (weight_kg, body_fat_pct, date), or (None, None, None)."""
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT weight_kg, body_fat_pct, date FROM weight_log
            ORDER BY date DESC, id DESC LIMIT 1
        """)).fetchone()
    return tuple(row) if row else (None, None, None)


def save_activity(date_iso, steps, exercise_kcal, note):
    """Insert or update one day's steps + exercise."""
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id FROM activity_log WHERE date = :d"), {"d": date_iso}
        ).fetchone()
        if existing:
            conn.execute(text("""
                UPDATE activity_log SET steps = :s, exercise_kcal = :e, note = :n
                WHERE id = :id
            """), {"s": steps, "e": exercise_kcal, "n": note, "id": existing[0]})
        else:
            conn.execute(text("""
                INSERT INTO activity_log (date, steps, exercise_kcal, note)
                VALUES (:d, :s, :e, :n)
            """), {"d": date_iso, "s": steps, "e": exercise_kcal, "n": note})


def get_activity_for(date_iso):
    """One day's activity as a dict (zeros if nothing logged)."""
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT steps, exercise_kcal, note FROM activity_log WHERE date = :d
        """), {"d": date_iso}).fetchone()
    if row is None:
        return {"steps": 0, "exercise_kcal": 0, "note": ""}
    return {"steps": row[0] or 0, "exercise_kcal": row[1] or 0, "note": row[2] or ""}


def get_active_targets():
    """The targets the dashboard should use. If you've filled in your profile AND
    logged at least one weight, these are computed dynamically from your latest
    weight (so they drift as you do). Otherwise we fall back to the fixed targets
    in the `targets` table."""
    profile = get_profile()
    weight, body_fat, _ = get_latest_weight()
    if profile and profile.get("height_cm") and weight:
        maintenance = health.tdee(
            weight, profile["height_cm"], profile["age"],
            profile["sex"], profile["activity_factor"],
        )
        deficit = profile.get("deficit_kcal") or 500
        cmin, cmax = health.calorie_targets(maintenance, deficit)
        pmin, pmax = health.protein_targets(weight, body_fat)
        return {
            "calorie_min": cmin, "calorie_max": cmax,
            "protein_min": pmin, "protein_max": pmax,
            "carb_target": None, "fat_target": None,
            "dynamic": True, "tdee": maintenance, "deficit": deficit,
            "weight": weight, "body_fat_pct": body_fat,
        }
    # Fallback: the fixed targets row.
    t = get_targets() or {}
    t["dynamic"] = False
    return t


def calories_out_for(day_iso):
    """Estimated calories burned through steps + logged exercise on a day."""
    act = get_activity_for(day_iso)
    weight, _, _ = get_latest_weight()
    step_kcal = health.steps_to_kcal(act["steps"], weight)
    return step_kcal + (act["exercise_kcal"] or 0), act


# ----------------------------------------------------------------------
# USDA API HELPER
# ----------------------------------------------------------------------
def _nutrient(food_nutrients, number):
    """
    Pull one nutrient value out of a USDA food's nutrient list.
    USDA identifies nutrients by a `nutrientNumber` string:
        "208" = Energy (kcal), "203" = Protein, "205" = Carbs, "204" = Fat.
    Returns 0.0 if the nutrient isn't present.
    """
    for n in food_nutrients:
        if str(n.get("nutrientNumber")) == number:
            return n.get("value") or 0.0
    return 0.0


def search_usda(query):
    """
    Call the USDA search API and return a clean list of food dicts.
    Each dict already has per-100g calories and macros pulled out.
    """
    params = {
        "query": query,
        "api_key": USDA_API_KEY,
        "pageSize": 15,        # how many results to fetch
        "dataType": ["Foundation", "SR Legacy", "Branded"],
    }
    response = requests.get(USDA_SEARCH_URL, params=params, timeout=15)
    response.raise_for_status()  # raises an error if the request failed
    data = response.json()

    results = []
    for item in data.get("foods", []):
        nutrients = item.get("foodNutrients", [])
        results.append(
            {
                "name": item.get("description", "Unknown"),
                "source": "USDA",
                "calories_per_100g": _nutrient(nutrients, "208"),
                "protein_g": _nutrient(nutrients, "203"),
                "carbs_g": _nutrient(nutrients, "205"),
                "fat_g": _nutrient(nutrients, "204"),
            }
        )
    return results


# ----------------------------------------------------------------------
# CLAUDE PHOTO ANALYSIS
# Send a food photo to Claude and get back a structured estimate.
# ----------------------------------------------------------------------

# This describes the EXACT shape of JSON we want back. Passing a schema (a
# "structured output") forces Claude to reply with valid JSON in this format,
# so we never have to guess or parse free-form text.
FOOD_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "estimated_grams": {"type": "number"},
                    "calories": {"type": "number"},
                    "protein_g": {"type": "number"},
                    "carbs_g": {"type": "number"},
                    "fat_g": {"type": "number"},
                },
                "required": [
                    "name", "estimated_grams", "calories",
                    "protein_g", "carbs_g", "fat_g",
                ],
                "additionalProperties": False,
            },
        },
        "meal_guess": {
            "type": "string",
            "enum": ["breakfast", "lunch", "dinner", "snack"],
        },
    },
    "required": ["items", "meal_guess"],
    "additionalProperties": False,
}

PHOTO_PROMPT = (
    "You are a nutrition assistant. Look at this photo of food and identify "
    "each distinct food item you can see. For EACH item, estimate the portion "
    "size in grams and the calories, protein, carbohydrates, and fat for that "
    "portion (not per 100g — the actual amount shown). If several foods share "
    "one plate, list them separately. Also guess which meal this most likely "
    "is. Give your best estimate even when you're unsure."
)


def analyze_food_photo(image_bytes, media_type):
    """
    Send the uploaded image to Claude and return a Python dict like:
        {"items": [{"name": ..., "estimated_grams": ..., "calories": ...}],
         "meal_guess": "lunch"}
    `media_type` is something like "image/jpeg" or "image/png".
    """
    # Pass the key explicitly so it works both locally (.env) and on Streamlit
    # Cloud (Secrets box) — get_secret checks both places.
    client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))

    # Images are sent as base64 text — a way of writing binary data as letters.
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": PHOTO_PROMPT},
                ],
            }
        ],
        # This is what guarantees Claude replies in our JSON shape.
        output_config={"format": {"type": "json_schema", "schema": FOOD_SCHEMA}},
    )

    # The reply's first text block is guaranteed-valid JSON, so we parse it.
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


TEXT_PROMPT = (
    "You are a nutrition assistant. The user describes what they ate in plain "
    "language — it may include local or brand names (e.g. 'Ya Kun kaya toast "
    "set'), portion hints ('half', 'shared with a friend'), and drinks. "
    "Identify each distinct food/drink item and estimate the portion in grams "
    "and the calories, protein, carbs and fat for the amount THEY actually ate "
    "(account for sharing / half portions). Use your knowledge of common foods "
    "and brands, including Singaporean dishes. Also guess which meal it is. "
    "Give your best estimate even when you're unsure."
)


def analyze_food_text(description):
    """Send a plain-language food description to Claude and get back the same
    structured estimate shape as the photo flow (a dict with items + meal_guess)."""
    client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[
            {"role": "user", "content": f"{TEXT_PROMPT}\n\nWhat I ate: {description}"}
        ],
        output_config={"format": {"type": "json_schema", "schema": FOOD_SCHEMA}},
    )
    text_out = next(b.text for b in response.content if b.type == "text")
    return json.loads(text_out)


# Schema for reading a fitness screenshot (Apple steps/energy, or a Hevy workout).
ACTIVITY_SHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["steps", "workout", "other"]},
        "steps": {"type": "number"},        # step count, 0 if none shown
        "active_kcal": {"type": "number"},  # any calories/energy figure, 0 if none
        "workout_minutes": {"type": "number"},  # workout duration, 0 if none
        "summary": {"type": "string"},      # one-line description of what was read
    },
    "required": ["kind", "steps", "active_kcal", "workout_minutes", "summary"],
    "additionalProperties": False,
}

ACTIVITY_PROMPT = (
    "This is a screenshot from a fitness app. It is most likely either "
    "(a) an Apple Health / Apple Fitness screen showing a step count and/or an "
    "'Active Energy' / Move calories figure, or (b) a Hevy strength-training "
    "workout summary showing the workout duration and exercises. Read it and "
    "extract: the step count (0 if none shown); any calories / active-energy "
    "number in kcal (0 if none); the workout duration in minutes (0 if none); "
    "classify the kind; and write a short one-line summary of what you saw."
)


def analyze_activity_screenshot(image_bytes, media_type):
    """Read a steps or workout screenshot and return a structured dict:
    {kind, steps, active_kcal, workout_minutes, summary}."""
    client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": ACTIVITY_PROMPT},
                ],
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": ACTIVITY_SHOT_SCHEMA}},
    )
    text_out = next(b.text for b in response.content if b.type == "text")
    return json.loads(text_out)


def save_ai_items(items, meal_type, source="AI photo"):
    """
    Save a list of Claude-estimated food items to the database. `source` records
    HOW it was logged ("AI photo" or "AI text"). Unlike the USDA path, Claude
    already gives totals for the actual portion eaten, so we store those numbers
    directly (no per-100g scaling).
    """
    today = date.today().isoformat()
    with engine.begin() as conn:
        for it in items:
            grams = it["estimated_grams"] or 0

            # We also keep a per-100g row in `foods` so the foods table stays
            # consistent with the USDA entries. Guard against divide-by-zero.
            def per_100g(value):
                return round(value / grams * 100, 1) if grams else 0

            food_id = conn.execute(text("""
                INSERT INTO foods (name, calories_per_100g, protein_g, carbs_g, fat_g, source)
                VALUES (:n, :cal, :p, :c, :f, :src)
                RETURNING id
            """), {
                "n": it["name"],
                "cal": per_100g(it["calories"]),
                "p": per_100g(it["protein_g"]),
                "c": per_100g(it["carbs_g"]),
                "f": per_100g(it["fat_g"]),
                "src": source,
            }).scalar()

            conn.execute(text("""
                INSERT INTO logs
                    (date, food_id, food_name, quantity_g, meal_type,
                     calories, protein_g, carbs_g, fat_g)
                VALUES (:date, :fid, :name, :qty, :meal, :cal, :p, :c, :f)
            """), {
                "date": today,
                "fid": food_id,
                "name": it["name"],
                "qty": grams,
                "meal": meal_type,
                "cal": round(it["calories"], 1),
                "p": round(it["protein_g"], 1),
                "c": round(it["carbs_g"], 1),
                "f": round(it["fat_g"], 1),
            })


# ----------------------------------------------------------------------
# PAGE 1: LOG FOOD
# ----------------------------------------------------------------------
def page_log_food():
    st.header("🍽️ Log Food")

    # Three ways to log: snap a photo, describe it in words, or type-search USDA.
    photo_tab, describe_tab, search_tab = st.tabs(
        ["📷 Photo", "✍️ Describe", "🔍 Search"]
    )
    with photo_tab:
        _photo_tab()
    with describe_tab:
        _describe_tab()
    with search_tab:
        _search_tab()


def _review_estimate(result, state_key, source):
    """Shared UI: show Claude's estimate as an editable table, pick the meal,
    and save. Used by both the Photo and Describe tabs. `state_key` keeps each
    tab's widgets separate; `source` records how it was logged."""
    if not (result and result.get("items")):
        return

    st.write("**Claude's estimate** — edit any number, then save:")
    rows = [
        {
            "Food": it["name"],
            "Grams": it["estimated_grams"],
            "Calories": it["calories"],
            "Protein (g)": it["protein_g"],
            "Carbs (g)": it["carbs_g"],
            "Fat (g)": it["fat_g"],
        }
        for it in result["items"]
    ]
    # num_rows="dynamic" lets you delete rows you don't want.
    edited = st.data_editor(
        rows, num_rows="dynamic", hide_index=True,
        use_container_width=True, key=f"editor_{state_key}",
    )

    meals = ["breakfast", "lunch", "dinner", "snack"]
    guess = result.get("meal_guess", "lunch")
    meal_type = st.selectbox(
        "Meal type", meals,
        index=meals.index(guess) if guess in meals else 1,
        key=f"meal_{state_key}",
    )

    if st.button("Add all to log", type="primary", key=f"save_{state_key}"):
        items = [
            {
                "name": r["Food"],
                "estimated_grams": r["Grams"],
                "calories": r["Calories"],
                "protein_g": r["Protein (g)"],
                "carbs_g": r["Carbs (g)"],
                "fat_g": r["Fat (g)"],
            }
            for r in edited
        ]
        save_ai_items(items, meal_type, source)
        st.success(f"Logged {len(items)} item(s) as {meal_type}.")
        del st.session_state[state_key]  # clear so the table disappears


def _photo_tab():
    """Upload a food photo, let Claude estimate it, review, then log."""
    st.caption("Snap or upload a photo and Claude will estimate the macros.")

    uploaded = st.file_uploader("Food photo", type=["jpg", "jpeg", "png", "webp"])
    if uploaded is not None:
        st.image(uploaded, caption="Your photo", width=300)
        if st.button("Analyze photo", type="primary"):
            with st.spinner("Claude is looking at your food…"):
                try:
                    result = analyze_food_photo(uploaded.getvalue(), uploaded.type)
                    st.session_state["photo_result"] = result
                except Exception as e:
                    st.error(f"Photo analysis failed: {e}")

    _review_estimate(st.session_state.get("photo_result"), "photo_result", "AI photo")


def _describe_tab():
    """Type what you ate; Claude estimates it from its food knowledge."""
    st.caption(
        "Type what you ate (great for known dishes you didn't photograph) and "
        "Claude estimates the macros. Include portion hints like 'half' or 'shared'."
    )

    desc = st.text_area(
        "What did you eat?",
        placeholder="e.g. half a Ya Kun kaya toast set, shared — 2 kaya toast, "
        "2 soft-boiled eggs, 1 kopi",
        key="desc_text",
    )
    if st.button("Estimate", type="primary", key="desc_btn") and desc.strip():
        with st.spinner("Claude is estimating…"):
            try:
                st.session_state["text_result"] = analyze_food_text(desc)
            except Exception as e:
                st.error(f"Estimate failed: {e}")

    _review_estimate(st.session_state.get("text_result"), "text_result", "AI text")


def _search_tab():
    """The original typed USDA search flow — a backup for when you have no photo."""
    # st.text_input draws a search box. Whatever the user typed comes back here.
    query = st.text_input("Search for a food", placeholder="e.g. chicken breast")

    # Run a search when the user clicks the button (saves needless API calls).
    if st.button("Search") and query:
        try:
            # Cache results in session_state so they survive Streamlit's reruns.
            st.session_state["search_results"] = search_usda(query)
        except Exception as e:
            st.error(f"Search failed: {e}")

    results = st.session_state.get("search_results", [])

    if results:
        # Build a human-readable label for each result for the dropdown.
        labels = [
            f"{r['name']}  —  {r['calories_per_100g']:.0f} kcal, "
            f"{r['protein_g']:.1f}g P / {r['carbs_g']:.1f}g C / {r['fat_g']:.1f}g F  (per 100g)"
            for r in results
        ]
        choice_index = st.selectbox(
            "Pick a result",
            options=range(len(results)),   # we pick by position...
            format_func=lambda i: labels[i],  # ...but show the nice label.
        )
        chosen = results[choice_index]

        # Quantity + meal type inputs, side by side in two columns.
        col1, col2 = st.columns(2)
        with col1:
            quantity = st.number_input(
                "Quantity (grams)", min_value=1.0, value=100.0, step=10.0
            )
        with col2:
            meal_type = st.selectbox(
                "Meal type", ["breakfast", "lunch", "dinner", "snack"]
            )

        # Show a live preview of what will be logged for this quantity.
        factor = quantity / 100.0
        st.caption(
            f"This will log **{chosen['calories_per_100g'] * factor:.0f} kcal**, "
            f"{chosen['protein_g'] * factor:.1f}g protein, "
            f"{chosen['carbs_g'] * factor:.1f}g carbs, "
            f"{chosen['fat_g'] * factor:.1f}g fat."
        )

        if st.button("Add to log", type="primary"):
            save_food_and_log(chosen, quantity, meal_type)
            st.success(f"Logged {quantity:.0f} g of {chosen['name']} ({meal_type}).")


# ----------------------------------------------------------------------
# PAGE 2: DASHBOARD
# ----------------------------------------------------------------------
def _progress_row(label, total, target, unit):
    """Draw one metric: a number vs a single target plus a progress bar."""
    if target:  # target is set (not None / not 0)
        pct = total / target
        st.write(
            f"**{label}:** {total:.0f} / {target:.0f} {unit}  ({pct * 100:.0f}%)"
        )
        # st.progress needs a value between 0 and 1; cap at 1 so it doesn't error.
        st.progress(min(pct, 1.0))
    else:
        # No target set yet (carbs/fat) — just show the running total.
        st.write(f"**{label}:** {total:.0f} {unit}  (no target set)")


def _range_row(label, total, lo, hi, unit, kind):
    """
    Draw a metric against a min–max RANGE with a status word.
    `kind` is "ceiling" (calories — stay under the top) or "floor"
    (protein — get above the bottom).
    """
    if not hi:  # no range set — just show the total
        st.write(f"**{label}:** {total:.0f} {unit}  (no target set)")
        return

    if kind == "ceiling":
        if total > hi:
            status = f"⚠️ {total - hi:.0f} over ceiling"
        elif total >= (lo or 0):
            status = "✅ in range"
        else:
            status = f"↓ {(lo or 0) - total:.0f} below floor"
    else:  # "floor" — the goal is to reach at least `lo`
        if total >= hi:
            status = "✅ goal hit"
        elif total >= (lo or 0):
            status = "✅ floor met"
        else:
            status = f"↓ {(lo or 0) - total:.0f} to floor"

    pct = total / hi
    st.write(
        f"**{label}:** {total:.0f} / {lo:.0f}–{hi:.0f} {unit}  "
        f"({pct * 100:.0f}% of top)  {status}"
    )
    st.progress(min(pct, 1.0))


def _render_day_view(day_iso):
    """Show one day's totals (vs targets) and its entries with delete buttons.
    Shared by the Dashboard (today) and the Calendar (any day)."""
    totals = get_totals_for(day_iso)
    targets = get_active_targets()

    # Calories: a ceiling to stay under. Protein: a floor to reach.
    _range_row(
        "Calories", totals["calories"],
        targets.get("calorie_min"), targets.get("calorie_max"), "kcal", "ceiling",
    )
    _range_row(
        "Protein", totals["protein_g"],
        targets.get("protein_min"), targets.get("protein_max"), "g", "floor",
    )
    # Carbs and fat have no target yet — these just show the running total.
    _progress_row("Carbs", totals["carbs_g"], targets.get("carb_target"), "g")
    _progress_row("Fat", totals["fat_g"], targets.get("fat_target"), "g")

    # Calories burned (steps + exercise), the resulting net intake, and — when we
    # have a maintenance estimate from your profile — the day's calorie deficit.
    out_kcal, act = calories_out_for(day_iso)
    food = totals["calories"]
    if out_kcal > 0:
        st.caption(
            f"Activity out: ~{out_kcal:.0f} kcal "
            f"({act['steps']:,} steps + {act['exercise_kcal']:.0f} exercise)  ·  "
            f"**Net intake: {food - out_kcal:.0f} kcal**"
        )

    if targets.get("dynamic") and food > 0:
        total_burn = targets["tdee"] + out_kcal           # maintenance + extra activity
        deficit = total_burn - food                       # +ve = losing, -ve = surplus
        fat_g = abs(deficit) / 7.7                         # ~7700 kcal per kg of fat
        if deficit >= 0:
            st.success(
                f"**Deficit so far today: ~{deficit:.0f} kcal** (≈ {fat_g:.0f} g fat)\n\n"
                f"Burn ≈ {total_burn:.0f} (maintenance {targets['tdee']:.0f} + "
                f"activity {out_kcal:.0f}) − food {food:.0f}."
            )
        else:
            st.warning(
                f"**Surplus so far today: +{-deficit:.0f} kcal** over maintenance "
                f"(≈ {fat_g:.0f} g fat). Burn ≈ {total_burn:.0f} − food {food:.0f}."
            )
        st.caption("Running estimate — most accurate once the day's food is fully logged.")

    # A list of everything logged that day, each with a delete button.
    st.subheader("Entries")
    logs = get_logs_for(day_iso)
    if logs:
        for row in logs:
            log_id, meal, food, qty, cal, prot, carb, fat = row
            # Two columns: the entry text, and a small delete button.
            text_col, btn_col = st.columns([6, 1])
            with text_col:
                st.write(
                    f"**{meal.title()}** — {food}  ·  {qty:.0f} g  ·  "
                    f"{cal:.0f} kcal  ·  {prot:.0f}P / {carb:.0f}C / {fat:.0f}F"
                )
            with btn_col:
                # key must be unique per row, so we use the log id.
                if st.button("🗑️", key=f"del_{log_id}", help="Delete this entry"):
                    delete_log(log_id)
                    st.rerun()  # reload the page so totals + list update
    else:
        st.info("Nothing logged on this day.")


def _trip_panel():
    """Days until the trip + a straight-line weight projection at your current rate."""
    profile = get_profile()
    if not profile or not profile.get("trip_date"):
        return
    trip = date.fromisoformat(profile["trip_date"])
    days = (trip - date.today()).days
    if days < 0:
        return

    st.subheader("🏖️ Trip countdown")
    st.write(f"**{days} days** until {trip.strftime('%d %b %Y')}.")

    weights = get_weights()
    if len(weights) >= 2:
        d0, w0 = date.fromisoformat(weights[0][0]), weights[0][1]
        d1, w1 = date.fromisoformat(weights[-1][0]), weights[-1][1]
        span = (d1 - d0).days
        if span > 0 and w0 and w1:
            kg_per_week = (w1 - w0) / span * 7
            proj = health.project_weight(w1, kg_per_week, days)
            st.write(
                f"At your current trend (**{kg_per_week:+.2f} kg/week**), "
                f"projected ~**{proj:.1f} kg** by your trip."
            )
            if profile.get("goal_weight_kg"):
                st.write(f"Goal: {profile['goal_weight_kg']:.1f} kg.")
    else:
        st.caption("Log at least two weigh-ins to see a projection.")


def page_dashboard():
    st.header("📊 Dashboard")
    st.caption(f"Today: {date.today().isoformat()}")

    targets = get_active_targets()
    if targets.get("dynamic"):
        st.caption(
            f"🎯 Targets auto-set from your latest weight "
            f"**{targets['weight']:.1f} kg** — est. maintenance "
            f"{targets['tdee']:.0f} kcal − {targets['deficit']:.0f} deficit."
        )
    else:
        st.caption(
            "🎯 Using fixed targets. Fill in **Profile** and log a weight under "
            "**Body & Activity** to switch to auto-adjusting targets."
        )

    _render_day_view(date.today().isoformat())
    _trip_panel()


# ----------------------------------------------------------------------
# PAGE: PROFILE & GOALS
# ----------------------------------------------------------------------
def page_profile():
    st.header("⚙️ Profile & goals")
    st.caption("These drive your auto-adjusting calorie & protein targets.")
    p = get_profile() or {}

    col1, col2 = st.columns(2)
    with col1:
        height = st.number_input(
            "Height (cm)", 100.0, 230.0, value=float(p.get("height_cm") or 170.0), step=0.5
        )
        age = st.number_input("Age", 12, 100, value=int(p.get("age") or 25))
        sexes = ["male", "female"]
        sex = st.selectbox(
            "Sex (for the calorie formula)", sexes,
            index=sexes.index(p["sex"]) if p.get("sex") in sexes else 0,
        )
    with col2:
        labels = list(health.ACTIVITY_FACTORS.keys())
        cur_factor = p.get("activity_factor") or health.ACTIVITY_FACTORS[labels[1]]
        cur_label = next(
            (l for l, f in health.ACTIVITY_FACTORS.items() if abs(f - cur_factor) < 1e-6),
            labels[1],
        )
        activity_label = st.selectbox(
            "Daily activity (not counting logged workouts)",
            labels, index=labels.index(cur_label),
        )
        deficit = st.number_input(
            "Daily calorie deficit", 0, 1200,
            value=int(p.get("deficit_kcal") or 500), step=50,
        )
        st.caption("~500/day ≈ 0.5 kg/week. Higher = faster, but harder to keep muscle.")

    st.divider()
    col3, col4, col5 = st.columns(3)
    with col3:
        goal_w = st.number_input(
            "Goal weight (kg, optional)", 0.0, 200.0,
            value=float(p.get("goal_weight_kg") or 0.0), step=0.5,
        )
    with col4:
        goal_bf = st.number_input(
            "Goal body fat % (optional)", 0.0, 60.0,
            value=float(p.get("goal_bodyfat_pct") or 0.0), step=0.5,
        )
    with col5:
        trip = st.date_input(
            "Trip date",
            value=date.fromisoformat(p["trip_date"]) if p.get("trip_date") else date.today(),
        )

    if st.button("Save profile", type="primary"):
        save_profile({
            "height_cm": height,
            "age": int(age),
            "sex": sex,
            "activity_factor": health.ACTIVITY_FACTORS[activity_label],
            "goal_weight_kg": goal_w or None,
            "goal_bodyfat_pct": goal_bf or None,
            "trip_date": trip.isoformat(),
            "deficit_kcal": deficit,
        })
        st.success("Profile saved. Your targets will now adjust to your weight.")


# ----------------------------------------------------------------------
# PAGE: BODY & ACTIVITY
# ----------------------------------------------------------------------
def page_body():
    st.header("⚖️ Body & activity")
    weigh_tab, activity_tab, import_tab = st.tabs(
        ["Weigh-in", "Steps & exercise", "📷 Import"]
    )

    with weigh_tab:
        col1, col2, col3 = st.columns(3)
        with col1:
            wdate = st.date_input("Date", value=date.today(), key="weigh_date")
        with col2:
            weight = st.number_input("Weight (kg)", 30.0, 250.0, value=70.0, step=0.1)
        with col3:
            bf = st.number_input("Body fat % (optional)", 0.0, 60.0, value=0.0, step=0.1)
        if st.button("Add weigh-in", type="primary"):
            add_weight(wdate.isoformat(), weight, bf or None)
            st.success(f"Logged {weight:.1f} kg on {wdate.isoformat()}.")

        weights = get_weights()
        if weights:
            df = pd.DataFrame(weights, columns=["date", "weight_kg", "body_fat_pct"])
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
            st.subheader("Weight trend")
            st.line_chart(df["weight_kg"])
            if df["body_fat_pct"].notna().any():
                st.subheader("Body fat % trend")
                st.line_chart(df["body_fat_pct"])

    with activity_tab:
        adate = st.date_input("Date", value=date.today(), key="act_date")
        current = get_activity_for(adate.isoformat())
        steps = st.number_input(
            "Steps", 0, 100000, value=int(current["steps"]), step=500
        )
        ex_kcal = st.number_input(
            "Exercise calories burned", 0, 5000,
            value=int(current["exercise_kcal"]), step=50,
            help="From your watch/app, or your best estimate.",
        )
        note = st.text_input("Workout note (optional)", value=current["note"])
        if st.button("Save activity", type="primary"):
            save_activity(adate.isoformat(), steps, ex_kcal, note)
            st.success(f"Saved activity for {adate.isoformat()}.")

        # Show today's estimated burn from steps for context.
        weight, _, _ = get_latest_weight()
        if steps:
            st.caption(
                f"~{health.steps_to_kcal(steps, weight):.0f} kcal estimated from "
                f"{steps:,} steps."
            )

    with import_tab:
        _import_tab()


def _import_tab():
    """Read a steps or Hevy-workout screenshot and turn it into a day's activity."""
    st.caption(
        "Upload a screenshot of your **Apple steps / Active Energy** or a **Hevy "
        "workout** — Claude reads it and fills in the numbers below."
    )
    idate = st.date_input("Date", value=date.today(), key="imp_date")
    shot = st.file_uploader(
        "Screenshot", type=["jpg", "jpeg", "png", "webp"], key="imp_shot"
    )
    if shot is not None:
        st.image(shot, width=240)
        if st.button("Read screenshot", type="primary", key="imp_read"):
            with st.spinner("Claude is reading your screenshot…"):
                try:
                    st.session_state["activity_shot"] = analyze_activity_screenshot(
                        shot.getvalue(), shot.type
                    )
                except Exception as e:
                    st.error(f"Couldn't read it: {e}")

    res = st.session_state.get("activity_shot")
    if not res:
        return

    st.info(f"Read: {res.get('summary', '')}")
    weight, _, _ = get_latest_weight()
    current = get_activity_for(idate.isoformat())

    # Suggested values from the screenshot, falling back to what's already logged.
    sugg_steps = int(res.get("steps") or 0) or int(current["steps"])
    wmin = res.get("workout_minutes") or 0
    active = res.get("active_kcal") or 0
    # Prefer a workout-duration estimate; otherwise use an on-screen calorie number.
    sugg_ex = round(health.workout_kcal(wmin, weight)) if wmin else round(active)
    sugg_ex = sugg_ex or int(current["exercise_kcal"])

    col1, col2 = st.columns(2)
    with col1:
        steps_val = st.number_input(
            "Steps", 0, 100000, value=sugg_steps, step=500, key="imp_steps"
        )
    with col2:
        ex_val = st.number_input(
            "Exercise calories", 0, 5000, value=sugg_ex, step=25, key="imp_ex"
        )
    note = st.text_input("Note", value=(res.get("summary", "") or "")[:80], key="imp_note")

    if wmin:
        st.caption(
            f"~{health.workout_kcal(wmin, weight):.0f} kcal estimated from a "
            f"{wmin:.0f}-min strength session (Hevy shows duration, not calories)."
        )
    if active and not wmin:
        st.caption(
            "Looks like an on-screen calorie figure (e.g. Apple Active Energy). "
            "If that number already covers *all* your daily movement, set Steps to "
            "0 here so walking isn't counted twice."
        )

    if st.button("Save to this day", type="primary", key="imp_save"):
        save_activity(idate.isoformat(), int(steps_val), float(ex_val), note)
        st.success(f"Saved activity for {idate.isoformat()}.")
        del st.session_state["activity_shot"]


# ----------------------------------------------------------------------
# PAGE 3: CALENDAR  (view any day)
# ----------------------------------------------------------------------
def page_calendar():
    st.header("🗓️ Calendar")
    # st.date_input pops up a real calendar to pick any day.
    chosen = st.date_input("Pick a day to view", value=date.today())
    st.caption(f"Showing {chosen.strftime('%A, %d %b %Y')}")
    _render_day_view(chosen.isoformat())


# ----------------------------------------------------------------------
# PAGE 4: WEEKLY SUMMARY  (totals across a Mon–Sun week)
# ----------------------------------------------------------------------
def page_weekly():
    st.header("📈 Weekly summary")

    # Pick any day; we show the Monday–Sunday week that contains it.
    anchor = st.date_input("Pick any day in the week", value=date.today())
    monday = anchor - timedelta(days=anchor.weekday())  # weekday(): Mon=0
    sunday = monday + timedelta(days=6)
    st.caption(f"Week of {monday.strftime('%d %b')} – {sunday.strftime('%d %b %Y')}")

    # One query pulls every logged day in the week.
    per_day = get_totals_in_range(monday.isoformat(), sunday.isoformat())

    # Build a 7-row table (Mon–Sun) and accumulate the weekly totals.
    table = []
    week_total = {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0}
    days_logged = 0
    for i in range(7):
        d = monday + timedelta(days=i)
        t = per_day.get(
            d.isoformat(),
            {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
        )
        if d.isoformat() in per_day:
            days_logged += 1
        for key in week_total:
            week_total[key] += t[key]
        table.append(
            {
                "Day": d.strftime("%a %d %b"),
                "Calories": round(t["calories"]),
                "Protein (g)": round(t["protein_g"]),
                "Carbs (g)": round(t["carbs_g"]),
                "Fat (g)": round(t["fat_g"]),
            }
        )

    st.dataframe(table, hide_index=True, use_container_width=True)

    # --- Weekly totals -------------------------------------------------
    st.subheader("Week total")
    st.write(f"**Calories:** {week_total['calories']:.0f} kcal")
    st.write(f"**Protein:** {week_total['protein_g']:.0f} g")
    st.write(f"**Carbs:** {week_total['carbs_g']:.0f} g")
    st.write(f"**Fat:** {week_total['fat_g']:.0f} g")

    # --- Daily averages (over the days you actually logged) ------------
    st.subheader("Daily average")
    if days_logged == 0:
        st.info("Nothing logged this week yet.")
        return

    avg_cal = week_total["calories"] / days_logged
    avg_prot = week_total["protein_g"] / days_logged
    st.caption(f"Averaged over {days_logged} logged day(s).")
    targets = get_targets() or {}
    # Reuse the range rows so the average is graded against your targets.
    _range_row(
        "Avg calories/day", avg_cal,
        targets.get("calorie_min"), targets.get("calorie_max"), "kcal", "ceiling",
    )
    _range_row(
        "Avg protein/day", avg_prot,
        targets.get("protein_min"), targets.get("protein_max"), "g", "floor",
    )


# ----------------------------------------------------------------------
# APP ENTRY POINT — sidebar navigation between the two pages.
# ----------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Calorie & Macro Tracker", page_icon="🥗")
    st.title("🥗 Calorie & Macro Tracker")

    # Make sure the tables exist (creates them on first run in the cloud).
    ensure_db()

    page = st.sidebar.radio(
        "Go to",
        ["Log Food", "Dashboard", "Body & Activity", "Calendar",
         "Weekly summary", "Profile"],
    )
    if page == "Log Food":
        page_log_food()
    elif page == "Dashboard":
        page_dashboard()
    elif page == "Body & Activity":
        page_body()
    elif page == "Calendar":
        page_calendar()
    elif page == "Weekly summary":
        page_weekly()
    else:
        page_profile()


if __name__ == "__main__":
    main()
