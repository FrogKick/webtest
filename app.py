from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, flash, g, jsonify, redirect, render_template, request, url_for


BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "data.db"


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-word-trainer-secret")
    app.config["DATABASE"] = str(DATABASE)

    @app.before_request
    def before_request() -> None:
        g.db = get_db()

    @app.teardown_appcontext
    def close_db(_error: Exception | None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def inject_now() -> dict:
        return {"now": datetime.now(), "asset_url": asset_url}

    @app.route("/")
    def index():
        stats = get_stats()
        lists = query_all(
            """
            SELECT l.*,
                   COUNT(w.id) AS total_words,
                   COALESCE(SUM(CASE WHEN w.box >= 3 THEN 1 ELSE 0 END), 0) AS learned_words
            FROM word_lists l
            LEFT JOIN words w ON w.list_id = l.id
            GROUP BY l.id
            ORDER BY l.created_at DESC
            """
        )
        recent = query_all(
            """
            SELECT w.*, l.title AS list_title
            FROM words w
            JOIN word_lists l ON l.id = w.list_id
            ORDER BY w.updated_at DESC
            LIMIT 6
            """
        )
        return render_template("index.html", stats=stats, lists=lists, recent=recent, page="home")

    @app.route("/library")
    def library():
        lists = query_all(
            """
            SELECT l.*,
                   COUNT(w.id) AS total_words,
                   COALESCE(SUM(CASE WHEN w.box >= 3 THEN 1 ELSE 0 END), 0) AS learned_words,
                   COALESCE(SUM(w.correct_count), 0) AS total_correct,
                   COALESCE(SUM(w.wrong_count), 0) AS total_wrong
            FROM word_lists l
            LEFT JOIN words w ON w.list_id = l.id
            GROUP BY l.id
            ORDER BY l.created_at DESC
            """
        )
        return render_template("library.html", lists=lists, page="library")

    @app.route("/lists/new", methods=["POST"])
    def create_list():
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        if not title:
            flash("Введите название списка.", "error")
            return redirect(url_for("library"))
        execute(
            "INSERT INTO word_lists (title, description, created_at) VALUES (?, ?, ?)",
            (title, description, datetime_iso()),
        )
        flash("Список создан.", "success")
        return redirect(url_for("library"))

    @app.route("/lists/<int:list_id>")
    def list_detail(list_id: int):
        word_list = query_one("SELECT * FROM word_lists WHERE id = ?", (list_id,))
        if word_list is None:
            flash("Список не найден.", "error")
            return redirect(url_for("library"))
        words = query_all("SELECT * FROM words WHERE list_id = ? ORDER BY created_at DESC", (list_id,))
        return render_template("list_detail.html", word_list=word_list, words=words, page="library")

    @app.route("/lists/<int:list_id>/words/new", methods=["POST"])
    def create_word(list_id: int):
        word = request.form.get("word", "").strip()
        meaning = request.form.get("meaning", "").strip()
        example = request.form.get("example", "").strip()
        note = request.form.get("note", "").strip()
        if not word or not meaning:
            flash("Заполните слово и закрытую часть со значением.", "error")
            return redirect(url_for("list_detail", list_id=list_id))
        execute(
            """
            INSERT INTO words
                (list_id, word, meaning, example, note, box, correct_count, wrong_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?)
            """,
            (list_id, word, meaning, example, note, datetime_iso(), datetime_iso()),
        )
        flash("Слово добавлено.", "success")
        return redirect(url_for("list_detail", list_id=list_id))

    @app.route("/words/<int:word_id>/delete", methods=["POST"])
    def delete_word(word_id: int):
        row = query_one("SELECT list_id FROM words WHERE id = ?", (word_id,))
        if row is None:
            flash("Слово не найдено.", "error")
            return redirect(url_for("library"))
        execute("DELETE FROM review_events WHERE word_id = ?", (word_id,))
        execute("DELETE FROM words WHERE id = ?", (word_id,))
        flash("Слово удалено.", "success")
        return redirect(url_for("list_detail", list_id=row["list_id"]))

    @app.route("/study")
    def study_select():
        lists = query_all(
            """
            SELECT l.*, COUNT(w.id) AS total_words
            FROM word_lists l
            LEFT JOIN words w ON w.list_id = l.id
            GROUP BY l.id
            HAVING total_words > 0
            ORDER BY l.created_at DESC
            """
        )
        return render_template("study_select.html", lists=lists, page="study")

    @app.route("/study/<int:list_id>")
    def study(list_id: int):
        word_list = query_one("SELECT * FROM word_lists WHERE id = ?", (list_id,))
        if word_list is None:
            flash("Список не найден.", "error")
            return redirect(url_for("study_select"))
        words = query_all(
            """
            SELECT * FROM words
            WHERE list_id = ?
            ORDER BY box ASC, wrong_count DESC, updated_at ASC
            """,
            (list_id,),
        )
        return render_template("study.html", word_list=word_list, words=words, page="study")

    @app.route("/api/review", methods=["POST"])
    def api_review():
        payload = request.get_json(force=True, silent=True) or {}
        word_id = int(payload.get("word_id", 0))
        result = payload.get("result")
        if result not in {"known", "unknown"}:
            return jsonify({"error": "Некорректный результат"}), 400
        word = query_one("SELECT * FROM words WHERE id = ?", (word_id,))
        if word is None:
            return jsonify({"error": "Слово не найдено"}), 404

        if result == "known":
            new_box = min(5, int(word["box"]) + 1)
            execute(
                """
                UPDATE words
                SET box = ?, correct_count = correct_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (new_box, datetime_iso(), word_id),
            )
            correct = 1
        else:
            new_box = max(0, int(word["box"]) - 1)
            execute(
                """
                UPDATE words
                SET box = ?, wrong_count = wrong_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (new_box, datetime_iso(), word_id),
            )
            correct = 0

        execute(
            "INSERT INTO review_events (word_id, list_id, result, created_at) VALUES (?, ?, ?, ?)",
            (word_id, word["list_id"], result, datetime_iso()),
        )
        stats = get_stats()
        return jsonify({"ok": True, "box": new_box, "correct": correct, "stats": stats})

    @app.route("/statistics")
    def statistics():
        stats = get_stats()
        lists = query_all(
            """
            SELECT l.title,
                   COUNT(w.id) AS total_words,
                   COALESCE(SUM(CASE WHEN w.box >= 3 THEN 1 ELSE 0 END), 0) AS learned_words,
                   COALESCE(SUM(w.correct_count), 0) AS total_correct,
                   COALESCE(SUM(w.wrong_count), 0) AS total_wrong
            FROM word_lists l
            LEFT JOIN words w ON w.list_id = l.id
            GROUP BY l.id
            ORDER BY total_words DESC
            """
        )
        events = query_all(
            """
            SELECT DATE(created_at) AS day,
                   COUNT(*) AS reviews,
                   SUM(CASE WHEN result = 'known' THEN 1 ELSE 0 END) AS known
            FROM review_events
            GROUP BY DATE(created_at)
            ORDER BY day DESC
            LIMIT 14
            """
        )
        return render_template("statistics.html", stats=stats, lists=lists, events=events, page="statistics")

    @app.route("/profile", methods=["GET", "POST"])
    def profile():
        profile_row = get_profile()
        if request.method == "POST":
            name = request.form.get("name", "").strip() or "Ученик"
            goal = request.form.get("goal", "").strip()
            daily_target = safe_int(request.form.get("daily_target"), 10)
            language_pair = request.form.get("language_pair", "").strip() or "Любой язык"
            execute(
                """
                UPDATE profile
                SET name = ?, goal = ?, daily_target = ?, language_pair = ?, updated_at = ?
                WHERE id = 1
                """,
                (name, goal, daily_target, language_pair, datetime_iso()),
            )
            flash("Профиль обновлён.", "success")
            return redirect(url_for("profile"))
        return render_template("profile.html", profile=profile_row, stats=get_stats(), page="profile")

    @app.route("/export")
    def export_data():
        data = {
            "profile": dict(get_profile()),
            "lists": [dict(row) for row in query_all("SELECT * FROM word_lists ORDER BY id")],
            "words": [dict(row) for row in query_all("SELECT * FROM words ORDER BY id")],
            "exported_at": datetime_iso(),
        }
        return app.response_class(
            json.dumps(data, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=word_trainer_export.json"},
        )

    init_db(app)
    return app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        g.db = db
    return g.db


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    cur = g.db.execute(sql, params)
    g.db.commit()
    return cur


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return g.db.execute(sql, params).fetchone()


def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return g.db.execute(sql, params).fetchall()


def datetime_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def safe_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or default)
        return max(1, min(parsed, 200))
    except ValueError:
        return default


def init_db(app: Flask) -> None:
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with app.app_context():
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT NOT NULL,
                goal TEXT NOT NULL DEFAULT '',
                daily_target INTEGER NOT NULL DEFAULT 10,
                language_pair TEXT NOT NULL DEFAULT 'Английский → русский',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS word_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                meaning TEXT NOT NULL,
                example TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                box INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0,
                wrong_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (list_id) REFERENCES word_lists(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS review_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word_id INTEGER NOT NULL,
                list_id INTEGER NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE,
                FOREIGN KEY (list_id) REFERENCES word_lists(id) ON DELETE CASCADE
            );
            """
        )
        profile_count = db.execute("SELECT COUNT(*) FROM profile").fetchone()[0]
        if profile_count == 0:
            now = datetime_iso()
            db.execute(
                """
                INSERT INTO profile (id, name, goal, daily_target, language_pair, created_at, updated_at)
                VALUES (1, 'Ученик', 'Пополнять активный словарь каждый день', 10, 'Английский → русский', ?, ?)
                """,
                (now, now),
            )
        list_count = db.execute("SELECT COUNT(*) FROM word_lists").fetchone()[0]
        if list_count == 0:
            seed_demo(db)
        db.commit()
        db.close()


def seed_demo(db: sqlite3.Connection) -> None:
    now = datetime_iso()
    cur = db.execute(
        "INSERT INTO word_lists (title, description, created_at) VALUES (?, ?, ?)",
        ("Стартовый набор", "Несколько слов для первой проверки тренажёра", now),
    )
    list_id = cur.lastrowid
    words = [
        ("resilient", "устойчивый, способный быстро восстановиться", "A resilient team adapts after setbacks.", "Подходит для описания систем и людей."),
        ("warehouse", "склад", "The warehouse uses barcode scanning.", "Полезно для логистики и автоматизации."),
        ("requirement", "требование", "The requirement must be clear and testable.", "Часто используется в аналитике."),
        ("schedule", "расписание, график", "We need to update the project schedule.", "В британском и американском произношении звучит по-разному."),
        ("insight", "понимание, ценный вывод", "The report gave us a useful insight.", "Не просто факт, а вывод из данных."),
    ]
    db.executemany(
        """
        INSERT INTO words
            (list_id, word, meaning, example, note, box, correct_count, wrong_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?)
        """,
        [(list_id, word, meaning, example, note, now, now) for word, meaning, example, note in words],
    )


def get_profile() -> sqlite3.Row:
    return query_one("SELECT * FROM profile WHERE id = 1")


def get_stats() -> dict:
    total_words = query_one("SELECT COUNT(*) AS value FROM words")["value"]
    total_lists = query_one("SELECT COUNT(*) AS value FROM word_lists")["value"]
    learned = query_one("SELECT COUNT(*) AS value FROM words WHERE box >= 3")["value"]
    total_reviews = query_one("SELECT COUNT(*) AS value FROM review_events")["value"]
    correct_reviews = query_one("SELECT COUNT(*) AS value FROM review_events WHERE result = 'known'")["value"]
    today_reviews = query_one("SELECT COUNT(*) AS value FROM review_events WHERE DATE(created_at) = DATE('now', 'localtime')")["value"]
    accuracy = round((correct_reviews / total_reviews) * 100) if total_reviews else 0
    progress = round((learned / total_words) * 100) if total_words else 0
    return {
        "total_words": total_words,
        "total_lists": total_lists,
        "learned": learned,
        "total_reviews": total_reviews,
        "correct_reviews": correct_reviews,
        "today_reviews": today_reviews,
        "accuracy": accuracy,
        "progress": progress,
    }


def asset_url(filename: str) -> str:
    """Return a static asset URL that works locally and behind the preview port proxy."""
    prefix = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
    if prefix:
        return f"{prefix}/static/{filename.lstrip('/')}"
    if request.path.startswith("/port/"):
        parts = request.path.strip("/").split("/")
        if len(parts) >= 2:
            return f"/{parts[0]}/{parts[1]}/static/{filename.lstrip('/')}"
    return url_for("static", filename=filename)


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
