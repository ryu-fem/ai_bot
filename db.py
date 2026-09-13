"""
db.py — طبقة قاعدة البيانات (SQLite) لبوت ليلى.

ده الملف اللي كان ناقص/غلط (كان فيه نسخة مكررة من ملف البوت نفسه بدل قاعدة
البيانات الحقيقية). دلوقتي بيوفر كل الدوال اللي laila_lbot.py بينادي عليها
عن طريق db.<function>.
"""

import os
import re
import time
import sqlite3
import threading

DB_PATH = os.environ.get("DB_PATH", "data.db")

_lock = threading.Lock()
_conn = None


def _get_conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA foreign_keys=ON;")
    return _conn


def _now_ms():
    return int(time.time() * 1000)


def _row_to_dict(row):
    return dict(row) if row is not None else None


# ==================== إنشاء الجداول ====================
def init_db():
    conn = _get_conn()
    with _lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                activated INTEGER NOT NULL DEFAULT 0,
                activated_by INTEGER,
                antilink INTEGER NOT NULL DEFAULT 0,
                games_enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                title TEXT,
                username TEXT,
                link TEXT,
                UNIQUE(chat_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS default_channels (
                channel_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                link TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT
            );

            CREATE TABLE IF NOT EXISTS points (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                messages INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS auto_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger TEXT NOT NULL,
                response TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS banned_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                word TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS game_content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game TEXT NOT NULL,
                content TEXT NOT NULL,
                answer TEXT
            );

            CREATE TABLE IF NOT EXISTS pending (
                user_id INTEGER PRIMARY KEY,
                target_chat_id INTEGER,
                extra TEXT,
                created_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS reminders (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_reminder INTEGER NOT NULL,
                prompt_message_id INTEGER,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS ai_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_history_chat_user
                ON ai_history(chat_id, user_id, id);
            """
        )
        conn.commit()


# ==================== تطبيع نص (للكلمات الممنوعة) ====================
def _normalize(text):
    if not text:
        return ""
    t = text.strip().lower()
    t = re.sub(r'[\u064B-\u0652\u0670\u0640]', '', t)
    t = t.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
    t = t.replace('ة', 'ه')
    t = t.replace('ى', 'ي')
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


# ==================== المستخدمين ====================
def save_user(user_id, first_name, username):
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO users (user_id, first_name, username)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username
            """,
            (user_id, first_name, username),
        )
        conn.commit()


def get_user(user_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return _row_to_dict(row)


# ==================== المجموعات ====================
def remember_group(chat_id, title):
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO groups (chat_id, title)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title
            """,
            (chat_id, title),
        )
        conn.commit()


def forget_group(chat_id):
    conn = _get_conn()
    with _lock:
        conn.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
        conn.commit()


def get_group(chat_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT * FROM groups WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    if row is None:
        return None
    d = _row_to_dict(row)
    d["activated"] = bool(d["activated"])
    return d


def list_groups():
    conn = _get_conn()
    with _lock:
        rows = conn.execute("SELECT * FROM groups ORDER BY chat_id").fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["activated"] = bool(d["activated"])
        out.append(d)
    return out


def activate_group(chat_id, uid):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE groups SET activated = 1, activated_by = ? WHERE chat_id = ?",
            (uid, chat_id),
        )
        conn.commit()


def deactivate_group(chat_id):
    conn = _get_conn()
    with _lock:
        conn.execute("UPDATE groups SET activated = 0 WHERE chat_id = ?", (chat_id,))
        conn.commit()


def get_antilink(chat_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT antilink FROM groups WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return bool(row["antilink"]) if row else False


def set_antilink(chat_id, value):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE groups SET antilink = ? WHERE chat_id = ?",
            (1 if value else 0, chat_id),
        )
        conn.commit()


def get_games_enabled(chat_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT games_enabled FROM groups WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return bool(row["games_enabled"]) if row else True


def set_games_enabled(chat_id, value):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE groups SET games_enabled = ? WHERE chat_id = ?",
            (1 if value else 0, chat_id),
        )
        conn.commit()


# ==================== القنوات (لكل جروب) ====================
def add_channel(chat_id, ch):
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO channels (chat_id, channel_id, title, username, link)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, channel_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                link = excluded.link
            """,
            (chat_id, ch["id"], ch.get("title"), ch.get("username"), ch.get("link")),
        )
        conn.commit()


def remove_channel(chat_id, channel_id):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM channels WHERE chat_id = ? AND channel_id = ?",
            (chat_id, channel_id),
        )
        conn.commit()


def list_channels(chat_id):
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT * FROM channels WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_channels(chat_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM channels WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["c"] if row else 0


# ==================== القنوات الافتراضية ====================
def add_default_channel(ch):
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO default_channels (channel_id, title, username, link)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                link = excluded.link
            """,
            (ch["id"], ch.get("title"), ch.get("username"), ch.get("link")),
        )
        conn.commit()


def remove_default_channel(channel_id):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM default_channels WHERE channel_id = ?", (channel_id,)
        )
        conn.commit()


def list_default_channels():
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT * FROM default_channels ORDER BY channel_id"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ==================== الردود التلقائية ====================
def add_auto_reply(trigger, response):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO auto_replies (trigger, response) VALUES (?, ?)",
            (trigger, response),
        )
        conn.commit()


def remove_auto_reply(reply_id):
    conn = _get_conn()
    with _lock:
        conn.execute("DELETE FROM auto_replies WHERE id = ?", (reply_id,))
        conn.commit()


def list_auto_replies():
    conn = _get_conn()
    with _lock:
        rows = conn.execute("SELECT * FROM auto_replies ORDER BY id").fetchall()
    return [_row_to_dict(r) for r in rows]


def auto_reply_exists(trigger, response):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT 1 FROM auto_replies WHERE trigger = ? AND response = ? LIMIT 1",
            (trigger, response),
        ).fetchone()
    return row is not None


# ==================== الكلمات الممنوعة ====================
def add_banned_word(chat_id, word):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO banned_words (chat_id, word) VALUES (?, ?)",
            (chat_id, word),
        )
        conn.commit()


def remove_banned_word(word_id):
    conn = _get_conn()
    with _lock:
        conn.execute("DELETE FROM banned_words WHERE id = ?", (word_id,))
        conn.commit()


def list_banned_words(chat_id):
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT * FROM banned_words WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def banned_word_exists(chat_id, word):
    norm = _normalize(word)
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT word FROM banned_words WHERE chat_id = ?", (chat_id,)
        ).fetchall()
    return any(_normalize(r["word"]) == norm for r in rows)


def find_banned_word(chat_id, text):
    """بترجع أول كلمة ممنوعة موجودة كجزء من النص، أو None."""
    if not text:
        return None
    norm_text = _normalize(text)
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT word FROM banned_words WHERE chat_id = ?", (chat_id,)
        ).fetchall()
    for r in rows:
        w = _normalize(r["word"])
        if w and w in norm_text:
            return r["word"]
    return None


# ==================== الألعاب (محتوى) ====================
def add_game_content(game, content, answer=None):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO game_content (game, content, answer) VALUES (?, ?, ?)",
            (game, content, answer),
        )
        conn.commit()


def remove_game_content(item_id):
    conn = _get_conn()
    with _lock:
        conn.execute("DELETE FROM game_content WHERE id = ?", (item_id,))
        conn.commit()


def list_game_content(game):
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT * FROM game_content WHERE game = ? ORDER BY id", (game,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def game_content_exists(game, content):
    norm = content.strip().lower()
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT content FROM game_content WHERE game = ?", (game,)
        ).fetchall()
    return any(r["content"].strip().lower() == norm for r in rows)


def count_game_content(game):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM game_content WHERE game = ?", (game,)
        ).fetchone()
    return row["c"] if row else 0


def count_all_games():
    conn = _get_conn()
    with _lock:
        row = conn.execute("SELECT COUNT(*) AS c FROM game_content").fetchone()
    return row["c"] if row else 0


# ==================== الحالة المؤقتة (Pending) ====================
def set_pending(user_id, target_chat_id, extra=None):
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO pending (user_id, target_chat_id, extra, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                target_chat_id = excluded.target_chat_id,
                extra = excluded.extra,
                created_at = excluded.created_at
            """,
            (user_id, target_chat_id, extra, _now_ms()),
        )
        conn.commit()


def get_pending(user_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT * FROM pending WHERE user_id = ?", (user_id,)
        ).fetchone()
    return _row_to_dict(row)


def clear_pending(user_id):
    conn = _get_conn()
    with _lock:
        conn.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
        conn.commit()


# ==================== تذكيرات الاشتراك ====================
def get_reminder(chat_id, user_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT * FROM reminders WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
    return _row_to_dict(row)


def set_reminder(chat_id, user_id, prompt_message_id):
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO reminders (chat_id, user_id, last_reminder, prompt_message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                last_reminder = excluded.last_reminder,
                prompt_message_id = excluded.prompt_message_id
            """,
            (chat_id, user_id, _now_ms(), prompt_message_id),
        )
        conn.commit()


def clear_reminder(chat_id, user_id):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM reminders WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()


# ==================== التحذيرات ====================
def add_warning(chat_id, user_id):
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO warnings (chat_id, user_id, count)
            VALUES (?, ?, 1)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET count = count + 1
            """,
            (chat_id, user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
    return row["count"] if row else 1


def clear_warnings(chat_id, user_id):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()


# ==================== محادثة الذكاء الاصطناعي (ليلى) ====================
_AI_HISTORY_CAP = 120  # أقصى عدد رسائل يتم الاحتفاظ بيها لكل (جروب، مستخدم)


def add_ai_message(chat_id, user_id, role, text):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO ai_history (chat_id, user_id, role, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, role, text, _now_ms()),
        )
        # تنضيف الرسائل القديمة عشان الداتابيز ما تكبرش من غير داعي
        conn.execute(
            """
            DELETE FROM ai_history
            WHERE chat_id = ? AND user_id = ? AND id NOT IN (
                SELECT id FROM ai_history
                WHERE chat_id = ? AND user_id = ?
                ORDER BY id DESC LIMIT ?
            )
            """,
            (chat_id, user_id, chat_id, user_id, _AI_HISTORY_CAP),
        )
        conn.commit()


def get_ai_history(chat_id, user_id, limit=200):
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            """
            SELECT role, text FROM ai_history
            WHERE chat_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (chat_id, user_id, limit),
        ).fetchall()
    out = [{"role": r["role"], "text": r["text"]} for r in rows]
    out.reverse()
    return out


# ==================== النقاط والإحصائيات ====================
def add_points(chat_id, user_id, amount, count_message=False):
    conn = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO points (chat_id, user_id, points, messages)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                points = points + excluded.points,
                messages = messages + excluded.messages
            """,
            (chat_id, user_id, amount, 1 if count_message else 0),
        )
        conn.commit()


def get_points(chat_id, user_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT points, messages FROM points WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
    if row is None:
        return {"points": 0, "messages": 0}
    return _row_to_dict(row)


def get_top(chat_id, limit=10):
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            """
            SELECT user_id, points FROM points
            WHERE chat_id = ? ORDER BY points DESC LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_global_top(limit=10):
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            """
            SELECT user_id, SUM(points) AS points FROM points
            GROUP BY user_id ORDER BY points DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_total_points(user_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(points), 0) AS points,
                   COALESCE(SUM(messages), 0) AS messages
            FROM points WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    return _row_to_dict(row)
