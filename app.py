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
import sqlite3
from datetime import date

import anthropic         # the Claude SDK — analyses food photos
import requests          # for calling the USDA web API
import streamlit as st   # the web-app framework
from dotenv import load_dotenv  # reads your API keys out of the .env file

# Load variables from the .env file into the environment so we can read them.
load_dotenv()
USDA_API_KEY = os.getenv("USDA_API_KEY", "DEMO_KEY")  # falls back to DEMO_KEY
DB_NAME = "foodtracker.db"

# The USDA FoodData Central search endpoint.
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

# The Claude model we use to look at photos. Opus 4.8 is Anthropic's most
# capable model and supports vision (reading images).
CLAUDE_MODEL = "claude-opus-4-8"


# ----------------------------------------------------------------------
# DATABASE HELPERS
# Small functions so the page code below stays readable.
# ----------------------------------------------------------------------
def get_connection():
    """Open a connection to the SQLite file."""
    return sqlite3.connect(DB_NAME)


def get_targets():
    """Return the single targets row as a dict (or None if missing)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT calorie_min, calorie_max, protein_min, protein_max, carb_target, fat_target
        FROM targets LIMIT 1
        """
    )
    row = cur.fetchone()
    conn.close()
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


def get_today_totals():
    """Sum up calories/macros for everything logged today."""
    today = date.today().isoformat()  # e.g. "2026-06-13"
    conn = get_connection()
    cur = conn.cursor()
    # COALESCE(..., 0) turns a NULL sum (no rows yet) into 0.
    cur.execute(
        """
        SELECT
            COALESCE(SUM(calories), 0),
            COALESCE(SUM(protein_g), 0),
            COALESCE(SUM(carbs_g), 0),
            COALESCE(SUM(fat_g), 0)
        FROM logs
        WHERE date = ?
        """,
        (today,),
    )
    row = cur.fetchone()
    conn.close()
    return {
        "calories": row[0],
        "protein_g": row[1],
        "carbs_g": row[2],
        "fat_g": row[3],
    }


def get_today_logs():
    """Return today's individual log rows, newest first, for a small table."""
    today = date.today().isoformat()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT meal_type, food_name, quantity_g, calories, protein_g, carbs_g, fat_g
        FROM logs
        WHERE date = ?
        ORDER BY id DESC
        """,
        (today,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def save_food_and_log(food, quantity_g, meal_type):
    """
    Save the chosen food into `foods` (if not already there) and add a row to
    `logs` with the macros scaled to the quantity actually eaten.
    `food` is a dict we build from the USDA result.
    """
    conn = get_connection()
    cur = conn.cursor()

    # Reuse an existing foods row if we already stored this exact food+source,
    # otherwise insert a new one. This keeps the foods table tidy.
    cur.execute(
        "SELECT id FROM foods WHERE name = ? AND source = ?",
        (food["name"], food["source"]),
    )
    existing = cur.fetchone()
    if existing:
        food_id = existing[0]
    else:
        cur.execute(
            """
            INSERT INTO foods (name, calories_per_100g, protein_g, carbs_g, fat_g, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                food["name"],
                food["calories_per_100g"],
                food["protein_g"],
                food["carbs_g"],
                food["fat_g"],
                food["source"],
            ),
        )
        food_id = cur.lastrowid

    # Scale per-100g values to the amount eaten. factor = grams / 100.
    factor = quantity_g / 100.0
    cur.execute(
        """
        INSERT INTO logs
            (date, food_id, food_name, quantity_g, meal_type,
             calories, protein_g, carbs_g, fat_g)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date.today().isoformat(),
            food_id,
            food["name"],
            quantity_g,
            meal_type,
            round(food["calories_per_100g"] * factor, 1),
            round(food["protein_g"] * factor, 1),
            round(food["carbs_g"] * factor, 1),
            round(food["fat_g"] * factor, 1),
        ),
    )
    conn.commit()
    conn.close()


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
    # anthropic.Anthropic() automatically reads ANTHROPIC_API_KEY from the
    # environment (which load_dotenv put there from your .env file).
    client = anthropic.Anthropic()

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


def save_photo_items(items, meal_type):
    """
    Save a list of photo-derived food items to the database.
    Unlike the USDA path, Claude already gives us the totals for the actual
    portion eaten, so we store those numbers directly (no per-100g scaling).
    """
    conn = get_connection()
    cur = conn.cursor()
    today = date.today().isoformat()

    for it in items:
        grams = it["estimated_grams"] or 0

        # We also keep a per-100g row in `foods` so the foods table stays
        # consistent with the USDA entries. Guard against divide-by-zero.
        def per_100g(value):
            return round(value / grams * 100, 1) if grams else 0

        cur.execute(
            """
            INSERT INTO foods (name, calories_per_100g, protein_g, carbs_g, fat_g, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                it["name"],
                per_100g(it["calories"]),
                per_100g(it["protein_g"]),
                per_100g(it["carbs_g"]),
                per_100g(it["fat_g"]),
                "AI photo",
            ),
        )
        food_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO logs
                (date, food_id, food_name, quantity_g, meal_type,
                 calories, protein_g, carbs_g, fat_g)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                today,
                food_id,
                it["name"],
                grams,
                meal_type,
                round(it["calories"], 1),
                round(it["protein_g"], 1),
                round(it["carbs_g"], 1),
                round(it["fat_g"], 1),
            ),
        )

    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# PAGE 1: LOG FOOD
# ----------------------------------------------------------------------
def page_log_food():
    st.header("🍽️ Log Food")

    # st.tabs creates clickable tabs. The photo tab is first (your main flow);
    # the search tab is the typed backup.
    photo_tab, search_tab = st.tabs(["📷 Photo", "🔍 Search"])
    with photo_tab:
        _photo_tab()
    with search_tab:
        _search_tab()


def _photo_tab():
    """Upload a food photo, let Claude estimate it, review, then log."""
    st.caption("Snap or upload a photo and Claude will estimate the macros.")

    uploaded = st.file_uploader(
        "Food photo", type=["jpg", "jpeg", "png", "webp"]
    )

    if uploaded is not None:
        # Show the picture back to the user.
        st.image(uploaded, caption="Your photo", width=300)

        if st.button("Analyze photo", type="primary"):
            with st.spinner("Claude is looking at your food…"):
                try:
                    image_bytes = uploaded.getvalue()  # raw bytes of the file
                    result = analyze_food_photo(image_bytes, uploaded.type)
                    st.session_state["photo_result"] = result
                except Exception as e:
                    st.error(f"Photo analysis failed: {e}")

    # If we have an analysis, show it as an EDITABLE table so you can correct
    # Claude's estimates before saving.
    result = st.session_state.get("photo_result")
    if result and result.get("items"):
        st.write("**Claude's estimate** — edit any number, then save:")
        # Turn the items into friendly-named rows for the editor.
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
            rows, num_rows="dynamic", hide_index=True, use_container_width=True
        )

        # Default the meal dropdown to Claude's guess.
        meals = ["breakfast", "lunch", "dinner", "snack"]
        guess = result.get("meal_guess", "lunch")
        meal_type = st.selectbox(
            "Meal type", meals, index=meals.index(guess) if guess in meals else 1
        )

        if st.button("Add all to log", type="primary"):
            # Convert the edited friendly rows back into the internal shape.
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
            save_photo_items(items, meal_type)
            st.success(f"Logged {len(items)} item(s) as {meal_type}.")
            # Clear so the table disappears after saving.
            del st.session_state["photo_result"]


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


def page_dashboard():
    st.header("📊 Dashboard")
    st.caption(f"Today: {date.today().isoformat()}")

    totals = get_today_totals()
    targets = get_targets() or {}

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

    # A small table of everything logged today.
    st.subheader("Today's entries")
    logs = get_today_logs()
    if logs:
        # Turn each row tuple into a dict so the table shows readable headers.
        columns = ["Meal", "Food", "Qty (g)", "Calories", "Protein", "Carbs", "Fat"]
        table = [dict(zip(columns, row)) for row in logs]
        st.dataframe(table, hide_index=True, use_container_width=True)
    else:
        st.info("Nothing logged yet today. Head to 'Log Food' to add something.")


# ----------------------------------------------------------------------
# APP ENTRY POINT — sidebar navigation between the two pages.
# ----------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Calorie & Macro Tracker", page_icon="🥗")
    st.title("🥗 Calorie & Macro Tracker")

    page = st.sidebar.radio("Go to", ["Log Food", "Dashboard"])
    if page == "Log Food":
        page_log_food()
    else:
        page_dashboard()


if __name__ == "__main__":
    main()
