"""
db.py — قاعدة بيانات SQLite لبوت ليلى (Laila Bot)
كل الدوال هنا synchronous (زي ما البوت بيناديها من غير await).
"""
import os
import sqlite3
import time

DB_PATH = os.environ.get("DB_PATH", "bot.db")


# ==================== أدوات مساعدة عامة ====================
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row(r):
    return dict(r) if r is not None else None


def _rows(rs):
    return [dict(r) for r in rs]


def _now_ms():
    return int(time.time() * 1000)


# ==================== إنشاء الجداول ====================
def init_db():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                chat_id        INTEGER PRIMARY KEY,
                title          TEXT,
                activated      INTEGER NOT NULL DEFAULT 0,
                activated_by   INTEGER,
                antilink       INTEGER NOT NULL DEFAULT 0,
                games_enabled  INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS channels (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                title      TEXT,
                username   TEXT,
                link       TEXT,
                UNIQUE(chat_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS default_channels (
                channel_id INTEGER PRIMARY KEY,
                title      TEXT,
                username   TEXT,
                link       TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                first_name TEXT,
                username   TEXT
            );

            CREATE TABLE IF NOT EXISTS points (
                chat_id  INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                points   INTEGER NOT NULL DEFAULT 0,
                messages INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS auto_replies (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger  TEXT NOT NULL,
                response TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS game_content (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                game    TEXT NOT NULL,
                content TEXT NOT NULL,
                answer  TEXT
            );

            CREATE TABLE IF NOT EXISTS banned_words (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                word    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending (
                user_id        INTEGER PRIMARY KEY,
                target_chat_id INTEGER,
                extra          TEXT,
                created_at     INTEGER
            );

            CREATE TABLE IF NOT EXISTS reminders (
                chat_id           INTEGER NOT NULL,
                user_id           INTEGER NOT NULL,
                last_reminder     INTEGER,
                prompt_message_id INTEGER,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS ai_history (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id  INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                role     TEXT NOT NULL,
                text     TEXT NOT NULL,
                ts       INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_history_chat_user
                ON ai_history (chat_id, user_id, id);
            """
        )


# ==================== المجموعات (Groups) ====================
def get_group(chat_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        return _row(r)


def list_groups():
    with _conn() as c:
        rs = c.execute("SELECT * FROM groups ORDER BY title").fetchall()
        return _rows(rs)


def remember_group(chat_id, title):
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        if exists:
            c.execute("UPDATE groups SET title=? WHERE chat_id=?", (title, chat_id))
        else:
            c.execute(
                "INSERT INTO groups (chat_id, title, activated, antilink, games_enabled) "
                "VALUES (?, ?, 0, 0, 1)",
                (chat_id, title),
            )


def forget_group(chat_id):
    with _conn() as c:
        c.execute("DELETE FROM groups WHERE chat_id=?", (chat_id,))
        c.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
        c.execute("DELETE FROM banned_words WHERE chat_id=?", (chat_id,))
        c.execute("DELETE FROM reminders WHERE chat_id=?", (chat_id,))


def activate_group(chat_id, uid):
    with _conn() as c:
        c.execute(
            "UPDATE groups SET activated=1, activated_by=? WHERE chat_id=?", (uid, chat_id)
        )


def deactivate_group(chat_id):
    with _conn() as c:
        c.execute("UPDATE groups SET activated=0 WHERE chat_id=?", (chat_id,))


def get_antilink(chat_id):
    with _conn() as c:
        r = c.execute("SELECT antilink FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r["antilink"]) if r else False


def set_antilink(chat_id, value):
    with _conn() as c:
        c.execute("UPDATE groups SET antilink=? WHERE chat_id=?", (1 if value else 0, chat_id))


def get_games_enabled(chat_id):
    with _conn() as c:
        r = c.execute("SELECT games_enabled FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r["games_enabled"]) if r else True


def set_games_enabled(chat_id, value):
    with _conn() as c:
        c.execute(
            "UPDATE groups SET games_enabled=? WHERE chat_id=?", (1 if value else 0, chat_id)
        )


# ==================== القنوات (Channels) ====================
def add_channel(chat_id, ch):
    with _conn() as c:
        c.execute(
            "INSERT INTO channels (chat_id, channel_id, title, username, link) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, channel_id) DO UPDATE SET "
            "title=excluded.title, username=excluded.username, link=excluded.link",
            (chat_id, ch["id"], ch.get("title"), ch.get("username"), ch.get("link")),
        )


def remove_channel(chat_id, channel_id):
    with _conn() as c:
        c.execute(
            "DELETE FROM channels WHERE chat_id=? AND channel_id=?", (chat_id, channel_id)
        )


def list_channels(chat_id):
    with _conn() as c:
        rs = c.execute(
            "SELECT * FROM channels WHERE chat_id=? ORDER BY id", (chat_id,)
        ).fetchall()
        return _rows(rs)


def count_channels(chat_id):
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) AS n FROM channels WHERE chat_id=?", (chat_id,)).fetchone()
        return r["n"] if r else 0


def add_default_channel(ch):
    with _conn() as c:
        c.execute(
            "INSERT INTO default_channels (channel_id, title, username, link) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET "
            "title=excluded.title, username=excluded.username, link=excluded.link",
            (ch["id"], ch.get("title"), ch.get("username"), ch.get("link")),
        )


def remove_default_channel(channel_id):
    with _conn() as c:
        c.execute("DELETE FROM default_channels WHERE channel_id=?", (channel_id,))


def list_default_channels():
    with _conn() as c:
        rs = c.execute("SELECT * FROM default_channels ORDER BY rowid").fetchall()
        return _rows(rs)


# ==================== المستخدمين (Users) ====================
def save_user(user_id, first_name, username):
    with _conn() as c:
        c.execute(
            "INSERT INTO users (user_id, first_name, username) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, "
            "username=excluded.username",
            (user_id, first_name, username),
        )


def get_user(user_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return _row(r)


# ==================== النقاط والإحصائيات ====================
def get_points(chat_id, user_id):
    with _conn() as c:
        r = c.execute(
            "SELECT points, messages FROM points WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return _row(r) or {"points": 0, "messages": 0}


def add_points(chat_id, user_id, amount, count_message=False):
    with _conn() as c:
        inc_msg = 1 if count_message else 0
        c.execute(
            "INSERT INTO points (chat_id, user_id, points, messages) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "points = points + excluded.points, messages = messages + excluded.messages",
            (chat_id, user_id, amount, inc_msg),
        )


def get_top(chat_id, limit=10):
    with _conn() as c:
        rs = c.execute(
            "SELECT user_id, points FROM points WHERE chat_id=? "
            "ORDER BY points DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return _rows(rs)


def get_global_top(limit=10):
    with _conn() as c:
        rs = c.execute(
            "SELECT user_id, SUM(points) AS points FROM points "
            "GROUP BY user_id ORDER BY points DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return _rows(rs)


def get_total_points(user_id):
    with _conn() as c:
        r = c.execute(
            "SELECT SUM(points) AS points, SUM(messages) AS messages "
            "FROM points WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not r or r["points"] is None:
            return {"points": 0, "messages": 0}
        return {"points": r["points"], "messages": r["messages"] or 0}


# ==================== الردود التلقائية ====================
def list_auto_replies():
    with _conn() as c:
        rs = c.execute("SELECT * FROM auto_replies ORDER BY id").fetchall()
        return _rows(rs)


def auto_reply_exists(trigger, response):
    with _conn() as c:
        r = c.execute(
            "SELECT 1 FROM auto_replies WHERE trigger=? AND response=?", (trigger, response)
        ).fetchone()
        return bool(r)


def add_auto_reply(trigger, response):
    with _conn() as c:
        c.execute(
            "INSERT INTO auto_replies (trigger, response) VALUES (?, ?)", (trigger, response)
        )


def remove_auto_reply(reply_id):
    with _conn() as c:
        c.execute("DELETE FROM auto_replies WHERE id=?", (reply_id,))


# ==================== محتوى الألعاب ====================
def add_game_content(game, content, answer=None):
    with _conn() as c:
        c.execute(
            "INSERT INTO game_content (game, content, answer) VALUES (?, ?, ?)",
            (game, content, answer),
        )


def remove_game_content(item_id):
    with _conn() as c:
        c.execute("DELETE FROM game_content WHERE id=?", (item_id,))


def list_game_content(game):
    with _conn() as c:
        rs = c.execute(
            "SELECT * FROM game_content WHERE game=? ORDER BY id", (game,)
        ).fetchall()
        return _rows(rs)


def game_content_exists(game, content):
    with _conn() as c:
        r = c.execute(
            "SELECT 1 FROM game_content WHERE game=? AND content=?", (game, content)
        ).fetchone()
        return bool(r)


def count_game_content(game):
    with _conn() as c:
        r = c.execute(
            "SELECT COUNT(*) AS n FROM game_content WHERE game=?", (game,)
        ).fetchone()
        return r["n"] if r else 0


def count_all_games():
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) AS n FROM game_content").fetchone()
        return r["n"] if r else 0


# ==================== الكلمات الممنوعة ====================
def add_banned_word(chat_id, word):
    with _conn() as c:
        c.execute("INSERT INTO banned_words (chat_id, word) VALUES (?, ?)", (chat_id, word))


def remove_banned_word(word_id):
    with _conn() as c:
        c.execute("DELETE FROM banned_words WHERE id=?", (word_id,))


def list_banned_words(chat_id):
    with _conn() as c:
        rs = c.execute(
            "SELECT * FROM banned_words WHERE chat_id=? ORDER BY id", (chat_id,)
        ).fetchall()
        return _rows(rs)


def banned_word_exists(chat_id, word):
    with _conn() as c:
        r = c.execute(
            "SELECT 1 FROM banned_words WHERE chat_id=? AND word=?", (chat_id, word)
        ).fetchone()
        return bool(r)


def find_banned_word(chat_id, text):
    if not text:
        return None
    norm_text = text.strip().lower()
    with _conn() as c:
        rs = c.execute("SELECT word FROM banned_words WHERE chat_id=?", (chat_id,)).fetchall()
    for r in rs:
        w = (r["word"] or "").strip().lower()
        if w and w in norm_text:
            return w
    return None


# ==================== الحالة المؤقتة (Pending) ====================
def get_pending(user_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM pending WHERE user_id=?", (user_id,)).fetchone()
        return _row(r)


def set_pending(user_id, target, extra=None):
    with _conn() as c:
        c.execute(
            "INSERT INTO pending (user_id, target_chat_id, extra, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "target_chat_id=excluded.target_chat_id, extra=excluded.extra, "
            "created_at=excluded.created_at",
            (user_id, target, extra, _now_ms()),
        )


def clear_pending(user_id):
    with _conn() as c:
        c.execute("DELETE FROM pending WHERE user_id=?", (user_id,))


# ==================== التذكيرات (Reminders) ====================
def get_reminder(chat_id, user_id):
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM reminders WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        return _row(r)


def set_reminder(chat_id, user_id, message_id):
    with _conn() as c:
        c.execute(
            "INSERT INTO reminders (chat_id, user_id, last_reminder, prompt_message_id) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "last_reminder=excluded.last_reminder, "
            "prompt_message_id=excluded.prompt_message_id",
            (chat_id, user_id, _now_ms(), message_id),
        )


def clear_reminder(chat_id, user_id):
    with _conn() as c:
        c.execute("DELETE FROM reminders WHERE chat_id=? AND user_id=?", (chat_id, user_id))


# ==================== التحذيرات (Warnings) ====================
def add_warning(chat_id, user_id):
    with _conn() as c:
        c.execute(
            "INSERT INTO warnings (chat_id, user_id, count) VALUES (?, ?, 1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET count = count + 1",
            (chat_id, user_id),
        )
        r = c.execute(
            "SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        return r["count"] if r else 1


def clear_warnings(chat_id, user_id):
    with _conn() as c:
        c.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))


# ==================== ذاكرة محادثات ليلى (AI) ====================
def add_ai_message(chat_id, user_id, role, text, keep_last=400):
    """بتضيف رسالة لسجل المحادثة، وبتقلّم القديم عشان الجدول ميكبرش من غير داعي."""
    with _conn() as c:
        c.execute(
            "INSERT INTO ai_history (chat_id, user_id, role, text, ts) VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, role, text, _now_ms()),
        )
        # سيب آخر keep_last رسالة بس لكل محادثة، امسح الباقي
        c.execute(
            "DELETE FROM ai_history WHERE chat_id=? AND user_id=? AND id NOT IN ("
            "  SELECT id FROM ai_history WHERE chat_id=? AND user_id=? "
            "  ORDER BY id DESC LIMIT ?"
            ")",
            (chat_id, user_id, chat_id, user_id, keep_last),
        )


def get_ai_history(chat_id, user_id, limit=200):
    with _conn() as c:
        rs = c.execute(
            "SELECT role, text FROM ai_history WHERE chat_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, user_id, limit),
        ).fetchall()
    rows = _rows(rs)
    rows.reverse()  # نرجعها بترتيب زمني (الأقدم الأول)
    return rows


def clear_ai_history(chat_id, user_id):
    with _conn() as c:
        c.execute("DELETE FROM ai_history WHERE chat_id=? AND user_id=?", (chat_id, user_id))
