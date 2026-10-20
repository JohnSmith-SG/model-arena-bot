"""SQLite: пользователи и их выбор модели, раунды (вопрос → 3 ответа), оценки, статистика.

Раунд ≈ 7 КБ (вопрос + 3 ответа по ~1000 знаков), 1000 раундов ≈ 7 МБ — для сервера мелочь.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# В Docker база живёт в томе /data (ARENA_DATA_DIR), чтобы переживать обновление контейнера
DATA_DIR = Path(os.environ.get("ARENA_DATA_DIR") or Path(__file__).parent)
DB = DATA_DIR / "arena.db"
MSK = timezone(timedelta(hours=3))  # «день» для лимита — по Москве


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, name TEXT, model_key TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, topic TEXT,
            question TEXT, summarized INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER, slot INTEGER,
            model_key TEXT, text TEXT, seconds REAL, input_tokens INTEGER,
            output_tokens INTEGER, cost REAL, error TEXT, rating INTEGER, rated_at TEXT);
        CREATE INDEX IF NOT EXISTS answers_round ON answers(round_id);
        CREATE INDEX IF NOT EXISTS rounds_user ON rounds(user_id, created_at);
        """)


# ---------- пользователи ----------

def touch_user(user_id: int, name: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO users (user_id, name) VALUES (?, ?) "
                  "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name", (user_id, name))


def get_model(user_id: int) -> str | None:
    with _conn() as c:
        r = c.execute("SELECT model_key FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return r["model_key"] if r else None


def set_model(user_id: int, model_key: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET model_key = ? WHERE user_id = ?", (model_key, user_id))


def rounds_today(user_id: int) -> int:
    start = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM rounds WHERE user_id = ? AND created_at >= ?",
                         (user_id, start_utc)).fetchone()[0]


# ---------- раунды и ответы ----------

def new_round(user_id: int, topic: str | None, question: str) -> int:
    with _conn() as c:
        return c.execute("INSERT INTO rounds (user_id, topic, question) VALUES (?, ?, ?)",
                         (user_id, topic, question)).lastrowid


def recent_questions(topic: str, limit: int = 15) -> list[str]:
    with _conn() as c:
        return [r[0] for r in c.execute(
            "SELECT question FROM rounds WHERE topic = ? ORDER BY id DESC LIMIT ?", (topic, limit))]


def add_answer(round_id: int, slot: int, model_key: str, a) -> int:
    with _conn() as c:
        return c.execute(
            "INSERT INTO answers (round_id, slot, model_key, text, seconds, input_tokens, "
            "output_tokens, cost, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (round_id, slot, model_key, a.text, a.seconds, a.input_tokens, a.output_tokens,
             a.cost, a.error)).lastrowid


def rate(answer_id: int, user_id: int, score: int) -> int | None:
    """Сохраняет оценку. Возвращает id раунда, если ответ принадлежит этому пользователю."""
    with _conn() as c:
        r = c.execute("SELECT a.round_id FROM answers a JOIN rounds r ON r.id = a.round_id "
                      "WHERE a.id = ? AND r.user_id = ?", (answer_id, user_id)).fetchone()
        if not r:
            return None
        c.execute("UPDATE answers SET rating = ?, rated_at = CURRENT_TIMESTAMP WHERE id = ?",
                  (score, answer_id))
        return r["round_id"]


def get_round(round_id: int) -> sqlite3.Row:
    with _conn() as c:
        return c.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()


def round_answers(round_id: int) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM answers WHERE round_id = ? ORDER BY slot", (round_id,)).fetchall()


def mark_summarized(round_id: int) -> bool:
    """True — если итог раунда ещё не отправляли (защита от двойного итога)."""
    with _conn() as c:
        return c.execute("UPDATE rounds SET summarized = 1 WHERE id = ? AND summarized = 0",
                         (round_id,)).rowcount == 1


# ---------- статистика ----------

def model_stats(user_id: int | None = None) -> list[sqlite3.Row]:
    """По каждой модели: число оценок, средняя оценка, среднее время и цена ответа."""
    where, args = ("AND r.user_id = ?", (user_id,)) if user_id else ("", ())
    with _conn() as c:
        return c.execute(f"""
            SELECT a.model_key, COUNT(*) AS n, AVG(a.rating) AS avg_rating,
                   AVG(a.seconds) AS avg_seconds, AVG(a.cost) AS avg_cost
            FROM answers a JOIN rounds r ON r.id = a.round_id
            WHERE a.rating IS NOT NULL {where}
            GROUP BY a.model_key ORDER BY avg_rating DESC, n DESC""", args).fetchall()


def topic_stats() -> list[sqlite3.Row]:
    """По каждой теме и модели — средняя оценка (для «лучшей модели в теме»)."""
    with _conn() as c:
        return c.execute("""
            SELECT r.topic, a.model_key, COUNT(*) AS n, AVG(a.rating) AS avg_rating
            FROM answers a JOIN rounds r ON r.id = a.round_id
            WHERE a.rating IS NOT NULL
            GROUP BY r.topic, a.model_key""").fetchall()


def totals(user_id: int | None = None) -> sqlite3.Row:
    where, args = ("WHERE user_id = ?", (user_id,)) if user_id else ("", ())
    with _conn() as c:
        return c.execute(f"SELECT COUNT(*) AS rounds, COUNT(DISTINCT user_id) AS users "
                         f"FROM rounds {where}", args).fetchone()
