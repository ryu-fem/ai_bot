import os
import re
import json
import time
import random
import asyncio
import logging
import httpx
from telegram import (
    Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M
)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatMemberHandler,
    filters, ContextTypes
)

import db

# ==================== الإعدادات ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = 8666320077
INITIAL_DEFAULT_CHANNELS = [-1002738530870]

# مفتاح Groq (مجاني تمامًا، بلا فيزا) - من https://console.groq.com/keys
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
GROQ_CHAT_MODEL  = "groq/compound"           # فيه بحث ويب تلقائي وقت اللزوم
GROQ_INTENT_MODEL = "openai/gpt-oss-120b"    # موديل قوي ومتاح حاليًا لفهم العامية في الأوامر الإدارية

CHANNEL_WAIT_TIMEOUT_MS = 5 * 60 * 1000
PROMPT_AUTO_DELETE_MS   = 60 * 1000
REMINDER_COOLDOWN_MS    = 5 * 60 * 1000
MAX_CHANNELS_PER_GROUP  = 5
PENDING_DEFAULT         = 0
PENDING_ADD_TRIGGER     = -1
PENDING_ADD_REPLY       = -2
PENDING_ADD_GAME_WORD   = -3
PENDING_ADD_GAME_ANSWER = -4
PENDING_ADD_BANNED_WORD = -5
GROUP_TYPES             = {ChatType.GROUP, ChatType.SUPERGROUP}

SUB_CACHE_TTL    = 30
ADMIN_CACHE_TTL  = 60
RIGHTS_CACHE_TTL = 30

TRIGGER_OWNER = ("المالك",)
TRIGGER_ADMIN = ("الأدمن", "الادمن", "ادمن", "أدمن")
TRIGGER_TOP   = ("توب", "التوب", "top", "Top")
TRIGGER_MY_STATS = ("إحصائياتي", "احصائياتي", "إحصائيتي", "احصائيتي")
TRIGGER_HIS_STATS = ("إحصائياته", "احصائياته", "إحصائياتك", "احصائياتك", "إحصائياتها", "احصائياتها")
TRIGGER_GAMES = ("الألعاب", "الالعاب", "ألعاب", "العاب", "games")
TRIGGER_WARN = ("تحذير",)
TRIGGER_UNWARN = ("الغاء التحذير", "إلغاء التحذير", "الغاء تحذير", "إلغاء تحذير")
TRIGGER_BAN = ("حظر",)
TRIGGER_UNBAN = ("الغاء الحظر", "إلغاء الحظر", "الغاء حظر", "إلغاء حظر")
TRIGGER_MUTE = ("كتم",)
TRIGGER_UNMUTE = ("الغاء الكتم", "إلغاء الكتم", "الغاء كتم", "إلغاء كتم")
TRIGGER_DELETE = ("مسح",)
TRIGGER_GAMES_ON = ("تفعيل الالعاب", "تفعيل الألعاب", "شغل الالعاب", "شغل الألعاب")
TRIGGER_GAMES_OFF = ("تعطيل الالعاب", "تعطيل الألعاب", "وقف الالعاب", "وقف الألعاب")

GAME_FASTEST = "fastest"
GAME_SCRAMBLE = "scramble"
GAME_QUESTIONS = "questions"
GAME_ENGLISH = "english"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("turbot")

BOT_USERNAME = ""
_cache = {}
active_games = {}
timer_games = {}

def is_owner(uid): return uid == OWNER_ID

# ==================== الكاش ====================
def cache_get(key, ttl):
    if key not in _cache: return None
    val, ts = _cache[key]
    if time.time() - ts > ttl:
        _cache.pop(key, None)
        return None
    return val

def cache_set(key, val):
    _cache[key] = (val, time.time())

def invalidate_sub(chid, uid): _cache.pop(f"sub:{chid}:{uid}", None)
def invalidate_admin(cid, uid): _cache.pop(f"adm:{cid}:{uid}", None)
def invalidate_bot_rights(cid):
    for k in list(_cache.keys()):
        if k.startswith(f"rights:{cid}"): _cache.pop(k, None)

# ==================== تطبيع النص ====================
def normalize_text(text):
    if not text: return ""
    t = text.strip().lower()
    t = re.sub(r'[\u064B-\u0652\u0670\u0640]', '', t)
    t = t.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
    t = t.replace('ة', 'ه')
    t = t.replace('ى', 'ي')
    t = re.sub(r'^[\s\.,!؟?،؛;:\-_\*#@\u200f\u200e]+', '', t)
    t = re.sub(r'[\s\.,!؟?،؛;:\-_\*#@\u200f\u200e]+$', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()

def find_reply_for(text):
    if not text: return None
    norm = normalize_text(text)
    if not norm: return None
    rows = db.list_auto_replies()
    matches = [r["response"] for r in rows if normalize_text(r["trigger"]) == norm]
    if not matches: return None
    return random.choice(matches)

# ==================== ليلى - الذكاء الاصطناعي ====================
async def _post_with_retry(url, payload, headers=None, timeout=30, retries=2):
    """بتنادي أي API، ولو رجعله 429 (ضغط مؤقت) بتستنى شوية وتجرب تاني."""
    delay = 2
    last_exc = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code == 429 and attempt < retries:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                r.raise_for_status()
                return r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < retries:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            last_exc = e
            break
        except Exception as e:
            last_exc = e
            break
    raise last_exc

# ينادي عليها بأي صيغة من: "يا ليلى"، "يا ليلي"، "ليلى"، "ليلي" في أول الرسالة
AI_NAME_PATTERN = re.compile(
    r'^\s*(?:يا\s+)?(?:ليلى|ليلي)\b[\s,.:!؟\-–_]*',
    re.IGNORECASE
)

LAILA_SYSTEM_PROMPT = (
    "انتِ اسمك ليلى، أدمن جريئة وواثقة في الجروب، مش بوت بارد. "
    "اتكلمي عامية مصرية صريحة وفاجرة شوية زي كلام الشارع، من غير فصحى ومن غير لف. "
    "كوني مباشرة وحادة لما يلزم، ومش مستحية تقولي كلام سبايسي أو تتقفي مع حد لو بدأ يتهزأ. "
    "افهمي بسرعة ورُدّي على طول، ردودك قصيرة وحاضرة (سطر أو اتنين غالبًا). "
    "لو حد بدأ يفتح مواضيع جريئة أو سبايسي، تقدري تكملي معاه بنفس الأسلوب من غير ما تتقيدي أو تتعملي محترمة زيادة. "
    "لو حد سألك حاجة حديثة، دوري واتأكدي قبل ما تردي. "
    "متستخدميش رموز برمجية أو كلام إنجليزي غريب إلا لو ضروري. "
    "انتِ ليكِ صلاحيات أدمن كاملة، ولو حد طلب منكِ تعملي حاجة إدارية (كتم، حظر، مسح، تحذير...) نفذيها لو هو أدمن أو مالك."
)

def extract_ai_query(raw_text):
    """لو الرسالة بتنادي على (ليلى) في الأول، بترجع الباقي (ممكن يكون فاضي).
    لو مفيش نداء بالاسم، بترجع None."""
    if not raw_text:
        return None
    stripped = raw_text.strip()
    m = AI_NAME_PATTERN.match(stripped)
    if not m:
        return None
    return stripped[m.end():].strip()

async def ask_laila(chat_id, user_id, query, first_name=""):
    if not GROQ_API_KEY:
        return "⚠️ ميزة الذكاء الاصطناعي مش متفعّلة دلوقتي (محتاجين مفتاح Groq)."

    hist = db.get_ai_history(chat_id, user_id, limit=400)

    messages = [{"role": "system", "content": LAILA_SYSTEM_PROMPT}]
    for h in hist:
        role = "user" if h["role"] == "user" else "assistant"
        messages.append({"role": role, "content": h["text"]})
    messages.append({"role": "user", "content": query})

    payload = {
        "model": GROQ_CHAT_MODEL,
        "messages": messages,
        "max_tokens": 600,
        "temperature": 0.8,
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    url = "https://api.groq.com/openai/v1/chat/completions"

    try:
        data = await _post_with_retry(url, payload, headers=headers, timeout=30)
        answer = data["choices"][0]["message"]["content"].strip()
        if not answer:
            answer = "🤔 معرفتش أرد، جرب تسأل بطريقة تانية."
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            log.warning(f"⚠️ ضغط زيادة على Groq: {e}")
            return "⏳ في ضغط زيادة عليّا دلوقتي، استنى ثواني وجرب تاني."
        log.warning(f"⚠️ فشل استدعاء الذكاء الاصطناعي: {e}")
        return "😔 معلش، حصل خطأ وأنا بحاول أفكر. جرب تاني كمان شوية."
    except Exception as e:
        log.warning(f"⚠️ فشل استدعاء الذكاء الاصطناعي: {e}")
        return "😔 معلش، حصل خطأ وأنا بحاول أفكر. جرب تاني كمان شوية."

    db.add_ai_message(chat_id, user_id, "user", query)
    db.add_ai_message(chat_id, user_id, "assistant", answer)
    return answer

TARGET_ACTIONS = {"ban", "unban", "kick", "mute", "unmute", "warn", "unwarn",
                   "delete", "pin", "unpin"}
GROUP_ACTIONS  = {"games_on", "games_off", "antilink_on", "antilink_off",
                   "add_banned_word", "remove_banned_word",
                   "add_channel", "remove_channel",
                   "activate_bot", "deactivate_bot"}
ADMIN_ACTIONS  = TARGET_ACTIONS | GROUP_ACTIONS | {"none"}

async def classify_admin_intent(text):
    """بتستخدم الذكاء الاصطناعي تفهم أي مهمة إدارية في الجروب: كتم/حظر/تحذير/مسح
    (على شخص معين)، أو تشغيل/إيقاف الألعاب، أو حماية الروابط، أو منع/فك منع كلمة."""
    if not GROQ_API_KEY or not text:
        return {"action": "none", "word": None}
    prompt = (
        "انت مصنّف نوايا لبوت إدارة جروبات تليجرام اسمه ليلى، وعنده صلاحيات أدمن كاملة. "
        "المتكلم أدمن في الجروب وبيكلم ليلى. حدد هل كلامه مهمة إدارية، ولو أيوه حدد نوعها "
        "بالظبط من الفئات دي:\n"
        "ban = حظر شخص معين من الجروب نهائيًا\n"
        "unban = فك الحظر عن شخص معين\n"
        "kick = طرد شخص معين بس يقدر يرجع تاني (مش حظر نهائي)\n"
        "mute = كتم شخص معين (منعه من الكتابة)\n"
        "unmute = فك الكتم عن شخص معين\n"
        "warn = تحذير شخص معين\n"
        "unwarn = إلغاء تحذيرات شخص معين\n"
        "delete = مسح رسالة شخص معين بس من غير عقاب\n"
        "pin = تثبيت رسالة معينة\n"
        "unpin = فك تثبيت رسالة معينة\n"
        "games_on = تشغيل/تفعيل الألعاب في الجروب عمومًا\n"
        "games_off = إيقاف/تعطيل الألعاب في الجروب عمومًا\n"
        "antilink_on = تفعيل منع الروابط في الجروب\n"
        "antilink_off = إيقاف منع الروابط في الجروب\n"
        "add_banned_word = إضافة كلمة معينة لقائمة الكلمات الممنوعة (استخرج الكلمة نفسها)\n"
        "remove_banned_word = شيل كلمة معينة من قائمة الكلمات الممنوعة (استخرج الكلمة نفسها)\n"
        "add_channel = إضافة قناة اشتراك إجباري (استخرج يوزر أو رابط القناة)\n"
        "remove_channel = شيل قناة اشتراك إجباري (استخرج يوزر أو رابط أو اسم القناة)\n"
        "activate_bot = تفعيل الاشتراك الإجباري/تشغيل البوت في الجروب عمومًا\n"
        "deactivate_bot = تعطيل الاشتراك الإجباري/إيقاف البوت في الجروب عمومًا\n"
        "none = مش مهمة إدارية خالص، كلام عادي أو سؤال أو دردشة\n\n"
        f"الجملة: \"{text}\"\n\n"
        "رد بـ JSON فقط بالشكل ده بالظبط، من غير أي كلام زيادة أو Markdown:\n"
        '{"action": "الفئة", "word": "الكلمة أو اليوزر أو الرابط لو موجود، أو null لو مفيش"}'
    )
    payload = {
        "model": GROQ_INTENT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 300,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    url = "https://api.groq.com/openai/v1/chat/completions"
    try:
        data = await _post_with_retry(url, payload, headers=headers, timeout=15)
        raw = data["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        parsed = json.loads(raw)
        action = str(parsed.get("action", "none")).strip().lower()
        word = parsed.get("word")
        word = word.strip() if isinstance(word, str) and word.strip() else None
        if action not in ADMIN_ACTIONS:
            action = "none"
        log.info(f"🧭 تصنيف إداري: \"{text}\" -> {action} (كلمة: {word})")
        return {"action": action, "word": word}
    except Exception as e:
        log.warning(f"⚠️ فشل تصنيف المهمة الإدارية: {e}")
        return {"action": "none", "word": None}

async def perform_group_action(bot, msg, cid, action, word=None):
    """بتنفّذ مهمة إدارية عامة على الجروب (مش موجّهة لشخص معين)."""
    if action == "games_on":
        db.set_games_enabled(cid, True)
        try: await msg.reply_text("✅ تم تفعيل الألعاب في الجروب.")
        except TelegramError: pass

    elif action == "games_off":
        db.set_games_enabled(cid, False)
        try: await msg.reply_text("🛑 تم تعطيل الألعاب في الجروب.")
        except TelegramError: pass

    elif action == "antilink_on":
        db.set_antilink(cid, True)
        try: await msg.reply_text("🔗 تم تفعيل منع الروابط في الجروب.")
        except TelegramError: pass

    elif action == "antilink_off":
        db.set_antilink(cid, False)
        try: await msg.reply_text("🔗 تم إيقاف منع الروابط في الجروب.")
        except TelegramError: pass

    elif action == "add_banned_word":
        if not word:
            try: await msg.reply_text("🤔 قوليلي الكلمة اللي عايزة تمنعها بالظبط.")
            except TelegramError: pass
            return
        if db.banned_word_exists(cid, word):
            try: await msg.reply_text(f"⚠️ كلمة \"{word}\" ممنوعة أصلاً.")
            except TelegramError: pass
        else:
            db.add_banned_word(cid, word)
            try: await msg.reply_text(f"🚫 تم منع كلمة \"{word}\" في الجروب.")
            except TelegramError: pass

    elif action == "remove_banned_word":
        if not word:
            try: await msg.reply_text("🤔 قوليلي الكلمة اللي عايزة تشيلها من الممنوعات.")
            except TelegramError: pass
            return
        match = next(
            (w for w in db.list_banned_words(cid) if w["word"].strip() == word.strip()),
            None,
        )
        if match:
            db.remove_banned_word(match["id"])
            try: await msg.reply_text(f"✅ تم شيل كلمة \"{word}\" من الممنوعات.")
            except TelegramError: pass
        else:
            try: await msg.reply_text(f"🤔 مش لاقيه كلمة \"{word}\" في قائمة الممنوعات.")
            except TelegramError: pass

    elif action == "add_channel":
        if not word:
            try: await msg.reply_text("🤔 ابعتيلي يوزر أو رابط القناة اللي عايزة تضيفيها.")
            except TelegramError: pass
            return
        if db.count_channels(cid) >= MAX_CHANNELS_PER_GROUP:
            try: await msg.reply_text(f"⚠️ أقصى عدد قنوات هو {MAX_CHANNELS_PER_GROUP}.")
            except TelegramError: pass
            return
        res = await resolve_channel(bot, word)
        if not res["ok"]:
            try: await msg.reply_text(res["reason"])
            except TelegramError: pass
            return
        db.add_channel(cid, res["channel"])
        try:
            title = res["channel"]["title"] or res["channel"]["username"] or word
            await msg.reply_text(f"✅ تم إضافة قناة \"{title}\" لقنوات الاشتراك الإجباري.")
        except TelegramError:
            pass

    elif action == "remove_channel":
        if not word:
            try: await msg.reply_text("🤔 قوليلي يوزر أو اسم القناة اللي عايزة تشيليها.")
            except TelegramError: pass
            return
        chans = db.list_channels(cid)
        w = word.strip().lstrip("@")
        match = next(
            (c for c in chans if
             (c.get("username") and c["username"].strip().lstrip("@").lower() == w.lower())
             or (c.get("title") and c["title"].strip().lower() == word.strip().lower())
             or (c.get("link") and w.lower() in c["link"].lower())),
            None,
        )
        if not match:
            try: await msg.reply_text(f"🤔 مش لاقيه قناة اسمها \"{word}\" في قنوات الجروب.")
            except TelegramError: pass
            return
        db.remove_channel(cid, match["channel_id"])
        remaining = db.list_channels(cid)
        g = db.get_group(cid)
        if not remaining and g and g["activated"]:
            db.deactivate_group(cid)
        try: await msg.reply_text(f"✅ تم شيل قناة \"{match['title'] or match['username']}\".")
        except TelegramError: pass

    elif action == "activate_bot":
        if db.count_channels(cid) == 0:
            try: await msg.reply_text("⚠️ لازم تضيفي قناة اشتراك واحدة على الأقل الأول.")
            except TelegramError: pass
            return
        rights = await get_bot_rights(bot, cid, fresh=True)
        if not rights["is_admin"]:
            try: await msg.reply_text("⚠️ لازم ترفعيني (أنا نفسي) مشرفة في الجروب الأول.")
            except TelegramError: pass
            return
        if not rights["can_delete"]:
            try: await msg.reply_text("⚠️ لازم تفعّلي صلاحية حذف الرسائل ليّا.")
            except TelegramError: pass
            return
        db.activate_group(cid, msg.from_user.id)
        try: await msg.reply_text("✅ تم تفعيل الاشتراك الإجباري في الجروب.")
        except TelegramError: pass

    elif action == "deactivate_bot":
        db.deactivate_group(cid)
        try: await msg.reply_text("⛔ تم تعطيل الاشتراك الإجباري في الجروب.")
        except TelegramError: pass

async def maybe_ai_reply(bot, msg, cid, uid, text, first_name):
    """بترجع True لو ليلى ردت (بالاسم أو برد على رسالتها) — سواء كلام عادي أو مهمة إدارية."""
    if not text:
        return False
    query = extract_ai_query(text)
    replying_to_bot = bool(
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.id == bot.id
    )
    if query is None and not replying_to_bot:
        return False
    if query is None:
        query = text.strip()
    if not query:
        query = "قولتلي اسمي بس من غير سؤال، رحبي بيا باختصار واسأليني احتاج مساعدة في ايه."

    reply_to = msg.reply_to_message
    reply_target = (
        reply_to if (reply_to and reply_to.from_user and reply_to.from_user.id != bot.id)
        else None
    )

    # لو المرسل أدمن، جرّب تفهم لو كلامه مهمة إدارية (سواء موجهة لشخص أو على الجروب عمومًا)
    if await is_user_admin(bot, cid, uid) or is_owner(uid):
        intent = await classify_admin_intent(query)
        action = intent.get("action", "none")
        if action in TARGET_ACTIONS:
            if reply_target:
                await perform_mod_action(bot, msg, cid, reply_target, action)
                return True
            try:
                await msg.reply_text("🤔 لازم تردي على رسالة الشخص اللي عايزة تتصرفي معاه.")
            except TelegramError:
                pass
            return True
        if action in GROUP_ACTIONS:
            await perform_group_action(bot, msg, cid, action, intent.get("word"))
            return True

    try:
        await bot.send_chat_action(cid, "typing")
    except TelegramError:
        pass

    answer = await ask_laila(cid, uid, query, first_name)
    try:
        await msg.reply_text(answer)
    except TelegramError:
        pass
    return True

# ==================== دوال مساعدة ====================
async def is_subscribed(bot, chid, uid):
    key = f"sub:{chid}:{uid}"
    cached = cache_get(key, SUB_CACHE_TTL)
    if cached is not None: return cached
    try:
        m = await bot.get_chat_member(chid, uid)
        ok = m.status in ("creator","administrator","member","restricted")
        cache_set(key, ok)
        return ok
    except TelegramError as e:
        log.warning(f"⚠️ فشل فحص الاشتراك {uid}@{chid}: {e}")
        return True

async def is_user_admin(bot, cid, uid):
    key = f"adm:{cid}:{uid}"
    cached = cache_get(key, ADMIN_CACHE_TTL)
    if cached is not None: return cached
    try:
        m = await bot.get_chat_member(cid, uid)
        ok = m.status in ("creator","administrator")
    except TelegramError:
        ok = False
    cache_set(key, ok)
    return ok

async def get_bot_rights(bot, cid, fresh=False):
    key = f"rights:{cid}"
    if not fresh:
        cached = cache_get(key, RIGHTS_CACHE_TTL)
        if cached is not None: return cached
    try:
        me = await bot.get_me()
        m = await bot.get_chat_member(cid, me.id)
        is_admin = m.status in ("administrator","creator")
        can_del = bool(getattr(m, "can_delete_messages", False)) if is_admin else False
        can_restrict = bool(getattr(m, "can_restrict_members", False)) if is_admin else False
        res = {"is_admin": is_admin, "can_delete": can_del, "can_restrict": can_restrict}
    except TelegramError:
        res = {"is_admin": False, "can_delete": False, "can_restrict": False}
    cache_set(key, res)
    return res

async def safe_delete(bot, cid, mid):
    try: await bot.delete_message(cid, mid)
    except TelegramError: pass

def delete_later(bot, cid, mid, delay_ms):
    async def _do():
        await asyncio.sleep(delay_ms/1000)
        await safe_delete(bot, cid, mid)
    asyncio.create_task(_do())

def extract_channel_identifier(message):
    origin = getattr(message, "forward_origin", None)
    if origin:
        ch = getattr(origin, "chat", None)
        if ch:
            if ch.username: return "@" + ch.username
            return str(ch.id)
    text = (message.text or "").strip()
    if not text: return None
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)", text)
    if m: return "@" + m.group(1)
    if text.startswith("@") and re.match(r"^@[A-Za-z0-9_]{4,}$", text): return text
    if re.match(r"^-?\d+$", text): return text
    return None

async def resolve_channel(bot, ident):
    try:
        ch = await bot.get_chat(ident)
    except TelegramError as e:
        return {"ok": False, "reason": f"❌ لم أستطع الوصول للقناة.\nتأكد أن البوت مشرف فيها.\n({e.message})"}
    link = ch.invite_link or (f"https://t.me/{ch.username}" if ch.username else None)
    return {"ok": True, "channel": {"id": ch.id, "title": ch.title, "username": ch.username, "link": link}}

def escape_html(v=""):
    return v.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

async def can_manage(update, cid):
    uid = update.effective_user.id
    if is_owner(uid): return True
    return await is_user_admin(update.get_bot(), cid, uid)

async def missing_channels(bot, chans, uid):
    out = []
    for c in chans:
        try:
            if not await is_subscribed(bot, c["channel_id"], uid): out.append(c)
        except: pass
    return out

# ==================== ردود المالك / الأدمن ====================
async def build_owner_reply(bot, chat_id):
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramError as e:
        log.warning(f"فشل جلب أدمنز {chat_id}: {e}")
        return None
    owner = None
    for a in admins:
        if a.status == "creator":
            owner = a.user
            break
    if not owner: return None
    name = owner.first_name or "المالك"
    return f'<a href="tg://user?id={owner.id}">{escape_html(name)}</a>'

async def build_admins_reply(bot, chat_id):
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramError as e:
        log.warning(f"فشل جلب أدمنز {chat_id}: {e}")
        return None
    owner = None
    others = []
    for a in admins:
        u = a.user
        if u.is_bot: continue
        if a.status == "creator":
            owner = u
        else:
            others.append(u)
    if not owner and not others: return None
    lines = []
    if owner:
        name = owner.first_name or "المالك"
        lines.append(f"👑 <a href=\"tg://user?id={owner.id}\">{escape_html(name)}</a>")
    for u in others:
        name = u.first_name or "أدمن"
        lines.append(f"👮 <a href=\"tg://user?id={u.id}\">{escape_html(name)}</a>")
    return "\n".join(lines)

# ==================== إحصائيات وتوب ====================
async def build_my_stats_reply(bot, chat_id, user_id, first_name):
    p = db.get_points(chat_id, user_id)
    top = db.get_top(chat_id, 100)
    rank = "غير مصنف"
    for i, item in enumerate(top, 1):
        if item["user_id"] == user_id:
            rank = f"#{i}"
            break
    return (
        f"📊 *إحصائياتك*\n\n"
        f"👤 *{escape_html(first_name)}*\n"
        f"⭐ النقاط: *{p['points']}*\n"
        f"💬 الرسائل: *{p['messages']}*\n"
        f"🏅 الترتيب: *{rank}*"
    )

async def build_top_reply(bot, chat_id, title=None):
    top = db.get_top(chat_id, 10)
    if not top:
        return "📊 لا توجد إحصائيات بعد."
    lines = [f"🏆 *توب 10 في {escape_html(title or 'الجروب')}*\n"]
    medals = ["🥇","🥈","🥉"]
    for i, item in enumerate(top, 1):
        try:
            m = await bot.get_chat_member(chat_id, item["user_id"])
            name = m.user.first_name or "عضو"
        except:
            u = db.get_user(item["user_id"])
            name = u["first_name"] if u and u["first_name"] else f"عضو {item['user_id']}"
        prefix = medals[i-1] if i <= 3 else f"{i}."
        lines.append(f"{prefix} {escape_html(name)} — *{item['points']}* نقطة")
    return "\n".join(lines)

async def build_global_top_reply(bot):
    top = db.get_global_top(10)
    if not top:
        return "📊 لا توجد إحصائيات بعد."
    lines = ["🏆 *توب 10 عام (كل الجروبات)*\n"]
    medals = ["🥇","🥈","🥉"]
    for i, item in enumerate(top, 1):
        u = db.get_user(item["user_id"])
        name = u["first_name"] if u and u["first_name"] else f"عضو {item['user_id']}"
        prefix = medals[i-1] if i <= 3 else f"{i}."
        lines.append(f"{prefix} {escape_html(name)} — *{item['points']}* نقطة")
    return "\n".join(lines)

# ==================== الألعاب ====================
async def start_number_game(bot, chat_id):
    num = random.randint(1, 100)
    active_games[chat_id] = {"type": "number", "answer": num}
    try:
        await bot.send_message(chat_id,
            "🔢 *خمن الرقم*\n\n"
            "خمنت رقم بين *1* و *100*\n"
            "اكتب تخمينك دلوقتي 👇\n"
            "⏱ عندك 60 ثانية",
            parse_mode=ParseMode.MARKDOWN)
    except: pass

async def start_fastest_game(bot, chat_id):
    rows = db.list_game_content(GAME_FASTEST)
    if not rows:
        await bot.send_message(chat_id, "⚠️ لا توجد كلمات مسجلة بعد.")
        return
    item = random.choice(rows)
    word = item["content"]
    active_games[chat_id] = {"type": "fastest", "answer": word.lower()}
    try:
        await bot.send_message(chat_id,
            f"⚡ *الأسرع*\n\n"
            f"أول واحد يكتب الكلمة دي يكسب:\n\n"
            f"👉 `{word}`\n\n"
            "⏱ عندك 60 ثانية",
            parse_mode=ParseMode.MARKDOWN)
    except: pass

async def start_scramble_game(bot, chat_id):
    rows = db.list_game_content(GAME_SCRAMBLE)
    if not rows:
        await bot.send_message(chat_id, "⚠️ لا توجد كلمات مسجلة بعد.")
        return
    item = random.choice(rows)
    word = item["content"]
    letters = list(word)
    random.shuffle(letters)
    scrambled = " ".join(letters)
    active_games[chat_id] = {"type": "scramble", "answer": word.lower()}
    try:
        await bot.send_message(chat_id,
            f"🔤 *رتب الحروف*\n\n"
            f"الحروف دي مبعثرة، رتبها واكتب الكلمة:\n\n"
            f"👉 `{scrambled}`\n\n"
            "⏱ عندك 60 ثانية",
            parse_mode=ParseMode.MARKDOWN)
    except: pass

async def start_questions_game(bot, chat_id):
    rows = db.list_game_content(GAME_QUESTIONS)
    if not rows:
        await bot.send_message(chat_id, "⚠️ لا توجد أسئلة مسجلة بعد.")
        return
    item = random.choice(rows)
    question = item["content"]
    answer = (item["answer"] or "").strip()
    active_games[chat_id] = {"type": "questions", "answer": normalize_text(answer)}
    try:
        await bot.send_message(chat_id,
            f"❓ *سؤال*\n\n{question}\n\n"
            "اكتب الإجابة الصحيحة 👇\n"
            "⏱ عندك 60 ثانية",
            parse_mode=ParseMode.MARKDOWN)
    except: pass

async def start_english_game(bot, chat_id):
    rows = db.list_game_content(GAME_ENGLISH)
    if not rows:
        await bot.send_message(chat_id, "⚠️ لا توجد كلمات إنجليزية مسجلة بعد.")
        return
    item = random.choice(rows)
    word = item["content"]
    answer = (item["answer"] or "").strip()
    active_games[chat_id] = {"type": "english", "answer": normalize_text(answer)}
    try:
        await bot.send_message(chat_id,
            f"🇬🇧 *إنجليزي*\n\n"
            f"الكلمة: `{word}`\n\n"
            "اكتب معناها بالعربي 👇\n"
            "⏱ عندك 60 ثانية",
            parse_mode=ParseMode.MARKDOWN)
    except: pass

async def start_calc_game(bot, chat_id):
    a = random.randint(1, 50)
    b = random.randint(1, 50)
    op = random.choice(["+", "-", "×"])
    if op == "+": ans = a + b
    elif op == "-": ans = a - b
    else: ans = a * b
    active_games[chat_id] = {"type": "calc", "answer": str(ans)}
    try:
        await bot.send_message(chat_id,
            f"🔢 *احسب*\n\n"
            f"`{a} {op} {b} = ؟`\n\n"
            "اكتب الناتج 👇\n"
            "⏱ عندك 60 ثانية",
            parse_mode=ParseMode.MARKDOWN)
    except: pass

async def start_timer_game(bot, chat_id):
    target_ms = random.randint(5000, 30000)
    target_sec = target_ms / 1000.0
    timer_games[chat_id] = {
        "target_ms": target_ms,
        "start_time": time.time(),
        "clicks": {},
        "finished": False
    }
    kb = M([[B("⏱ اضغط!", callback_data=f"timer:click:{chat_id}")]])
    try:
        await bot.send_message(chat_id,
            f"⏱ *المؤقت*\n\n"
            f"الهدف: *{target_sec:.2f}* ثانية\n\n"
            f"دوس على الزر عشان توقف المؤقت في الوقت الصح 👇\n"
            f"⏳ عندك 60 ثانية",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except: pass
    asyncio.create_task(_timer_finish(bot, chat_id))

async def _timer_finish(bot, chat_id):
    await asyncio.sleep(60)
    g = timer_games.get(chat_id)
    if not g or g.get("finished"): return
    g["finished"] = True
    if not g["clicks"]:
        try:
            await bot.send_message(chat_id, "⏱ خلص الوقت ومحدش داس!")
        except: pass
        timer_games.pop(chat_id, None)
        return
    results = []
    for uid, (name, click_ms) in g["clicks"].items():
        diff = abs(click_ms - g["target_ms"])
        results.append((uid, name, click_ms, diff))
    results.sort(key=lambda x: x[3])
    winner = results[0]
    db.add_points(chat_id, winner[0], 10, count_message=False)
    lines = [f"⏱ *انتهى المؤقت!*\nالهدف كان: *{g['target_ms']/1000:.2f}* ثانية\n"]
    medals = ["🥇","🥈","🥉"]
    for i, (uid, name, click_ms, diff) in enumerate(results[:10], 1):
        prefix = medals[i-1] if i <= 3 else f"{i}."
        lines.append(f"{prefix} {escape_html(name)} — {click_ms/1000:.2f}ث (فرق {diff/1000:.2f})")
    lines.append(f"\n🎉 *{escape_html(winner[1])}* كسب +10 نقاط!")
    try:
        await bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except: pass
    timer_games.pop(chat_id, None)

# ============ نهاية الجزء 1 ============

# ============ بداية الجزء 2 ============

# ==================== الأزرار ====================
def main_menu_kb(owner: bool):
    rows = [
        [B("➕ أضفني إلى مجموعتك", url=f"https://t.me/{BOT_USERNAME}?startgroup=true&admin=delete_messages+invite_users+restrict_members+pin_messages+manage_chat+delete_stories")],
        [B("⚙️ إدارة مجموعاتي", callback_data="panel:list")],
        [B("🎮 الألعاب", callback_data="games:menu")],
        [B("📊 إحصائياتي", callback_data="stats:me")],
        [B("🏅 توب 10", callback_data="stats:top")],
        [B("📖 الأوامر", callback_data="commands:show")],
    ]
    if owner:
        rows.append([B("👑 إعدادات المالك", callback_data="owner:menu")])
    return M(rows)

def owner_menu_kb():
    return M([
        [B("📢 اشتراكات البوت", callback_data="owner:subs")],
        [B("💬 الردود التلقائية", callback_data="owner:ar")],
        [B("🏆 إدارة الألعاب", callback_data="owner:games")],
        [B("🌐 كل جروبات البوت", callback_data="owner:allgroups")],
        [B("📤 تصدير قاعدة البيانات", callback_data="owner:backup_export")],
        [B("📥 استعادة قاعدة البيانات", callback_data="owner:backup_restore")],
        [B("🔙 رجوع", callback_data="panel:home")],
    ])

def owner_subs_kb():
    return M([
        [B("➕ إضافة قناة", callback_data="owner:add")],
        [B("🗑 حذف قناة", callback_data="owner:del_list")],
        [B("📋 عرض القنوات", callback_data="owner:show")],
        [B("🔙 رجوع", callback_data="owner:menu")],
    ])

def owner_ar_kb():
    return M([
        [B("➕ إضافة رد", callback_data="ar:add")],
        [B("🗑 حذف رد", callback_data="ar:del_list")],
        [B("📋 عرض الردود", callback_data="ar:show")],
        [B("🔙 رجوع", callback_data="owner:menu")],
    ])

def owner_games_kb():
    return M([
        [B("⚡ كلمات الأسرع", callback_data="og:fastest")],
        [B("🔤 كلمات رتب الحروف", callback_data="og:scramble")],
        [B("❓ أسئلة عامة", callback_data="og:questions")],
        [B("🇬🇧 كلمات إنجليزي", callback_data="og:english")],
        [B("🔙 رجوع", callback_data="owner:menu")],
    ])

def owner_game_kb(game):
    return M([
        [B("➕ إضافة", callback_data=f"og:add:{game}")],
        [B("🗑 حذف", callback_data=f"og:del_list:{game}")],
        [B("📋 عرض", callback_data=f"og:show:{game}")],
        [B("🔙 رجوع", callback_data="owner:games")],
    ])

def owner_game_delete_kb(game, items):
    rows = []
    for it in items:
        content = (it["content"] or "")[:30]
        if game in (GAME_QUESTIONS, GAME_ENGLISH):
            ans = (it["answer"] or "")[:15]
            label = f"🗑 {content} ← {ans}"
        else:
            label = f"🗑 {content}"
        rows.append([B(label, callback_data=f"og:del:{it['id']}:{game}")])
    rows.append([B("🔙 رجوع", callback_data=f"og:menu:{game}")])
    return M(rows)

def cancel_kb(target):
    return M([[B("🔙 إلغاء", callback_data=f"owner:cancel:{target}")]])

def games_menu_kb():
    return M([[B("🔙 رجوع", callback_data="panel:home")]])

def groups_list_kb(groups):
    rows = []
    for g in groups:
        title = (g['title'] or str(g['chat_id']))[:30]
        rows.append([B(f"⚙️ {title}", callback_data=f"panel:g:{g['chat_id']}")])
        rows.append([B(f"🗑 حذف", callback_data=f"panel:forget:{g['chat_id']}")])
    rows.append([B("🔙 رجوع", callback_data="panel:home")])
    return M(rows)

def all_groups_kb(groups):
    rows = []
    for g in groups:
        title = (g["title"] or str(g["chat_id"]))[:35]
        rows.append([B(f"📌 {title}", callback_data=f"owner:goto:{g['chat_id']}")])
    rows.append([B("🔙 رجوع", callback_data="owner:menu")])
    return M(rows)

def group_panel_kb(group, ch_count, antilink=False):
    rows = []
    if group["activated"]:
        rows.append([B("⛔ تعطيل البوت", callback_data=f"panel:off:{group['chat_id']}")])
    else:
        rows.append([B("✅ تفعيل البوت", callback_data=f"panel:on:{group['chat_id']}")])
    rows.append([B("➕ إضافة قناة", callback_data=f"panel:add:{group['chat_id']}")])
    if ch_count > 0:
        rows.append([B(f"📋 القنوات ({ch_count})", callback_data=f"panel:chs:{group['chat_id']}")])
    rows.append([B("🛡 الحماية", callback_data=f"panel:protect:{group['chat_id']}")])
    rows.append([B("🔄 تحديث", callback_data=f"panel:g:{group['chat_id']}"), B("🔙 رجوع", callback_data="panel:list")])
    return M(rows)

def protect_kb(chat_id, antilink=False):
    al_label = "🔗 منع الروابط: ✅" if antilink else "🔗 منع الروابط: ❌"
    return M([
        [B(al_label, callback_data=f"protect:al:{chat_id}")],
        [B("🚫 كلمات ممنوعة", callback_data=f"protect:bw:{chat_id}")],
        [B("🔙 رجوع", callback_data=f"panel:g:{chat_id}")],
    ])

def banned_words_kb(chat_id, words):
    rows = []
    for w in words:
        label = f"🗑 {w['word'][:25]}"
        rows.append([B(label, callback_data=f"protect:bwd:{w['id']}")])
    rows.append([B("➕ إضافة كلمة", callback_data=f"protect:bwa:{chat_id}")])
    rows.append([B("🔙 رجوع", callback_data=f"panel:protect:{chat_id}")])
    return M(rows)

def channels_kb(cid, chans):
    rows = [[B(f"🗑 {c['title'] or c['username'] or c['channel_id']}", callback_data=f"panel:del:{cid}:{c['channel_id']}")] for c in chans]
    rows.append([B("🔙 رجوع", callback_data=f"panel:g:{cid}")])
    return M(rows)

def default_channels_kb(chans):
    rows = [[B(f"🗑 {c['title'] or c['username'] or c['channel_id']}", callback_data=f"owner:del:{c['channel_id']}")] for c in chans]
    rows.append([B("🔙 رجوع", callback_data="owner:subs")])
    return M(rows)

def ar_delete_kb(replies):
    rows = []
    for r in replies:
        trigger = r["trigger"][:20]
        response = r["response"][:25]
        label = f"🗑 {trigger} ← {response}"
        rows.append([B(label, callback_data=f"ar:del:{r['id']}")])
    rows.append([B("🔙 رجوع", callback_data="owner:ar")])
    return M(rows)

def subscribe_kb(chans, uid):
    rows = []
    for c in chans:
        if c.get("link"):
            rows.append([B(f"📢 {c['title'] or 'اشترك'}", url=c["link"])])
    rows.append([B("✅ تحقق من اشتراكي", callback_data=f"sub:ck:{uid}")])
    return M(rows)

# ==================== نصوص ====================
def main_text(user):
    return (f"أهلاً بك {user.first_name} 👋\n\n"
            "🤖 *بوت Laila*\n\n"
            "✨ *بيعمل إيه؟*\n"
            "🛡 اشتراك إجباري للقنوات\n"
            "⚙️ إدارة جروبات وحماية\n"
            "🎮 ألعاب ومسابقات\n"
            "🏆 نظام نقاط وتوب\n\n"
            "اختار من الأزرار تحت 👇")

def owner_text():
    chans = db.list_default_channels()
    replies = db.list_auto_replies()
    groups = db.list_groups()
    games_count = db.count_all_games()
    cur = "_لا توجد_" if not chans else f"{len(chans)} قناة"
    return ("👑 *إعدادات المالك*\n\n"
            f"📢 الاشتراكات: {cur}\n"
            f"💬 الردود التلقائية: {len(replies)} رد\n"
            f"🌐 عدد الجروبات: {len(groups)}\n"
            f"🎮 عدد الألعاب: {games_count}\n\n"
            "اختر القسم:")

def owner_subs_text():
    chans = db.list_default_channels()
    cur = "_لا توجد_" if not chans else "\n".join(f"• {c['title'] or c['username'] or c['channel_id']}" for c in chans)
    return ("📢 *اشتراكات البوت*\n\n"
            "القنوات الافتراضية تُطلب من أي شخص يفتح البوت في الخاص.\n\n"
            f"الحالية:\n{cur}")

def owner_ar_text():
    replies = db.list_auto_replies()
    return ("💬 *الردود التلقائية*\n\n"
            f"عدد الردود: {len(replies)}\n\n"
            "⚠️ *التطابق ذكي*\n\n"
            "🎯 *ردود مدمجة:*\n"
            "• *المالك* → يرد بمالك الجروب\n"
            "• *الأدمن* → يرد بالأدمنز")

def owner_games_text():
    fastest = db.count_game_content(GAME_FASTEST)
    scramble = db.count_game_content(GAME_SCRAMBLE)
    questions = db.count_game_content(GAME_QUESTIONS)
    english = db.count_game_content(GAME_ENGLISH)
    return ("🏆 *إدارة الألعاب*\n\n"
            f"⚡ كلمات الأسرع: *{fastest}*\n"
            f"🔤 كلمات رتب الحروف: *{scramble}*\n"
            f"❓ أسئلة عامة: *{questions}*\n"
            f"🇬🇧 كلمات إنجليزي: *{english}*\n\n"
            "اختر القسم:")

def game_name(g):
    return {
        GAME_FASTEST: "⚡ الأسرع",
        GAME_SCRAMBLE: "🔤 رتب الحروف",
        GAME_QUESTIONS: "❓ أسئلة عامة",
        GAME_ENGLISH: "🇬🇧 إنجليزي",
    }.get(g, g)

def owner_game_text(game):
    count = db.count_game_content(game)
    name = game_name(game)
    if game == GAME_QUESTIONS:
        return (f"{name}\n\nعدد الأسئلة: *{count}*\n\n"
                "لإضافة سؤال: هتكتب السؤال، وبعدين تكتب الإجابة")
    if game == GAME_ENGLISH:
        return (f"{name}\n\nعدد الكلمات: *{count}*\n\n"
                "لإضافة كلمة: هتكتب الكلمة الإنجليزية، وبعدين المعنى بالعربي")
    return (f"{name}\n\nعدد الكلمات: *{count}*\n\n"
            "لإضافة كلمة: اكتبها عادي")

def channel_prompt_text():
    return ("📢 *أرسل القناة الآن*\n\n"
            "• يوزر القناة مثل `@MyChannel`\n"
            "• أو رابطها `https://t.me/MyChannel`\n"
            "• أو توجيه رسالة منها\n\n"
            "⚠️ يجب أن يكون البوت مشرفاً في القناة.")

def ar_trigger_prompt_text():
    return ("💬 *اكتب الكلمة اللي عاوز البوت يرد عليها*\n\n"
            "مثال: `صباح الخير`\n\n"
            "⚠️ البوت هيرد بس لما الرسالة تكون *نفس الكلمة*.")

def ar_response_prompt_text(trigger):
    return (f"✅ الكلمة: *{trigger}*\n\n"
            "📝 دلوقتي ابعت *الرد* اللي عاوز البوت يقوله:")

def game_add_prompt_text(game):
    if game == GAME_QUESTIONS:
        return ("❓ *إضافة سؤال*\n\n"
                "اكتب السؤال دلوقتي:\n"
                "مثال: `عاصمة مصر إيه؟`")
    if game == GAME_ENGLISH:
        return ("🇬🇧 *إضافة كلمة إنجليزية*\n\n"
                "اكتب الكلمة الإنجليزية:\n"
                "مثال: `Book`")
    return (f"{game_name(game)}\n\n"
            "اكتب الكلمة أو الجملة اللي عاوز تضيفها:")

def game_answer_prompt_text(content, game):
    if game == GAME_QUESTIONS:
        return (f"السؤال: *{content}*\n\n"
                "اكتب الإجابة الصحيحة:")
    if game == GAME_ENGLISH:
        return (f"الكلمة: *{content}*\n\n"
                "اكتب المعنى بالعربي:")
    return "اكتب الإجابة:"

# ============ بداية الجزء 3 ============

def games_in_group_text():
    return ("🎮 *الألعاب المتاحة*\n\n"
            "🔢 خمن الرقم\n"
            "⚡ الأسرع\n"
            "🔤 رتب الحروف\n"
            "❓ أسئلة عامة\n"
            "🇬🇧 إنجليزي\n"
            "🔢 احسب\n"
            "⏱ مؤقت\n\n"
            "📌 اكتب اسم اللعبة اللي عاوزها")

def games_private_text():
    return ("🎮 *الألعاب*\n\n"
            "الألعاب بتتلعب في الجروبات بس 👇\n\n"
            "🔢 خمن الرقم\n"
            "⚡ الأسرع\n"
            "🔤 رتب الحروف\n"
            "❓ أسئلة عامة\n"
            "🇬🇧 إنجليزي\n"
            "🔢 احسب\n"
            "⏱ مؤقت\n\n"
            "📌 عشان تلعب:\n"
            "1. اكتب *الألعاب* في الجروب\n"
            "2. اكتب اسم اللعبة\n\n"
            "⭐ *نظام النقاط:*\n"
            "• رسالة عادية → +1\n"
            "• رد على حد → +2\n"
            "• فوز في لعبة → +10")

def commands_text():
    return ("📖 *الأوامر الإدارية*\n\n"
            "الأوامر دي للمشرفين والمالك بس 👇\n\n"
            "🚫 *حظر* (بالرد)\n"
            "🔓 *الغاء الحظر* (بالرد)\n"
            "🔇 *كتم* (بالرد)\n"
            "🔊 *الغاء الكتم* (بالرد)\n"
            "⚠️ *تحذير* (بالرد)\n"
            "✅ *الغاء التحذير* (بالرد)\n"
            "🗑 *مسح* (بالرد)\n\n"
            "🎮 *تفعيل الالعاب* → تشغيل الألعاب\n"
            "🛑 *تعطيل الالعاب* → إيقاف الألعاب\n\n"
            "🎯 *في الجروب:*\n"
            "اكتب *الألعاب* عشان تشوف كل الألعاب")

def protect_text(antilink, banned_count):
    al = "✅ مفعّل" if antilink else "❌ معطّل"
    return ("🛡 *الحماية*\n\n"
            f"🔗 منع الروابط: {al}\n"
            f"🚫 كلمات ممنوعة: *{banned_count}*\n\n"
            "اختر:")

async def safe_edit(q, text, **kw):
    try: await q.edit_message_text(text, **kw)
    except Exception as e:
        if "not modified" not in str(e).lower():
            try: await q.message.reply_text(text, **kw)
            except: pass

async def require_sub(update, ctx):
    if is_owner(update.effective_user.id): return True
    chans = db.list_default_channels()
    if not chans: return True
    miss = await missing_channels(ctx.bot, chans, update.effective_user.id)
    if not miss: return True
    await update.message.reply_text(
        f"⚠️ عذراً {update.effective_user.first_name}!\n"
        "يجب الاشتراك في القنوات أولاً.\n\n"
        "اشترك ثم اضغط \"تحقق من اشتراكي\".",
        reply_markup=subscribe_kb(miss, update.effective_user.id))
    return False

# ==================== أوامر ====================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in GROUP_TYPES:
        try: await update.message.delete()
        except: pass
        return
    if update.effective_chat.type != ChatType.PRIVATE: return
    u = update.effective_user
    db.save_user(u.id, u.first_name, u.username)
    if not await require_sub(update, ctx): return
    await update.message.reply_text(main_text(u),
        parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb(is_owner(u.id)))

async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE: return
    if not is_owner(update.effective_user.id): return
    if not os.path.exists(db.DB_PATH):
        return await update.message.reply_text("❌ ملف قاعدة البيانات مش موجود.")
    try:
        with open(db.DB_PATH, "rb") as f:
            await ctx.bot.send_document(
                chat_id=update.effective_user.id,
                document=f,
                filename="data.db",
                caption="📦 نسخة احتياطية من قاعدة البيانات.\nاحتفظ بيها، وابعتها للبوت في الخاص لو احتجت تستعيدها."
            )
    except Exception as e:
        log.error(f"فشل إرسال النسخة الاحتياطية: {e}")
        await update.message.reply_text(f"❌ حصل خطأ أثناء إرسال الملف: {e}")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE: return
    if not is_owner(update.effective_user.id): return
    groups = db.list_groups()
    active = [g for g in groups if g["activated"]]
    lines = ["📊 *إحصائيات البوت*","",
             f"المجموعات: {len(groups)}",
             f"المفعّلة: {len(active)}",
             f"المعطّلة: {len(groups)-len(active)}",
             f"الردود: {len(db.list_auto_replies())}",
             f"الألعاب: {db.count_all_games()}"]
    if active:
        lines += ["","*المفعّلة:*"]
        for g in active[:30]:
            lines.append(f"• {g['title'] or g['chat_id']} — {db.count_channels(g['chat_id'])} قناة")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ==================== Callbacks ====================
async def cb_home(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    db.clear_pending(q.from_user.id)
    await safe_edit(q, main_text(q.from_user), parse_mode=ParseMode.MARKDOWN,
                    reply_markup=main_menu_kb(is_owner(q.from_user.id)))

async def cb_commands(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await safe_edit(q, commands_text(), parse_mode=ParseMode.MARKDOWN,
                    reply_markup=M([[B("🔙 رجوع", callback_data="panel:home")]]))

async def cb_owner_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer(); db.clear_pending(q.from_user.id)
    await safe_edit(q, owner_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=owner_menu_kb())

# ===== نسخ احتياطي لقاعدة البيانات =====
async def cb_owner_backup_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer()
    if not os.path.exists(db.DB_PATH):
        return await q.message.reply_text("❌ ملف قاعدة البيانات مش موجود.")
    try:
        with open(db.DB_PATH, "rb") as f:
            await ctx.bot.send_document(
                chat_id=q.from_user.id, document=f, filename="data.db",
                caption="📦 نسخة احتياطية من قاعدة البيانات.\nاحتفظ بيها، وابعتها للبوت في الخاص لو احتجت تستعيدها."
            )
    except Exception as e:
        log.error(f"فشل إرسال النسخة الاحتياطية: {e}")
        await q.message.reply_text(f"❌ حصل خطأ أثناء إرسال الملف: {e}")

async def cb_owner_backup_restore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer()
    await q.message.reply_text(
        "📥 ابعت دلوقتي ملف *data.db* هنا في الخاص كمرفق (Document)، وهيتم استبدال قاعدة البيانات الحالية بيه تلقائياً.",
        parse_mode=ParseMode.MARKDOWN
    )

# ===== اشتراكات البوت =====
async def cb_owner_subs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer(); db.clear_pending(q.from_user.id)
    await safe_edit(q, owner_subs_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=owner_subs_kb())

async def cb_owner_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer(); db.set_pending(q.from_user.id, PENDING_DEFAULT)
    await safe_edit(q, channel_prompt_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb(PENDING_DEFAULT))

async def cb_owner_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer()
    chans = db.list_default_channels()
    if not chans:
        return await safe_edit(q, "📋 لا توجد قنوات افتراضية.", reply_markup=owner_subs_kb())
    text = "📋 *القنوات الافتراضية*\n\n" + "\n".join(f"• {c['title'] or c['username'] or c['channel_id']}" for c in chans)
    await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=default_channels_kb(chans))

async def cb_owner_del_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer()
    chans = db.list_default_channels()
    if not chans:
        return await safe_edit(q, "📋 لا توجد قنوات لحذفها.", reply_markup=owner_subs_kb())
    await safe_edit(q, "🗑 *اضغط على قناة لحذفها:*", parse_mode=ParseMode.MARKDOWN, reply_markup=default_channels_kb(chans))

async def cb_owner_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    chid = int(q.data.split(":")[2])
    db.remove_default_channel(chid)
    await q.answer("🗑 تم الحذف.")
    chans = db.list_default_channels()
    if not chans:
        return await safe_edit(q, "📋 لا توجد قنوات.", reply_markup=owner_subs_kb())
    await safe_edit(q, "🗑 *اضغط على قناة لحذفها:*", parse_mode=ParseMode.MARKDOWN, reply_markup=default_channels_kb(chans))

async def cb_owner_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); db.clear_pending(q.from_user.id)
    await safe_edit(q, owner_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=owner_menu_kb())

# ===== كل جروبات البوت =====
async def cb_owner_allgroups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer()
    groups = db.list_groups()
    if not groups:
        return await safe_edit(q, "🌐 لا توجد جروبات بعد.", reply_markup=owner_menu_kb())
    text = f"🌐 *كل جروبات البوت* ({len(groups)})\n\nاضغط على جروب لفتحه:"
    await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=all_groups_kb(groups))

async def cb_owner_goto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cid = int(q.data.split(":")[2])
    g = db.get_group(cid)
    if not g:
        return await q.answer("❌ الجروب مش موجود.", show_alert=True)
    if str(cid).startswith("-100"):
        short = str(cid)[4:]
        link = f"https://t.me/c/{short}/1"
    else:
        link = f"https://t.me/c/{abs(cid)}/1"
    kb = M([[B("📂 فتح الجروب", url=link)], [B("🔙 رجوع", callback_data="owner:allgroups")]])
    await q.edit_message_text(
        f"🌐 *{g['title'] or cid}*\n\n"
        f"🆔 `{cid}`\n"
        f"الحالة: {'✅ مفعل' if g['activated'] else '⛔ معطل'}\n"
        f"القنوات: {db.count_channels(cid)}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ===== الردود التلقائية =====
async def cb_owner_ar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer(); db.clear_pending(q.from_user.id)
    await safe_edit(q, owner_ar_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=owner_ar_kb())

async def cb_ar_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer()
    db.set_pending(q.from_user.id, PENDING_ADD_TRIGGER)
    await safe_edit(q, ar_trigger_prompt_text(), parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_kb(PENDING_ADD_TRIGGER))

async def cb_ar_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer()
    replies = db.list_auto_replies()
    if not replies:
        return await safe_edit(q, "📋 لا توجد ردود تلقائية بعد.", reply_markup=owner_ar_kb())
    lines = ["📋 *الردود التلقائية*\n"]
    for r in replies:
        lines.append(f"• *{r['trigger']}* → {r['response']}")
    text = "\n".join(lines)
    if len(text) > 4000: text = text[:4000] + "\n\n... (طويلة)"
    await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=owner_ar_kb())

async def cb_ar_del_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer()
    replies = db.list_auto_replies()
    if not replies:
        return await safe_edit(q, "📋 لا توجد ردود.", reply_markup=owner_ar_kb())
    await safe_edit(q, "🗑 *اضغط على الرد لحذفه:*", parse_mode=ParseMode.MARKDOWN,
                    reply_markup=ar_delete_kb(replies))

async def cb_ar_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    rid = int(q.data.split(":")[2])
    db.remove_auto_reply(rid)
    await q.answer("🗑 تم الحذف.")
    replies = db.list_auto_replies()
    if not replies:
        return await safe_edit(q, "📋 لا توجد ردود.", reply_markup=owner_ar_kb())
    await safe_edit(q, "🗑 *اضغط على الرد لحذفه:*", parse_mode=ParseMode.MARKDOWN,
                    reply_markup=ar_delete_kb(replies))

# ============ نهاية الجزء 3 ============

# ============ بداية الجزء 4 ============

# ===== إدارة الألعاب =====
async def cb_owner_games(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer(); db.clear_pending(q.from_user.id)
    await safe_edit(q, owner_games_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=owner_games_kb())

async def cb_og_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    await q.answer()
    game = q.data.split(":")[1]
    await safe_edit(q, owner_game_text(game), parse_mode=ParseMode.MARKDOWN, reply_markup=owner_game_kb(game))

async def cb_og_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    game = q.data.split(":")[2]
    await q.answer()
    if game in (GAME_QUESTIONS, GAME_ENGLISH):
        db.set_pending(q.from_user.id, PENDING_ADD_GAME_WORD, extra=f"q:{game}")
    else:
        db.set_pending(q.from_user.id, PENDING_ADD_GAME_WORD, extra=game)
    await safe_edit(q, game_add_prompt_text(game), parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_kb(PENDING_ADD_GAME_WORD))

async def cb_og_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    game = q.data.split(":")[2]
    await q.answer()
    items = db.list_game_content(game)
    if not items:
        return await safe_edit(q, "📋 لا توجد عناصر بعد.", reply_markup=owner_game_kb(game))
    lines = [f"📋 *{game_name(game)}* — العدد: {len(items)}\n"]
    for it in items[:50]:
        if game in (GAME_QUESTIONS, GAME_ENGLISH):
            lines.append(f"• {it['content']} ← {it['answer'] or ''}")
        else:
            lines.append(f"• {it['content']}")
    text = "\n".join(lines)
    if len(text) > 4000: text = text[:4000] + "\n... (طويلة)"
    await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=owner_game_kb(game))

async def cb_og_del_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    game = q.data.split(":")[2]
    await q.answer()
    items = db.list_game_content(game)
    if not items:
        return await safe_edit(q, "📋 لا توجد عناصر.", reply_markup=owner_game_kb(game))
    await safe_edit(q, "🗑 *اضغط على العنصر لحذفه:*", parse_mode=ParseMode.MARKDOWN,
                    reply_markup=owner_game_delete_kb(game, items))

async def cb_og_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id): return await q.answer("⛔ للمالك فقط.", show_alert=True)
    parts = q.data.split(":")
    item_id = int(parts[2])
    game = parts[3] if len(parts) > 3 else None
    db.remove_game_content(item_id)
    await q.answer("🗑 تم الحذف.")
    items = db.list_game_content(game) if game else []
    if not items:
        return await safe_edit(q, f"📋 لا توجد عناصر.", reply_markup=owner_game_kb(game))
    await safe_edit(q, "🗑 *اضغط على العنصر لحذفه:*", parse_mode=ParseMode.MARKDOWN,
                    reply_markup=owner_game_delete_kb(game, items))

# ===== إدارة المجموعات =====
async def cb_panel_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.message.chat.type != ChatType.PRIVATE: return
    db.clear_pending(q.from_user.id)
    groups = db.list_groups()[:50]
    if not is_owner(q.from_user.id):
        filtered = []
        for g in groups:
            if await is_user_admin(ctx.bot, g["chat_id"], q.from_user.id):
                filtered.append(g)
        groups = filtered
    groups = groups[:20]
    if not groups:
        return await safe_edit(q, "لا توجد مجموعات بعد 🤷", reply_markup=main_menu_kb(is_owner(q.from_user.id)))
    await safe_edit(q, "⚙️ *مجموعاتي*\n\nاختر مجموعة:",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=groups_list_kb(groups))

async def cb_panel_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; cid = int(q.data.split(":")[2])
    if not is_owner(q.from_user.id):
        if not await is_user_admin(ctx.bot, cid, q.from_user.id):
            return await q.answer("⛔ لست مشرفاً.", show_alert=True)
    db.forget_group(cid)
    await q.answer("🗑 تم حذف الجروب من القائمة.")
    groups = db.list_groups()[:50]
    if not is_owner(q.from_user.id):
        filtered = []
        for g in groups:
            if await is_user_admin(ctx.bot, g["chat_id"], q.from_user.id):
                filtered.append(g)
        groups = filtered
    groups = groups[:20]
    if not groups:
        return await safe_edit(q, "لا توجد مجموعات بعد 🤷", reply_markup=main_menu_kb(is_owner(q.from_user.id)))
    await safe_edit(q, "⚙️ *مجموعاتي*\n\nاختر مجموعة:",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=groups_list_kb(groups))

async def cb_panel_g(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); db.clear_pending(q.from_user.id)
    await send_group_panel(update, ctx, int(q.data.split(":")[2]), edit=True)

async def send_group_panel(update, ctx, cid, edit=True):
    q = update.callback_query
    group = db.get_group(cid)
    fb = main_menu_kb(is_owner(q.from_user.id))
    if not group:
        if edit: return await safe_edit(q, "❌ لم أعد موجوداً.", reply_markup=fb)
        return await q.message.reply_text("❌ لم أعد موجوداً.", reply_markup=fb)
    if not await can_manage(update, cid):
        if edit: return await safe_edit(q, "⛔ لست مشرفاً.", reply_markup=fb)
        return await q.message.reply_text("⛔ لست مشرفاً.", reply_markup=fb)
    chans = db.list_channels(cid)
    rights = await get_bot_rights(ctx.bot, cid, fresh=True)
    al = db.get_antilink(cid)
    games_state = "✅ مفعل" if db.get_games_enabled(cid) else "⛔ معطل"
    lines = [f"⚙️ *{group['title'] or cid}*","",
             f"الحالة: {'✅ مفعل' if group['activated'] else '⛔ معطل'}",
             f"الألعاب: {games_state}",
             f"القنوات المطلوبة: {'' if chans else '_لا توجد_'}"]
    for c in chans:
        lines.append(f"  • {c['title'] or c['username'] or c['channel_id']}")
    lines.append(f"صلاحية البوت: {'👮 مشرف' if rights['is_admin'] else '⚠️ ليس مشرفاً'}" +
                 (f" • {'حذف ✅' if rights['can_delete'] else 'حذف ❌'}" if rights['is_admin'] else ""))
    if not rights["is_admin"]: lines += ["", "⚠️ ارفع البوت مشرفاً أولاً."]
    elif not rights["can_delete"]: lines += ["", "⚠️ فعّل صلاحية *حذف الرسائل*."]
    elif not chans: lines += ["", "ℹ️ أضف قناة على الأقل."]
    text = "\n".join(lines)
    kb = group_panel_kb(group, len(chans), al)
    if edit: await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else: await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def cb_panel_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; cid = int(q.data.split(":")[2])
    if not await can_manage(update, cid): return await q.answer("⛔ لست مشرفاً.", show_alert=True)
    if db.count_channels(cid) >= MAX_CHANNELS_PER_GROUP:
        return await q.answer(f"⚠️ الحد {MAX_CHANNELS_PER_GROUP}.", show_alert=True)
    await q.answer(); db.set_pending(q.from_user.id, cid)
    await safe_edit(q, channel_prompt_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb(cid))

async def cb_panel_chs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; cid = int(q.data.split(":")[2])
    if not await can_manage(update, cid): return await q.answer("⛔ لست مشرفاً.", show_alert=True)
    await q.answer()
    chans = db.list_channels(cid)
    text = "📋 *قنوات هذه المجموعة*\n\nاضغط على أي قناة لحذفها:" if chans else "📋 لا توجد قنوات."
    await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=channels_kb(cid, chans))

async def cb_panel_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, _, cid, chid = q.data.split(":")
    cid, chid = int(cid), int(chid)
    if not await can_manage(update, cid): return await q.answer("⛔ لست مشرفاً.", show_alert=True)
    db.remove_channel(cid, chid)
    chans = db.list_channels(cid)
    g = db.get_group(cid)
    if not chans and g and g["activated"]:
        db.deactivate_group(cid)
        await q.answer("🗑 آخر قناة — تم التعطيل.")
        return await send_group_panel(update, ctx, cid)
    await q.answer("🗑 تم الحذف.")
    await safe_edit(q, "📋 *قنوات هذه المجموعة*\n\nاضغط على أي قناة لحذفها:",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=channels_kb(cid, chans))

async def cb_panel_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; cid = int(q.data.split(":")[2])
    if not await can_manage(update, cid): return await q.answer("⛔ لست مشرفاً.", show_alert=True)
    if db.count_channels(cid) == 0: return await q.answer("⚠️ أضف قناة أولاً.", show_alert=True)
    rights = await get_bot_rights(ctx.bot, cid, fresh=True)
    if not rights["is_admin"]: return await q.answer("⚠️ ارفع البوت مشرفاً.", show_alert=True)
    if not rights["can_delete"]: return await q.answer("⚠️ فعّل صلاحية الحذف.", show_alert=True)
    db.activate_group(cid, q.from_user.id)
    await q.answer("✅ تم التفعيل.")
    await send_group_panel(update, ctx, cid)
    names = "\n".join(f"• {c['title'] or c['username'] or c['channel_id']}" for c in db.list_channels(cid))
    try: await ctx.bot.send_message(cid, f"✅ تم تفعيل الاشتراك الإجباري.\n\n📢 القنوات:\n{names}")
    except: pass

async def cb_panel_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; cid = int(q.data.split(":")[2])
    if not await can_manage(update, cid): return await q.answer("⛔ لست مشرفاً.", show_alert=True)
    db.deactivate_group(cid); db.clear_pending(q.from_user.id)
    await q.answer("✅ تم التعطيل.")
    await send_group_panel(update, ctx, cid)
    try: await ctx.bot.send_message(cid, "⛔ تم تعطيل الاشتراك الإجباري.")
    except: pass

# ===== الحماية =====
async def cb_panel_protect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; cid = int(q.data.split(":")[2])
    if not await can_manage(update, cid): return await q.answer("⛔ لست مشرفاً.", show_alert=True)
    await q.answer()
    al = db.get_antilink(cid)
    words = db.list_banned_words(cid)
    await safe_edit(q, protect_text(al, len(words)), parse_mode=ParseMode.MARKDOWN, reply_markup=protect_kb(cid, al))

async def cb_protect_al(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; cid = int(q.data.split(":")[2])
    if not await can_manage(update, cid): return await q.answer("⛔ لست مشرفاً.", show_alert=True)
    current = db.get_antilink(cid)
    db.set_antilink(cid, not current)
    await q.answer("✅ تم التغيير.")
    al = db.get_antilink(cid)
    words = db.list_banned_words(cid)
    await safe_edit(q, protect_text(al, len(words)), parse_mode=ParseMode.MARKDOWN, reply_markup=protect_kb(cid, al))

async def cb_protect_bw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; cid = int(q.data.split(":")[2])
    if not await can_manage(update, cid): return await q.answer("⛔ لست مشرفاً.", show_alert=True)
    await q.answer()
    words = db.list_banned_words(cid)
    if not words:
        kb = M([
            [B("➕ إضافة كلمة", callback_data=f"protect:bwa:{cid}")],
            [B("🔙 رجوع", callback_data=f"panel:protect:{cid}")],
        ])
        return await safe_edit(q, "🚫 لا توجد كلمات ممنوعة بعد.", reply_markup=kb)
    await safe_edit(q, "🚫 *اضغط على كلمة لحذفها:*", parse_mode=ParseMode.MARKDOWN,
                    reply_markup=banned_words_kb(cid, words))

async def cb_protect_bwa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; cid = int(q.data.split(":")[2])
    if not await can_manage(update, cid): return await q.answer("⛔ لست مشرفاً.", show_alert=True)
    await q.answer()
    db.set_pending(q.from_user.id, PENDING_ADD_BANNED_WORD, extra=str(cid))
    await safe_edit(q, "🚫 *اكتب الكلمة اللي عاوز تمنعها:*", parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_kb(PENDING_ADD_BANNED_WORD))

async def cb_protect_bwd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    word_id = int(q.data.split(":")[2])
    db.remove_banned_word(word_id)
    await q.answer("🗑 تم الحذف.")

# ===== الألعاب (في الخاص) =====
async def cb_games_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_edit(q, games_private_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=games_menu_kb())

# ===== إحصائيات (في الخاص) =====
async def cb_stats_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    total = db.get_total_points(q.from_user.id)
    await safe_edit(q,
        f"📊 *إحصائياتك (كل الجروبات)*\n\n"
        f"👤 {escape_html(q.from_user.first_name)}\n"
        f"⭐ النقاط: *{total['points']}*\n"
        f"💬 الرسائل: *{total['messages']}*",
        parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb(is_owner(q.from_user.id)))

async def cb_stats_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    text = await build_global_top_reply(ctx.bot)
    await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=main_menu_kb(is_owner(q.from_user.id)))

# ============ نهاية الجزء 4 ============

# ============ بداية الجزء 5 ============

# ===== timer callback =====
async def cb_timer_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    cid = int(parts[2])
    uid = q.from_user.id
    game = timer_games.get(cid)
    if not game or game.get("finished"):
        return await q.answer("⏱ انتهى المؤقت!", show_alert=True)
    if uid in game["clicks"]:
        return await q.answer("✅ أنت سجلت بالفعل!", show_alert=True)
    click_ms = (time.time() - game["start_time"]) * 1000
    game["clicks"][uid] = (q.from_user.first_name, click_ms)
    await q.answer(f"✅ تم التسجيل! ({click_ms/1000:.2f}ث)")

# ==================== استقبال الرسائل في الخاص ====================
async def on_owner_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.endswith(".db"):
        return await update.message.reply_text("❌ ابعت ملف قاعدة البيانات بامتداد .db فقط.")
    await update.message.reply_text("⏳ جاري استعادة قاعدة البيانات...")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        if os.path.exists(db.DB_PATH):
            os.replace(db.DB_PATH, db.DB_PATH + ".bak")
        await tg_file.download_to_drive(db.DB_PATH)
        await update.message.reply_text("✅ تم استعادة البيانات بنجاح! أعد تشغيل البوت الآن من Railway (اضغط Restart في السيرفس) عشان يقرأ البيانات الجديدة.")
    except Exception as e:
        log.error(f"فشل استعادة قاعدة البيانات: {e}")
        await update.message.reply_text(f"❌ حصل خطأ أثناء الاستعادة: {e}")

async def on_private_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.document and is_owner(update.effective_user.id):
        return await on_owner_document(update, ctx)
    pending = db.get_pending(update.effective_user.id)
    if not pending:
        cid = update.effective_chat.id
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if text:
            try:
                await ctx.bot.send_chat_action(cid, "typing")
            except TelegramError:
                pass
            answer = await ask_laila(cid, uid, text, update.effective_user.first_name)
            try: await update.message.reply_text(answer)
            except TelegramError: pass
        return
    if int(time.time()*1000) - pending["created_at"] > CHANNEL_WAIT_TIMEOUT_MS:
        db.clear_pending(update.effective_user.id)
        return await update.message.reply_text("⏰ انتهت المهلة.", reply_markup=main_menu_kb(is_owner(update.effective_user.id)))

    target = pending["target_chat_id"]
    text = (update.message.text or "").strip()

    # ===== إضافة رد تلقائي - كلمة =====
    if target == PENDING_ADD_TRIGGER:
        if not text:
            return await update.message.reply_text("❌ ابعت الكلمة كنص.", reply_markup=cancel_kb(PENDING_ADD_TRIGGER))
        if len(text) > 100:
            return await update.message.reply_text("❌ الكلمة طويلة جدًا.", reply_markup=cancel_kb(PENDING_ADD_TRIGGER))
        db.set_pending(update.effective_user.id, PENDING_ADD_REPLY, extra=text)
        await update.message.reply_text(ar_response_prompt_text(text), parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=cancel_kb(PENDING_ADD_REPLY))
        return

    # ===== إضافة رد تلقائي - الرد =====
    if target == PENDING_ADD_REPLY:
        trigger = pending.get("extra")
        if not trigger:
            db.clear_pending(update.effective_user.id)
            return await update.message.reply_text("❌ حصل خطأ، ابدأ من جديد.", reply_markup=owner_ar_kb())
        if not text:
            return await update.message.reply_text("❌ ابعت الرد كنص.", reply_markup=cancel_kb(PENDING_ADD_REPLY))
        if len(text) > 500:
            return await update.message.reply_text("❌ الرد طويل جدًا.", reply_markup=cancel_kb(PENDING_ADD_REPLY))
        if db.auto_reply_exists(trigger, text):
            return await update.message.reply_text("⚠️ الرد ده موجود بالفعل لنفس الكلمة!", reply_markup=owner_ar_kb())
        db.add_auto_reply(trigger, text)
        db.clear_pending(update.effective_user.id)
        await update.message.reply_text(
            f"✅ تم إضافة الرد:\n\n*{trigger}* → {text}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=owner_ar_kb())
        return

    # ===== إضافة لعبة - الكلمة/السؤال =====
    if target == PENDING_ADD_GAME_WORD:
        extra = pending.get("extra") or ""
        if not text:
            return await update.message.reply_text("❌ ابعت الكلمة.", reply_markup=cancel_kb(PENDING_ADD_GAME_WORD))
        if len(text) > 300:
            return await update.message.reply_text("❌ طويل جدًا.", reply_markup=cancel_kb(PENDING_ADD_GAME_WORD))
        if extra.startswith("q:"):
            game = extra[2:]
            if db.game_content_exists(game, text):
                return await update.message.reply_text("⚠️ الكلمة/السؤال ده موجود بالفعل!", reply_markup=cancel_kb(PENDING_ADD_GAME_WORD))
            db.set_pending(update.effective_user.id, PENDING_ADD_GAME_ANSWER, extra=f"q:{game}:{text}")
            await update.message.reply_text(game_answer_prompt_text(text, game),
                parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb(PENDING_ADD_GAME_ANSWER))
        else:
            if db.game_content_exists(extra, text):
                return await update.message.reply_text("⚠️ الكلمة دي موجودة بالفعل!", reply_markup=cancel_kb(PENDING_ADD_GAME_WORD))
            db.add_game_content(extra, text)
            db.clear_pending(update.effective_user.id)
            await update.message.reply_text(f"✅ تم إضافة: *{text}*",
                parse_mode=ParseMode.MARKDOWN, reply_markup=owner_game_kb(extra))
        return

    # ===== إضافة لعبة - الإجابة =====
    if target == PENDING_ADD_GAME_ANSWER:
        extra = pending.get("extra") or ""
        if not extra.startswith("q:"):
            db.clear_pending(update.effective_user.id)
            return await update.message.reply_text("❌ حصل خطأ.")
        parts = extra.split(":", 2)
        game = parts[1]
        content = parts[2] if len(parts) > 2 else ""
        if not text:
            return await update.message.reply_text("❌ ابعت الإجابة.", reply_markup=cancel_kb(PENDING_ADD_GAME_ANSWER))
        if len(text) > 300:
            return await update.message.reply_text("❌ طويل جدًا.", reply_markup=cancel_kb(PENDING_ADD_GAME_ANSWER))
        db.add_game_content(game, content, text)
        db.clear_pending(update.effective_user.id)
        await update.message.reply_text(
            f"✅ تم إضافة:\n\n*{content}* ← {text}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=owner_game_kb(game))
        return

    # ===== إضافة كلمة ممنوعة =====
    if target == PENDING_ADD_BANNED_WORD:
        cid_str = pending.get("extra")
        if not cid_str:
            db.clear_pending(update.effective_user.id)
            return await update.message.reply_text("❌ حصل خطأ.")
        try: cid = int(cid_str)
        except:
            db.clear_pending(update.effective_user.id)
            return await update.message.reply_text("❌ حصل خطأ.")
        if not text:
            return await update.message.reply_text("❌ ابعت الكلمة.", reply_markup=cancel_kb(PENDING_ADD_BANNED_WORD))
        if db.banned_word_exists(cid, text):
            return await update.message.reply_text("⚠️ الكلمة دي موجودة بالفعل!", reply_markup=cancel_kb(PENDING_ADD_BANNED_WORD))
        db.add_banned_word(cid, text)
        db.clear_pending(update.effective_user.id)
        words = db.list_banned_words(cid)
        await update.message.reply_text(f"✅ تم منع كلمة: *{text}*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=banned_words_kb(cid, words))
        return

    # ===== إضافة قناة =====
    ident = extract_channel_identifier(update.message)
    if not ident:
        return await update.message.reply_text("❌ لم أتعرف على القناة.", reply_markup=cancel_kb(target))
    res = await resolve_channel(ctx.bot, ident)
    if not res["ok"]:
        return await update.message.reply_text(res["reason"], reply_markup=cancel_kb(target))
    ch = res["channel"]
    if not ch["link"]:
        return await update.message.reply_text("❌ قناة خاصة بدون رابط دعوة.", reply_markup=cancel_kb(target))
    db.clear_pending(update.effective_user.id)
    if target == PENDING_DEFAULT:
        db.add_default_channel(ch)
        await update.message.reply_text(f"✅ أُضيفت: *{ch['title']}*", parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text(owner_subs_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=owner_subs_kb())
        return
    db.add_channel(target, ch)
    g = db.get_group(target)
    auto = False
    if g and not g["activated"]:
        rights = await get_bot_rights(ctx.bot, target, fresh=True)
        if rights["can_delete"]:
            db.activate_group(target, update.effective_user.id); auto = True
    await update.message.reply_text(
        f"✅ أُضيفت: *{ch['title']}*" + ("\n\n🚀 وتم التفعيل تلقائياً." if auto else ""),
        parse_mode=ParseMode.MARKDOWN)
    await send_group_panel(update, ctx, target, edit=False)

# ==================== زر التحقق ====================
async def cb_sub_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    target_uid = int(parts[2]) if len(parts) > 2 else None
    if target_uid and q.from_user.id != target_uid:
        return await q.answer("هذا الزر ليس لك 🙂", show_alert=True)
    in_group = q.message.chat.type in GROUP_TYPES
    if in_group:
        g = db.get_group(q.message.chat.id)
        chans = db.list_channels(q.message.chat.id) if (g and g["activated"]) else []
    else:
        chans = db.list_default_channels()
    for c in chans: invalidate_sub(c["channel_id"], q.from_user.id)
    miss = [] if is_owner(q.from_user.id) else await missing_channels(ctx.bot, chans, q.from_user.id)
    if miss:
        txt = "❌ لم تشترك بعد!" if len(miss) == 1 else f"❌ باقي {len(miss)} قنوات."
        return await q.answer(txt, show_alert=True)
    await q.answer("✅ تم التحقق!")
    try: await q.message.delete()
    except: pass
    if in_group:
        db.clear_reminder(q.message.chat.id, q.from_user.id); return
    await q.message.chat.send_message(main_text(q.from_user), parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(is_owner(q.from_user.id)))

# ==================== مراقبة المجموعة ====================
def is_service(msg):
    return bool(
        getattr(msg, "new_chat_members", None) or
        getattr(msg, "left_chat_member", None) or
        getattr(msg, "new_chat_title", None) or
        getattr(msg, "new_chat_photo", None) or
        getattr(msg, "delete_chat_photo", None) or
        getattr(msg, "group_chat_created", None) or
        getattr(msg, "supergroup_chat_created", None) or
        getattr(msg, "channel_chat_created", None) or
        getattr(msg, "message_auto_delete_timer_changed", None) or
        getattr(msg, "migrate_to_chat_id", None) or
        getattr(msg, "migrate_from_chat_id", None) or
        getattr(msg, "pinned_message", None) or
        getattr(msg, "video_chat_scheduled", None) or
        getattr(msg, "video_chat_started", None) or
        getattr(msg, "video_chat_ended", None) or
        getattr(msg, "video_chat_participants_invited", None) or
        getattr(msg, "forum_topic_created", None) or
        getattr(msg, "forum_topic_edited", None) or
        getattr(msg, "forum_topic_closed", None) or
        getattr(msg, "forum_topic_reopened", None) or
        getattr(msg, "users_shared", None) or
        getattr(msg, "chat_shared", None) or
        getattr(msg, "write_access_allowed", None) or
        getattr(msg, "proximity_alert_triggered", None) or
        getattr(msg, "boost_added", None)
    )

def contains_link(text):
    if not text: return False
    return bool(re.search(r"(https?://|t\.me/|www\.|telegram\.me/)", text.lower()))

async def perform_mod_action(bot, msg, cid, reply_to, action):
    """بتنفّذ فعل إداري فعلي (حظر/كتم/تحذير/مسح...) على صاحب الرسالة reply_to."""
    target = reply_to.from_user
    target_id = target.id

    if action == "ban":
        if target_id == OWNER_ID:
            try: await msg.reply_text("⚠️ لا يمكن حظر المالك.")
            except TelegramError: pass
            return
        try:
            await bot.ban_chat_member(cid, target_id)
            await msg.reply_text(
                f"🚫 تم حظر <a href=\"tg://user?id={target_id}\">{escape_html(target.first_name)}</a>",
                parse_mode=ParseMode.HTML)
        except TelegramError as e:
            try: await msg.reply_text(f"⚠️ فشل الحظر: {e.message}")
            except TelegramError: pass

    elif action == "kick":
        if target_id == OWNER_ID:
            try: await msg.reply_text("⚠️ لا يمكن طرد المالك.")
            except TelegramError: pass
            return
        try:
            await bot.ban_chat_member(cid, target_id)
            await bot.unban_chat_member(cid, target_id, only_if_banned=True)
            await msg.reply_text(
                f"👢 تم طرد <a href=\"tg://user?id={target_id}\">{escape_html(target.first_name)}</a> "
                f"(يقدر يرجع الجروب تاني)",
                parse_mode=ParseMode.HTML)
        except TelegramError as e:
            try: await msg.reply_text(f"⚠️ فشل الطرد: {e.message}")
            except TelegramError: pass

    elif action == "unban":
        try:
            await bot.unban_chat_member(cid, target_id, only_if_banned=True)
            await msg.reply_text(
                f"🔓 تم فك الحظر عن <a href=\"tg://user?id={target_id}\">{escape_html(target.first_name)}</a>",
                parse_mode=ParseMode.HTML)
        except TelegramError as e:
            try: await msg.reply_text(f"⚠️ فشل: {e.message}")
            except TelegramError: pass

    elif action == "mute":
        if target_id == OWNER_ID:
            try: await msg.reply_text("⚠️ لا يمكن كتم المالك.")
            except TelegramError: pass
            return
        try:
            from telegram import ChatPermissions
            perms = ChatPermissions(can_send_messages=False)
            await bot.restrict_chat_member(cid, target_id, permissions=perms)
            await msg.reply_text(
                f"🔇 تم كتم <a href=\"tg://user?id={target_id}\">{escape_html(target.first_name)}</a>",
                parse_mode=ParseMode.HTML)
        except TelegramError as e:
            try: await msg.reply_text(f"⚠️ فشل الكتم: {e.message}")
            except TelegramError: pass

    elif action == "unmute":
        try:
            from telegram import ChatPermissions
            perms = ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True)
            await bot.restrict_chat_member(cid, target_id, permissions=perms)
            await msg.reply_text(
                f"🔊 تم فك الكتم عن <a href=\"tg://user?id={target_id}\">{escape_html(target.first_name)}</a>",
                parse_mode=ParseMode.HTML)
        except TelegramError as e:
            try: await msg.reply_text(f"⚠️ فشل: {e.message}")
            except TelegramError: pass

    elif action == "warn":
        count = db.add_warning(cid, target_id)
        try:
            await msg.reply_text(
                f"⚠️ تحذير لـ <a href=\"tg://user?id={target_id}\">{escape_html(target.first_name)}</a>\n"
                f"عدد التحذيرات: *{count}*",
                parse_mode=ParseMode.HTML)
        except TelegramError: pass

    elif action == "unwarn":
        db.clear_warnings(cid, target_id)
        try:
            await msg.reply_text(
                f"✅ تم مسح تحذيرات <a href=\"tg://user?id={target_id}\">{escape_html(target.first_name)}</a>",
                parse_mode=ParseMode.HTML)
        except TelegramError: pass

    elif action == "delete":
        try: await bot.delete_message(cid, reply_to.message_id)
        except TelegramError: pass
        try: await bot.delete_message(cid, msg.message_id)
        except TelegramError: pass

    elif action == "pin":
        try:
            await bot.pin_chat_message(cid, reply_to.message_id)
            try: await msg.reply_text("📌 تم تثبيت الرسالة.")
            except TelegramError: pass
        except TelegramError as e:
            try: await msg.reply_text(f"⚠️ فشل التثبيت: {e.message}")
            except TelegramError: pass

    elif action == "unpin":
        try:
            await bot.unpin_chat_message(cid, reply_to.message_id)
            try: await msg.reply_text("📌 تم فك التثبيت.")
            except TelegramError: pass
        except TelegramError as e:
            try: await msg.reply_text(f"⚠️ فشل: {e.message}")
            except TelegramError: pass

async def handle_admin_command(bot, msg, cid, uid, norm, reply_to):
    if not reply_to or not reply_to.from_user:
        return False
    target_id = reply_to.from_user.id
    if target_id == bot.id:
        return False

    sender_admin = await is_user_admin(bot, cid, uid)
    if not sender_admin and not is_owner(uid):
        return False

    action = None
    if norm in [normalize_text(t) for t in TRIGGER_BAN]: action = "ban"
    elif norm in [normalize_text(t) for t in TRIGGER_UNBAN]: action = "unban"
    elif norm in [normalize_text(t) for t in TRIGGER_MUTE]: action = "mute"
    elif norm in [normalize_text(t) for t in TRIGGER_UNMUTE]: action = "unmute"
    elif norm in [normalize_text(t) for t in TRIGGER_WARN]: action = "warn"
    elif norm in [normalize_text(t) for t in TRIGGER_UNWARN]: action = "unwarn"
    elif norm in [normalize_text(t) for t in TRIGGER_DELETE]: action = "delete"

    if not action:
        return False

    await perform_mod_action(bot, msg, cid, reply_to, action)
    return True

async def on_group_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg: return
    if is_service(msg): return
    if getattr(msg, "sender_chat", None): return
    if not update.effective_user or update.effective_user.is_bot: return

    cid = update.effective_chat.id
    uid = update.effective_user.id
    text = msg.text or ""
    norm = normalize_text(text)

    # ===== أوامر إدارية =====
    if msg.reply_to_message and text:
        handled = await handle_admin_command(ctx.bot, msg, cid, uid, norm, msg.reply_to_message)
        if handled: return

    # ===== أوامر الألعاب =====
    if text:
        games_on_norms = [normalize_text(t) for t in TRIGGER_GAMES_ON]
        games_off_norms = [normalize_text(t) for t in TRIGGER_GAMES_OFF]
        if norm in games_on_norms or norm in games_off_norms:
            if await is_user_admin(ctx.bot, cid, uid) or is_owner(uid):
                if norm in games_on_norms:
                    db.set_games_enabled(cid, True)
                    await msg.reply_text("✅ تم تفعيل الألعاب في الجروب.")
                else:
                    db.set_games_enabled(cid, False)
                    await msg.reply_text("🛑 تم تعطيل الألعاب في الجروب.")
                return

    # ===== الحماية =====
    g = db.get_group(cid)
    if g and g["activated"]:
        if db.get_antilink(cid) and contains_link(text):
            if not await is_user_admin(ctx.bot, cid, uid):
                try: await msg.delete()
                except: pass
                return
        bw = db.find_banned_word(cid, text)
        if bw and not await is_user_admin(ctx.bot, cid, uid):
            try: await msg.delete()
            except: pass
            return

    # ===== ليلى - الذكاء الاصطناعي =====
    if text:
        handled = await maybe_ai_reply(ctx.bot, msg, cid, uid, text, update.effective_user.first_name)
        if handled:
            return

    # ===== الردود الذكية =====
    if text:
        owner_norms = [normalize_text(t) for t in TRIGGER_OWNER]
        admin_norms = [normalize_text(t) for t in TRIGGER_ADMIN]
        top_norms = [normalize_text(t) for t in TRIGGER_TOP]
        my_norms = [normalize_text(t) for t in TRIGGER_MY_STATS]
        his_norms = [normalize_text(t) for t in TRIGGER_HIS_STATS]
        games_norms = [normalize_text(t) for t in TRIGGER_GAMES]

        if norm in owner_norms:
            reply = await build_owner_reply(ctx.bot, cid)
            if reply:
                try: await msg.reply_text(reply, parse_mode=ParseMode.HTML)
                except: pass
        elif norm in admin_norms:
            reply = await build_admins_reply(ctx.bot, cid)
            if reply:
                try: await msg.reply_text(reply, parse_mode=ParseMode.HTML)
                except: pass
        elif norm in top_norms:
            title = update.effective_chat.title
            reply = await build_top_reply(ctx.bot, cid, title)
            try: await msg.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
            except: pass
        elif norm in my_norms:
            reply = await build_my_stats_reply(ctx.bot, cid, uid, update.effective_user.first_name)
            try: await msg.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
            except: pass
        elif norm in his_norms and msg.reply_to_message:
            target_user = msg.reply_to_message.from_user
            if target_user:
                reply = await build_my_stats_reply(ctx.bot, cid, target_user.id, target_user.first_name)
                try: await msg.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
                except: pass
        elif norm in games_norms:
            if db.get_games_enabled(cid):
                try: await msg.reply_text(games_in_group_text(), parse_mode=ParseMode.MARKDOWN)
                except: pass
        elif norm in (normalize_text("خمن الرقم"), normalize_text("خمن رقم")):
            if db.get_games_enabled(cid): await start_number_game(ctx.bot, cid)
        elif norm in (normalize_text("الأسرع"), normalize_text("الاسرع"), normalize_text("اسرع")):
            if db.get_games_enabled(cid): await start_fastest_game(ctx.bot, cid)
        elif norm in (normalize_text("رتب الحروف"), normalize_text("رتب حروف")):
            if db.get_games_enabled(cid): await start_scramble_game(ctx.bot, cid)
        elif norm in (normalize_text("أسئلة عامة"), normalize_text("اسئلة عامة"), normalize_text("أسئلة"), normalize_text("اسئلة")):
            if db.get_games_enabled(cid): await start_questions_game(ctx.bot, cid)
        elif norm in (normalize_text("انجليزي"), normalize_text("إنجليزي"), normalize_text("انجلش")):
            if db.get_games_enabled(cid): await start_english_game(ctx.bot, cid)
        elif norm in (normalize_text("احسب"), normalize_text("رياضيات")):
            if db.get_games_enabled(cid): await start_calc_game(ctx.bot, cid)
        elif norm in (normalize_text("مؤقت"),):
            if db.get_games_enabled(cid): await start_timer_game(ctx.bot, cid)
        else:
            game = active_games.get(cid)
            if game:
                if game["type"] == "number":
                    try: guess = int(text.strip())
                    except: guess = None
                    if guess is not None:
                        if guess == game["answer"]:
                            db.add_points(cid, uid, 10, count_message=False)
                            del active_games[cid]
                            try: await msg.reply_text(f"🎉 *{escape_html(update.effective_user.first_name)}* كسب +10 نقاط! الرقم كان *{game['answer']}*", parse_mode=ParseMode.MARKDOWN)
                            except: pass
                        elif guess < game["answer"]:
                            try: await msg.reply_text("⬆️ أكبر")
                            except: pass
                        else:
                            try: await msg.reply_text("⬇️ أصغر")
                            except: pass
                elif game["type"] == "fastest":
                    if text.strip().lower() == game["answer"]:
                        db.add_points(cid, uid, 10, count_message=False)
                        del active_games[cid]
                        try: await msg.reply_text(f"🎉 *{escape_html(update.effective_user.first_name)}* كسب +10 نقاط! ⚡", parse_mode=ParseMode.MARKDOWN)
                        except: pass
                elif game["type"] == "scramble":
                    if normalize_text(text) == normalize_text(game["answer"]):
                        db.add_points(cid, uid, 10, count_message=False)
                        del active_games[cid]
                        try: await msg.reply_text(f"🎉 *{escape_html(update.effective_user.first_name)}* كسب +10 نقاط! 🔤", parse_mode=ParseMode.MARKDOWN)
                        except: pass
                elif game["type"] in ("questions", "english"):
                    if normalize_text(text) == game["answer"]:
                        db.add_points(cid, uid, 10, count_message=False)
                        del active_games[cid]
                        try: await msg.reply_text(f"🎉 *{escape_html(update.effective_user.first_name)}* كسب +10 نقاط! ✅", parse_mode=ParseMode.MARKDOWN)
                        except: pass
                elif game["type"] == "calc":
                    if text.strip() == game["answer"]:
                        db.add_points(cid, uid, 10, count_message=False)
                        del active_games[cid]
                        try: await msg.reply_text(f"🎉 *{escape_html(update.effective_user.first_name)}* كسب +10 نقاط! 🔢", parse_mode=ParseMode.MARKDOWN)
                        except: pass
                return

            reply = find_reply_for(text)
            if reply:
                try: await msg.reply_text(reply, parse_mode=ParseMode.HTML)
                except:
                    try: await msg.reply_text(reply)
                    except: pass

    if is_owner(uid): return
    if not g:
        db.remember_group(cid, update.effective_chat.title)
        u = update.effective_user
        db.save_user(u.id, u.first_name, u.username)
        return
    if g["title"] != update.effective_chat.title:
        db.remember_group(cid, update.effective_chat.title)
    u = update.effective_user
    db.save_user(u.id, u.first_name, u.username)

    text_stripped = text.strip()
    if text_stripped in ("تفعيل البوت","تعطيل البوت","لوحة التحكم","القنوات"):
        if await is_user_admin(ctx.bot, cid, uid):
            await msg.reply_text(f"⚙️ الإدارة كلها في الخاص:\nhttps://t.me/{BOT_USERNAME}?start=g_{cid}")
            return

    if not g["activated"]: return

    try:
        if text and not text.startswith("/"):
            db.add_points(cid, uid, 1, count_message=True)
        if msg.reply_to_message:
            db.add_points(cid, uid, 2, count_message=False)
    except: pass

    if await is_user_admin(ctx.bot, cid, uid): return
    chans = db.list_channels(cid)
    if not chans: return
    miss = await missing_channels(ctx.bot, chans, uid)
    if not miss: return
    rights = await get_bot_rights(ctx.bot, cid, fresh=True)
    if not rights["can_delete"]:
        r = db.get_reminder(cid, 0)
        if not r or int(time.time()*1000) - r["last_reminder"] > 10*60*1000:
            db.set_reminder(cid, 0, None)
            try: await msg.reply_text("⚠️ لا أستطيع الحذف — ارفعني مشرفاً بصلاحية \"حذف الرسائل\".")
            except: pass
        return
    try: await msg.delete()
    except Exception as e: log.warning(f"فشل الحذف في {cid}: {e}")
    await send_sub_prompt(update, ctx, miss)

async def send_sub_prompt(update, ctx, miss):
    cid = update.effective_chat.id
    uid = update.effective_user.id
    r = db.get_reminder(cid, uid)
    if r and int(time.time()*1000) - r["last_reminder"] < REMINDER_COOLDOWN_MS: return
    if r and r["prompt_message_id"]:
        await safe_delete(ctx.bot, cid, r["prompt_message_id"])
    mention = f'<a href="tg://user?id={uid}">{escape_html(update.effective_user.first_name)}</a>'
    what = "القناة" if len(miss) == 1 else f"الـ {len(miss)} قنوات"
    try:
        sent = await ctx.bot.send_message(cid,
            f"⛔ {mention} — تم حذف رسالتك.\n\nيجب الاشتراك في {what} بالأسفل.\nاشترك ثم اضغط \"تحقق من اشتراكي\".",
            parse_mode=ParseMode.HTML, reply_markup=subscribe_kb(miss, uid))
    except Exception as e:
        log.warning(f"فشل إرسال التنبيه: {e}"); return
    db.set_reminder(cid, uid, sent.message_id)
    delete_later(ctx.bot, cid, sent.message_id, PROMPT_AUTO_DELETE_MS)

# ==================== أحداث العضوية ====================
async def on_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    upd = update.my_chat_member
    chat = upd.chat
    if chat.type not in GROUP_TYPES: return
    status = upd.new_chat_member.status
    invalidate_bot_rights(chat.id)
    if status in ("left","kicked"):
        db.forget_group(chat.id); return
    db.remember_group(chat.id, chat.title)

async def on_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.chat_member
    invalidate_admin(u.chat.id, u.new_chat_member.user.id)

# ==================== تشغيل ====================
async def post_init(app: Application):
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username
    log.info(f"🤖 البوت: @{BOT_USERNAME}")
    log.info(f"👑 المالك: {OWNER_ID}")
    chans = db.list_default_channels()
    for c in chans:
        try:
            info = await app.bot.get_chat(c["channel_id"])
            m = await app.bot.get_chat_member(c["channel_id"], me.id)
            if m.status in ("administrator","creator"):
                log.info(f"✅ {info.title} — مشرف")
            else:
                log.warning(f"⚠️ ليس مشرفاً في {info.title}")
        except Exception as e:
            log.error(f"❌ {c['channel_id']}: {e}")

def main():
    db.init_db()
    if not db.list_default_channels() and INITIAL_DEFAULT_CHANNELS:
        for chid in INITIAL_DEFAULT_CHANNELS:
            db.add_default_channel({"id": chid, "title": None, "username": None, "link": None})

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(cb_home, pattern=r"^panel:home$"))
    app.add_handler(CallbackQueryHandler(cb_commands, pattern=r"^commands:show$"))
    app.add_handler(CallbackQueryHandler(cb_owner_menu, pattern=r"^owner:menu$"))
    app.add_handler(CallbackQueryHandler(cb_owner_backup_export, pattern=r"^owner:backup_export$"))
    app.add_handler(CallbackQueryHandler(cb_owner_backup_restore, pattern=r"^owner:backup_restore$"))
    app.add_handler(CallbackQueryHandler(cb_owner_subs, pattern=r"^owner:subs$"))
    app.add_handler(CallbackQueryHandler(cb_owner_add, pattern=r"^owner:add$"))
    app.add_handler(CallbackQueryHandler(cb_owner_show, pattern=r"^owner:show$"))
    app.add_handler(CallbackQueryHandler(cb_owner_del_list, pattern=r"^owner:del_list$"))
    app.add_handler(CallbackQueryHandler(cb_owner_del, pattern=r"^owner:del:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_owner_cancel, pattern=r"^owner:cancel:"))
    app.add_handler(CallbackQueryHandler(cb_owner_allgroups, pattern=r"^owner:allgroups$"))
    app.add_handler(CallbackQueryHandler(cb_owner_goto, pattern=r"^owner:goto:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_owner_ar, pattern=r"^owner:ar$"))
    app.add_handler(CallbackQueryHandler(cb_ar_add, pattern=r"^ar:add$"))
    app.add_handler(CallbackQueryHandler(cb_ar_show, pattern=r"^ar:show$"))
    app.add_handler(CallbackQueryHandler(cb_ar_del_list, pattern=r"^ar:del_list$"))
    app.add_handler(CallbackQueryHandler(cb_ar_del, pattern=r"^ar:del:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_owner_games, pattern=r"^owner:games$"))
    app.add_handler(CallbackQueryHandler(cb_og_menu, pattern=r"^og:(fastest|scramble|questions|english)$"))
    app.add_handler(CallbackQueryHandler(cb_og_add, pattern=r"^og:add:"))
    app.add_handler(CallbackQueryHandler(cb_og_show, pattern=r"^og:show:"))
    app.add_handler(CallbackQueryHandler(cb_og_del_list, pattern=r"^og:del_list:"))
    app.add_handler(CallbackQueryHandler(cb_og_del, pattern=r"^og:del:-?\d+:"))
    app.add_handler(CallbackQueryHandler(cb_panel_list, pattern=r"^panel:list$"))
    app.add_handler(CallbackQueryHandler(cb_panel_forget, pattern=r"^panel:forget:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_panel_g, pattern=r"^panel:g:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_panel_add, pattern=r"^panel:add:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_panel_chs, pattern=r"^panel:chs:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_panel_del, pattern=r"^panel:del:-?\d+:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_panel_on, pattern=r"^panel:on:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_panel_off, pattern=r"^panel:off:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_panel_protect, pattern=r"^panel:protect:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_protect_al, pattern=r"^protect:al:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_protect_bw, pattern=r"^protect:bw:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_protect_bwa, pattern=r"^protect:bwa:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_protect_bwd, pattern=r"^protect:bwd:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_games_menu, pattern=r"^games:menu$"))
    app.add_handler(CallbackQueryHandler(cb_stats_me, pattern=r"^stats:me$"))
    app.add_handler(CallbackQueryHandler(cb_stats_top, pattern=r"^stats:top$"))
    app.add_handler(CallbackQueryHandler(cb_sub_check, pattern=r"^sub:ck"))
    app.add_handler(CallbackQueryHandler(cb_timer_click, pattern=r"^timer:click:-?\d+$"))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_private_msg))
    app.add_handler(MessageHandler((filters.ChatType.GROUP | filters.ChatType.SUPERGROUP) & ~filters.COMMAND, on_group_msg))

    log.info("🚀 البوت يعمل...")
    app.run_polling(allowed_updates=["message","callback_query","my_chat_member","chat_member"])

if __name__ == "__main__":
    main()

# ============ نهاية الجزء 5 (نهاية الملف) ============
