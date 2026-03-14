from flask import Flask, render_template, session, request, redirect
import uuid
import json
import smtplib
from email.mime.text import MIMEText
import os
import difflib
import sqlite3
from datetime import datetime

EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY", "mealmatch_secret_key_change_later")


def load_meals():
    file_path = os.path.join(os.path.dirname(__file__), "meals.json")
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


meals = load_meals()
print("TOTAL MEALS =", len(meals))


def get_db_connection():
    conn = sqlite3.connect(os.path.join(os.getcwd(), "mealmatch.db"))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS saved_searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            goal TEXT,
            category TEXT,
            ingredients TEXT,
            searched_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            meal_id TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            UNIQUE(session_id, meal_id)
        )
    """)

    conn.commit()
    conn.close()


init_db()


def get_current_user_id():
    """
    Creates a unique ID for the visitor and stores it in the browser session.
    This is NOT an account system - it only helps keep a consistent session.
    """
    if "current_user_id" not in session:
        session["current_user_id"] = str(uuid.uuid4())

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO users (session_id) VALUES (?)",
            (session["current_user_id"],)
        )
        conn.commit()
        conn.close()

    return session["current_user_id"]


def get_all_ingredients():
    ingredients_set = set()

    for meal in meals:
        for ingredient in meal.get("ingredients", []):
            ingredients_set.add(ingredient.lower().strip())

    return sorted(list(ingredients_set))


def normalize_text(text):
    text = text.lower().strip().replace("-", " ").replace("_", " ")

    replacements = {
        "tomatoes": "tomato",
        "onions": "onion",
        "chickpeas": "chickpea",
        "eggs": "egg",
        "peppers": "pepper",
        "grapes leaves": "grape leaves"
    }

    return replacements.get(text, text)


def get_recommended_meals(session_id, limit=12):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT meal_id FROM favorites
        WHERE session_id = ?
    """, (session_id,))
    favorite_rows = cursor.fetchall()
    conn.close()

    favorite_ids = [row["meal_id"] for row in favorite_rows]

    # Get the user's favorite meals
    favorite_meals = [meal for meal in meals if meal["id"] in favorite_ids]

    # Collect ingredients from favorites
    favorite_ingredients = set()
    for meal in favorite_meals:
        for ingredient in meal.get("ingredients", []):
            favorite_ingredients.add(normalize_text(ingredient))

    recommended = []

    for meal in meals:
        if meal["id"] in favorite_ids:
            continue  # do not recommend already-favorited meals

        meal_ingredients = {
            normalize_text(ingredient)
            for ingredient in meal.get("ingredients", [])
        }

        shared_count = len(favorite_ingredients.intersection(meal_ingredients))

        if shared_count > 0:
            meal_copy = meal.copy()
            meal_copy["shared_count"] = shared_count
            recommended.append(meal_copy)

    recommended.sort(key=lambda x: x["shared_count"], reverse=True)
    return recommended[:limit] if recommended else []


def get_related_meals(current_meal, all_meals, limit=3):

    ignored_ingredients = {"salt", "black pepper", "water", "oil", "olive oil"}

    current_ingredients = {
        normalize_text(item)
        for item in current_meal.get("ingredients", [])
        if normalize_text(item) not in ignored_ingredients
    }

    related = []

    for meal in all_meals:
        if meal.get("id") == current_meal.get("id"):
            continue

        meal_ingredients = {
            normalize_text(item)
            for item in meal.get("ingredients", [])
            if normalize_text(item) not in ignored_ingredients
        }

        shared = current_ingredients.intersection(meal_ingredients)
        shared_count = len(shared)

        if shared_count > 0:
            meal_copy = meal.copy()
            meal_copy["shared_count"] = shared_count
            related.append(meal_copy)

    related.sort(key=lambda x: x["shared_count"], reverse=True)

    return related[:limit]


@app.route("/")
def home():
    # No accounts created which means we will create a unique ID for the current user if it doesn't exist
    return render_template("index.html", meals=meals)


@app.route("/search", methods=["GET"])
def search():
    goal = request.args.get("goal", "").strip()
    category = request.args.get("category", "").strip().lower()
    ingredients_raw = request.args.get("ingredients", "").strip().lower()

    user_ingredients = []
    if ingredients_raw:
        user_ingredients = [
            normalize_text(item)
            for item in ingredients_raw.split(",")
            if item.strip()
        ]

    matched_meals = []

    for meal in meals:
        meal_category = meal.get("category", "").strip().lower()
        meal_diet = meal.get("diet", "").strip().lower()
        meal_ingredients = [
            normalize_text(item)
            for item in meal.get("ingredients", [])
        ]

        # category filter
        if category and meal_category != category:
            continue

        # goal filter
        if goal:
            calories = int(meal.get("calories") or 0)

            if goal == "weight_loss":
                if not (calories <= 450 and meal_category != "dessert"):
                    continue

            elif goal == "maintenance":
                if not (451 <= calories <= 650):
                    continue

            elif goal == "muscle_gain":
                if not (calories > 650):
                    continue

        # ingredient filter
        match_count = 0

        if user_ingredients:
            for ingredient in user_ingredients:
                normalized_user = normalize_text(ingredient)

                for meal_ing in meal_ingredients:
                    normalized_meal = normalize_text(meal_ing)

                    if normalized_user == normalized_meal:
                        match_count += 1
                        break

                    if normalized_user in normalized_meal or normalized_meal in normalized_user:
                        match_count += 1
                        break

                    similarity = difflib.SequenceMatcher(
                        None, normalized_user, normalized_meal
                    ).ratio()

                    if similarity > 0.72:
                        match_count += 1
                        break

            if match_count < 1:
                continue

        meal_copy = meal.copy()
        meal_copy["match_count"] = match_count
        matched_meals.append(meal_copy)

    matched_meals.sort(key=lambda x: x["match_count"], reverse=True)

    return render_template(
        "search.html",
        matched_meals=matched_meals,
        ingredient_suggestions=get_all_ingredients(),
        selected_goal=goal,
        selected_category=category,
        ingredients_value=ingredients_raw
    )


@app.route("/save_meal/<meal_id>", methods=["POST"])
def save_meal(meal_id):
    current_user_id = get_current_user_id()
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM favorites WHERE session_id = ? AND meal_id = ?",
        (current_user_id, meal_id)
    )
    existing = cursor.fetchone()

    if existing:
        cursor.execute(
            "DELETE FROM favorites WHERE session_id = ? AND meal_id = ?",
            (current_user_id, meal_id)
        )
        popup_message = "Meal unsaved."
    else:
        cursor.execute(
            "INSERT INTO favorites (session_id, meal_id, saved_at) VALUES (?, ?, ?)",
            (current_user_id, meal_id, datetime.now().isoformat())
        )
        popup_message = "Saved to favorite meals!"

    conn.commit()
    conn.close()

    meal_found = None
    for meal in meals:
        if meal.get("id") == meal_id:
            meal_found = meal
            break

    if not meal_found:
        return "Meal not found", 404

    related_meals = get_related_meals(meal_found, meals)

    is_favorite = not existing

    return render_template(
        "meal_detail.html",
        meal=meal_found,
        related_meals=related_meals,
        is_favorite=is_favorite,
        popup_message=popup_message
    )


@app.route("/contact", methods=["GET", "POST"])
def contact():

    success = False

    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        message = request.form.get("message")

        subject = "MealMatch Feedback"

        email_body = f"""
New MealMatch Contact Message

Name: {name}
Email: {email}

-------------------------------------------------

Message:
{message}

-------------------------------------------------
"""

        msg = MIMEText(email_body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_ADDRESS
        msg["To"] = EMAIL_ADDRESS
        msg["Reply-To"] = email

        try:
            server = smtplib.SMTP("smtp.gmail.com", 587, timeout=10)
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.send_message(msg)
            server.quit()
            success = True
        except Exception as e:
            print("Email error:", e)
            success = False

    return render_template("contact.html", success=success)


@app.route("/recommended")
def recommended():
    current_user_id = get_current_user_id()
    recommended_meals = get_recommended_meals(current_user_id)

    return render_template(
        "recommended.html",
        current_user_id=current_user_id,
        meals=recommended_meals
    )


@app.route("/meal/<meal_id>")
def meal_detail(meal_id):
    # find the meal by its "id" field in meals.json
    meal_found = None
    for meal in meals:
        if meal.get("id") == meal_id:
            meal_found = meal
            break

    if not meal_found:
        return "Meal not found", 404

    related_meals = get_related_meals(meal_found, meals)

    current_user_id = get_current_user_id()

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id FROM favorites
        WHERE session_id = ? AND meal_id = ?
    """, (current_user_id, meal_id))

    favorite_row = cursor.fetchone()
    conn.close()

    is_favorite = favorite_row is not None

    popup_message = None

    return render_template("meal_detail.html", meal=meal_found, related_meals=related_meals, is_favorite=is_favorite, popup_message=popup_message)


@app.route("/favorites")
def favorites():
    current_user_id = get_current_user_id()

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT meal_id
        FROM favorites
        WHERE session_id = ?
        ORDER BY saved_at DESC
    """, (current_user_id,))

    favorite_rows = cursor.fetchall()
    conn.close()

    favorite_ids = [row["meal_id"] for row in favorite_rows]

    favorite_meals = [meal for meal in meals if meal["id"] in favorite_ids]

    return render_template(
        "favorites.html",
        meals=favorite_meals
    )


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
