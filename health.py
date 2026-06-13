"""
health.py
---------
Pure calculation helpers for the body-composition side of the app: estimating
how many calories you burn and turning that into daily calorie & protein
targets, plus simple weight projection.

These are ESTIMATES from standard formulas (Mifflin-St Jeor for resting burn),
good enough to steer a cut by — not lab-measured. There's no database or UI
in here, just maths, so it's easy to read and test on its own.
"""

# How active you are day-to-day, NOT counting workouts you log separately.
# The number is the multiplier applied to your resting burn (BMR).
ACTIVITY_FACTORS = {
    "Sedentary (desk job, little walking)": 1.2,
    "Light (on your feet a bit / light exercise)": 1.375,
    "Moderate (active job or regular exercise)": 1.55,
    "Very active (hard training / physical job)": 1.725,
}

CALORIE_BAND = 350           # the daily calorie range is [ceiling - band, ceiling]
PROTEIN_PER_KG_LBM_MIN = 2.2  # grams of protein per kg of LEAN mass (floor)
PROTEIN_PER_KG_LBM_MAX = 2.5  # grams per kg of lean mass (goal)
PROTEIN_PER_KG_BW = 1.9       # fallback per kg of BODY weight when body-fat unknown


def bmr_mifflin(weight_kg, height_cm, age, sex):
    """Resting calories burned per day (Mifflin-St Jeor equation)."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if sex == "male" else base - 161


def tdee(weight_kg, height_cm, age, sex, activity_factor):
    """Maintenance calories per day = resting burn x your daily-activity multiplier."""
    return bmr_mifflin(weight_kg, height_cm, age, sex) * activity_factor


def lean_body_mass(weight_kg, body_fat_pct):
    """Kilograms of you that isn't fat. Returns None if body fat is unknown."""
    if body_fat_pct is None:
        return None
    return weight_kg * (1 - body_fat_pct / 100.0)


def calorie_targets(maintenance, deficit):
    """(min, max) calories to eat. max is the ceiling = maintenance - deficit.
    Floors are clamped so the app never suggests dangerously low intake."""
    ceiling = max(maintenance - deficit, 1200)
    floor = max(ceiling - CALORIE_BAND, 1000)
    return round(floor), round(ceiling)


def protein_targets(weight_kg, body_fat_pct):
    """(min, max) grams of protein per day. Uses lean mass when body-fat is known
    (more accurate on a cut), otherwise scales off total body weight."""
    lbm = lean_body_mass(weight_kg, body_fat_pct)
    if lbm is not None:
        return round(lbm * PROTEIN_PER_KG_LBM_MIN), round(lbm * PROTEIN_PER_KG_LBM_MAX)
    return round(weight_kg * PROTEIN_PER_KG_BW), round(weight_kg * (PROTEIN_PER_KG_BW + 0.3))


def steps_to_kcal(steps, weight_kg):
    """Rough calories burned from walking `steps`, scaled to body weight."""
    if not steps:
        return 0.0
    return steps * 0.04 * ((weight_kg or 70) / 70.0)


# Approx METs (metabolic equivalent) for vigorous resistance training.
STRENGTH_MET = 5.0


def workout_kcal(minutes, weight_kg, met=STRENGTH_MET):
    """Estimated calories burned for a strength session of `minutes` minutes.
    kcal = METs x body-weight(kg) x hours. (Hevy shows duration, not calories.)"""
    if not minutes:
        return 0.0
    return met * (weight_kg or 70) * (minutes / 60.0)


def project_weight(latest_kg, kg_per_week, days_ahead):
    """Straight-line projection of weight `days_ahead` from now."""
    return latest_kg + kg_per_week * (days_ahead / 7.0)
