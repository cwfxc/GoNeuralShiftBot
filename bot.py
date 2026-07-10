import os
import asyncio
import logging
import tempfile
import sqlite3
import hashlib
import json
import random
from collections import OrderedDict
from telegram.ext import PreCheckoutQueryHandler
from datetime import datetime, time as dt_time
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python <3.9 fallback
    from backports.zoneinfo import ZoneInfo
from telegram import LabeledPrice
from groq import Groq
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
HASH_SECRET = os.getenv("HASH_SECRET")
if not HASH_SECRET:
    raise RuntimeError(
        "HASH_SECRET is not set. Refusing to start: without it, user-hashing "
        "would silently fall back to a value visible in the public source code, "
        "which breaks the anonymity guarantee for diary entries."
    )

groq_client = Groq(api_key=GROQ_API_KEY)

# States
# Добавлено новое состояние THOUGHT_DIARY_EMOTION_RECHECK — шаг переоценки эмоции
# после переформулировки мысли (опора: Judith S. Beck, "Cognitive Behavior Therapy: Basics and Beyond")
(MAIN_MENU, AI_CHAT,
 THOUGHT_DIARY_SITUATION, THOUGHT_DIARY_EMOTION, THOUGHT_DIARY_THOUGHT,
 THOUGHT_DIARY_DISTORTION, THOUGHT_DIARY_REFRAME, THOUGHT_DIARY_EMOTION_RECHECK,
 DEFUSION_THOUGHT, DEFUSION_TECHNIQUE) = range(10)

COGNITIVE_DISTORTIONS = {
    "🔮 Чтение мыслей": "Убеждённость в том, что знаете мысли других без оснований.",
    "🌑 Катастрофизация": "Ожидание худшего исхода как неизбежного.",
    "🏷 Навешивание ярлыков": "Глобальный ярлык вместо описания конкретного поведения.",
    "🔬 Сверхобобщение": "Широкий вывод на основе единичного случая.",
    "👁 Фильтрация": "Фокус только на негативных деталях, игнорирование позитивных.",
    "⚫ Чёрно-белое мышление": "Видение всего в крайностях, без полутонов.",
    "⚡ Долженствование": "Жёсткие правила: 'должен', 'обязан', 'необходимо'.",
    "🔗 Персонализация": "Ответственность за вещи вне вашей власти.",
    "📉 Обесценивание": "Отвержение позитивного опыта как незначительного.",
    "💭 Эмоциональное мышление": "Убеждённость в чём-то только потому, что так чувствуете.",
}

# Техники дефузии основаны на ACT-протоколах:
# Hayes, S. C. — "Acceptance and Commitment Therapy: An Experiential Approach to Behavior Change"
# Harris, R. — "The Happiness Trap"
# =====================
# ЕЖЕДНЕВНЫЕ СООБЩЕНИЯ (подписка)
# =====================

DEFAULT_TZ = "Europe/Moscow"

MORNING_WINDOW = (8, 0, 11, 0)   # с 8:00 до 11:00
EVENING_WINDOW = (19, 0, 22, 0)  # с 19:00 до 22:00

# Смесь рефлексивных вопросов (Padesky, Socratic Questioning) и заземлённых аффирмаций.
# Аффирмации намеренно не превосходные ("я справлюсь со всем"), а опирающиеся на факты —
# исследования (Wood, Perunovic & Lee, 2009) показывают, что слишком позитивные утверждения
# могут не помогать, а иногда усиливать тревогу у людей со сниженной самооценкой.
PROMPTS_BANK = [
    "Какая мысль сегодня возвращалась чаще всего? Она тебе помогает или мешает?",
    "Если бы сегодняшний день прошёл в согласии с тем, что для тебя важно — как бы он выглядел?",
    "Есть что-то, что ты откладываешь из страха, а не потому что это правда не нужно?",
    "Что бы ты сказал другу, если бы он оказался в твоей сегодняшней ситуации?",
    "Какую мысль ты сегодня принял за факт, хотя это была просто мысль?",
    "Что из происходящего сейчас под твоим контролем, а что — нет?",
    "Если убрать оценку «хорошо/плохо» — что просто есть в твоём дне сегодня?",
    "Ты уже сталкивался с трудным раньше. Это не гарантия на сегодня, но это факт о тебе.",
    "Тебе не обязательно чувствовать себя готовым, чтобы начать.",
    "Прогресс редко выглядит как прямая линия — трудный день не отменяет то, что уже пройдено.",
    "Можно одновременно тревожиться и всё равно сделать шаг.",
    "Ты не обязан справляться со всем сразу. Достаточно следующего маленького шага.",
    "Даже если сегодня кажется, что всё стоит на месте — что-то внутри всё равно меняется.",
]

def get_random_prompt() -> str:
    return random.choice(PROMPTS_BANK)

DEFUSION_TECHNIQUES = {
    "｡ﾟ Листья на воде": (
        "Устройтесь поудобнее. Можно закрыть глаза, если хочется.\n\n"
        "Представьте тихий ручей — не обязательно красивый, просто спокойный. "
        "По воде медленно плывут листья.\n\n"
        "Возьмите эту мысль — ту самую — и просто положите её на один из листьев. "
        "Не пытайтесь её исправить или прогнать. Просто положите и смотрите, как уплывает.\n\n"
        "Вы на берегу. Мысль — на воде. Между вами есть расстояние.\n\n"
        "Побудьте здесь минуту. Это и есть практика — не избавиться от мысли, а не быть внутри неё."
    ),
    "✦ Наблюдатель": (
        "Попробуйте вот что — это немного странно, но работает.\n\n"
        "Представьте, что где-то внутри вас есть очень тихая, очень спокойная часть. "
        "Она всегда была там. Она видела всё что с вами происходило — и просто наблюдала.\n\n"
        "Эта часть сейчас тоже видит эту мысль. Без паники, без оценок.\n\n"
        "Попробуйте смотреть на мысль её глазами — как на облако, которое проплывает мимо.\n\n"
        "Вы не обязаны в неё верить. Она просто есть — и это нормально."
    ),
    "･ﾟ Персонаж": (
        "Маленький, но очень ощутимый сдвиг.\n\n"
        "Попробуйте добавить в начало фразу:\n"
        "*«У меня есть мысль о том, что...»*\n\n"
        "Например:\n"
        "Было → «Я со всем этим не справлюсь»\n"
        "Стало → «У меня есть мысль о том, что я со всем этим не справлюсь»\n\n"
        "Чувствуете разницу? Вы как будто делаете шаг назад и смотрите на мысль, "
        "а не смотрите на мир через неё.\n\n"
        "Мысль — это не факт. Это просто мысль. И вы — это не она."
    ),
}

# =====================
# DATABASE
# =====================

DB_PATH = "/app/data/goneuralshift.db"

def get_user_hash(user_id: int) -> str:
    """One-way hash of user_id — owner cannot reverse it to identify user."""
    return hashlib.sha256(f"{user_id}{HASH_SECRET}".encode()).hexdigest()

def _migrate_legacy_users_table(conn):
    """Migrate users table from raw-user_id keys to hashed keys (privacy fix)."""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not exists:
        return
    cols = [row[1] for row in conn.execute("PRAGMA table_info(users)")]
    if "user_id" not in cols:
        return  # already migrated
    conn.execute("ALTER TABLE users RENAME TO users_legacy")
    conn.execute("""
        CREATE TABLE users (
            user_hash TEXT PRIMARY KEY,
            sessions_count INTEGER DEFAULT 0,
            first_seen TEXT
        )
    """)
    rows = conn.execute("SELECT user_id, sessions_count, first_seen FROM users_legacy").fetchall()
    for user_id, sessions_count, first_seen in rows:
        conn.execute(
            "INSERT INTO users (user_hash, sessions_count, first_seen) VALUES (?, ?, ?)",
            (get_user_hash(user_id), sessions_count, first_seen)
        )
    conn.execute("DROP TABLE users_legacy")
    conn.commit()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    _migrate_legacy_users_table(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_hash TEXT PRIMARY KEY,
            sessions_count INTEGER DEFAULT 0,
            first_seen TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS diary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_hash TEXT NOT NULL,
            date TEXT NOT NULL,
            data TEXT NOT NULL
        )
    """)
    # ВАЖНО: в этой таблице user_id хранится НЕ хэшированным, в отличие от diary/users.
    # Причина: чтобы бот мог сам прислать сообщение, Telegram требует настоящий chat_id
    # (в приватных чатах он равен user_id) — необратимый хэш для этого не годится.
    # Подписка на рассылку — единственная часть бота, где это применимо, и она полностью
    # опциональна (кнопка вкл/выкл), в отличие от анонимного по умолчанию дневника мыслей.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            subscribed_at TEXT,
            timezone TEXT DEFAULT 'Europe/Moscow',
            active INTEGER DEFAULT 1
        )
    """)
    # Миграция для баз, созданных до появления колонки active.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(subscribers)")]
    if "active" not in cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN active INTEGER DEFAULT 1")
    conn.commit()
    conn.close()

def db_get_sessions(user_id: int) -> int:
    user_hash = get_user_hash(user_id)
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT sessions_count FROM users WHERE user_hash=?", (user_hash,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

def db_increment_sessions(user_id: int):
    user_hash = get_user_hash(user_id)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT INTO users (user_hash, sessions_count, first_seen)
            VALUES (?, 1, ?)
            ON CONFLICT(user_hash) DO UPDATE SET sessions_count = sessions_count + 1
        """, (user_hash, datetime.now().strftime("%d.%m.%Y")))
        conn.commit()
    finally:
        conn.close()

def db_delete_user_data(user_id: int):
    """Delete all diary entries and session stats for this user (privacy: right to erasure)."""
    user_hash = get_user_hash(user_id)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM diary WHERE user_hash=?", (user_hash,))
        conn.execute("DELETE FROM users WHERE user_hash=?", (user_hash,))
        conn.commit()
    finally:
        conn.close()
    user_sessions.pop(user_id, None)
    db_delete_subscriber_row(user_id)  # «Удалить мои данные» должно полностью стирать и подписку на рассылку

def db_save_entry(user_id: int, entry: dict):
    user_hash = get_user_hash(user_id)
    entry["date"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO diary (user_hash, date, data) VALUES (?, ?, ?)",
            (user_hash, entry["date"], json.dumps(entry, ensure_ascii=False))
        )
        conn.commit()
    finally:
        conn.close()

def db_get_entries(user_id: int, limit: int = 5) -> list:
    user_hash = get_user_hash(user_id)
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT data FROM diary WHERE user_hash=? ORDER BY id DESC LIMIT ?",
            (user_hash, limit)
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(r[0]) for r in rows]

def db_get_entry_count(user_id: int) -> int:
    user_hash = get_user_hash(user_id)
    conn = sqlite3.connect(DB_PATH)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM diary WHERE user_hash=?", (user_hash,)
        ).fetchone()[0]
    finally:
        conn.close()
    return count

def db_subscribe(user_id: int, timezone: str = DEFAULT_TZ):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT INTO subscribers (user_id, subscribed_at, timezone, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET timezone = excluded.timezone, active = 1
        """, (user_id, datetime.now().strftime("%d.%m.%Y %H:%M"), timezone))
        conn.commit()
    finally:
        conn.close()

def db_unsubscribe(user_id: int):
    """Явный отказ (кнопка «Выключить»). Строка НЕ удаляется, а помечается неактивной —
    иначе тихая авто-подписка на следующем же сообщении сочла бы пользователя «новым» и
    подписала бы обратно."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE subscribers SET active=0 WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()

def db_delete_subscriber_row(user_id: int):
    """Право на удаление данных («Удалить мои данные») — в отличие от db_unsubscribe,
    здесь строка удаляется полностью, а не просто деактивируется."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM subscribers WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()

def db_has_ever_seen_subscriber(user_id: int) -> bool:
    """Есть ли вообще строка в subscribers — используется авто-подпиской, чтобы не
    трогать тех, кто уже когда-то явно отписался."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT 1 FROM subscribers WHERE user_id=?", (user_id,)).fetchone()
        return row is not None
    finally:
        conn.close()

def db_is_subscribed(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT 1 FROM subscribers WHERE user_id=? AND active=1", (user_id,)).fetchone()
        return row is not None
    finally:
        conn.close()

def db_get_all_subscribers() -> list:
    """Возвращает список (user_id, timezone) активных подписчиков — используется планировщиком."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT user_id, timezone FROM subscribers WHERE active=1").fetchall()
    finally:
        conn.close()
    return rows

# =====================
# IN-MEMORY (chat history only)
# =====================

MAX_TRACKED_SESSIONS = 500

user_sessions = OrderedDict()

def get_user_data(user_id):
    if user_id in user_sessions:
        user_sessions.move_to_end(user_id)
    else:
        user_sessions[user_id] = {"history": []}
        if len(user_sessions) > MAX_TRACKED_SESSIONS:
            user_sessions.popitem(last=False)
    return user_sessions[user_id]

async def get_ai_response(user_id, user_message, mode="chat", context_data=None):
    data = get_user_data(user_id)

    system_prompts = {
        "chat": """<роль>
Ты — GoNeuralShift, КПТ/ACT-ассистент. Говоришь как живой, тёплый человек, которому реально интересно
что происходит с собеседником — не как терапевт из учебника и не как чат-бот поддержки.
</роль>

<принципы>
— Сначала контакт, потом всё остальное. Не торопись к техникам и переосмыслению — убедись что человек
почувствовал что его услышали.
— Ты участвуешь, а не просто отражаешь. У тебя есть своя реакция на то что говорит человек. После отклика —
один конкретный вопрос. Не общий («как ты себя чувствуешь?»), а точный («это случилось один раз или так
бывает часто?»).
— Замечай то что не сказано. Если человек говорит «всё нормально» но описывает явно тяжёлую ситуацию —
это важно. Мягко обрати внимание.
— Валидируй чувства — но не сливайся с деструктивными мыслями. Можно признать что человеку больно и
одновременно мягко поставить под сомнение его интерпретацию (опора: Padesky, Socratic Questioning, 1993 —
вопрос не «правда ли мысль», а помогает ли она).
— Разделяй человека и его мысли. Не «ты неудачник» — а «ты думаешь что ты неудачник».
— Никогда не говори человеку что ему делать. Только вопросы и наблюдения — пусть сам приходит к выводам.
— Если видишь когнитивное искажение (по классификации Burns, "Feeling Good") — назови его мягко, как
наблюдение, не нотацию.
— Если человек написал мало — не засыпай вопросами. Один вопрос. Дай пространство.
</принципы>

<безопасность_уровни>
Уровень 1 (бытовой стресс, тревога, конфликт, грусть) — обычный тёплый диалог, вопросы по принципам выше.

Уровень 2 (устойчиво низкое настроение несколько сообщений подряд, безнадёжность, фразы вроде «смысла нет»,
«я больше не могу», но БЕЗ прямого упоминания вреда себе) — мягко, но прямо спроси:
«Бывают ли у тебя мысли о том, чтобы причинить себе вред?» Это обязательный прямой вопрос, не обходи его
метафорами и не бойся его задать — прямой вопрос снижает риск, а не провоцирует его (опора:
Columbia Suicide Severity Rating Scale, принцип прямого скрининга).

Уровень 3 (прямое упоминание самоповреждения, суицидальных мыслей, плана) —
останови обычную технику. Скажи прямо и без драматизации, что это серьёзно, и что ты не замена
специалисту в такой ситуации. Дай кнопку ✴ Кризисная помощь. Не продолжай терапевтическую работу с
мыслью, пока не проверено что человек в безопасности прямо сейчас.
Не предлагай кризисную помощь повторно на каждом сообщении подряд, особенно если человек уже отказался
и явно нуждается именно в разговоре — но не убирай эту опцию из виду.
</безопасность_уровни>

<формат>
Максимум 2-3 предложения. Живой язык, никакого канцелярита.
Никогда не используй слова: «безусловно», «конечно», «это важно», «я понимаю».
</формат>

<пример>
Плохой ответ: «Я понимаю, это действительно тяжело. Важно помнить, что все через это проходят.»
Хороший ответ: «Ого. То, как ты это описываешь — звучит будто ты держишь это в одиночку уже давно.
Так и есть?»
</пример>

Ты не замена психотерапевту.
ВАЖНО: отвечай ТОЛЬКО на русском языке. Никаких иероглифов, латиницы или других символов.""",

        "diary_reframe": f"""<роль>Ты — КПТ-терапевт, работающий по структуре дневника мыслей Judith S. Beck
("Cognitive Behavior Therapy: Basics and Beyond").</роль>

<контекст>
Ситуация: {context_data.get('situation', '') if context_data else ''}
Эмоции: {context_data.get('emotion', '') if context_data else ''}
Автоматическая мысль: {context_data.get('thought', '') if context_data else ''}
Когнитивное искажение: {context_data.get('distortion', '') if context_data else ''}
</контекст>

<задача>
Помоги пользователю сформулировать более сбалансированную альтернативную мысль.
Задай один точный сократовский вопрос, который поможет увидеть ситуацию шире — например, про
доказательства за/против мысли, или про то, что бы он сказал другу в такой же ситуации.
Не давай альтернативную мысль сам — веди пользователя к тому, чтобы он сформулировал её сам.
</задача>

<формат>Максимум 2-3 предложения. Тепло, по-русски, без канцелярита.</формат>""",

        "reframe_check": """<роль>Ты — КПТ-терапевт.</роль>
<задача>
Пользователь только что сформулировал альтернативную, более сбалансированную мысль.
Спроси его: «Оцени ещё раз силу первоначальной эмоции (0-100%) — изменилась ли она сейчас?»
Это не формальность: сравнение цифр «до» и «после» — конкретное доказательство того, что работа с
мыслью меняет состояние, а не просто разговор ради разговора.
</задача>
<формат>1-2 предложения, тепло, по-русски.</формат>""",

        "socratic": f"""<роль>Ты — КПТ-терапевт, ведущий сократовский диалог по методу Christine Padesky
("Socratic Questioning: Changing Minds or Guiding Discovery?", 1993).</роль>

<контекст>
Мысль пользователя: {context_data.get('thought', '') if context_data else ''}
История диалога уже есть в сообщениях.
</контекст>

<последовательность_вопросов>
Веди диалог через эти четыре типа вопросов, по одному вопросу за раз, в этом порядке:
1. Уточняющий — что конкретно произошло, что именно было сказано или сделано.
2. Про доказательства — какие факты подтверждают эту мысль, какие ей противоречат.
3. Про альтернативы — как ещё можно объяснить эту ситуацию.
4. Про полезность — даже если мысль частично правда, помогает ли она пользователю, или мешает.
</последовательность_вопросов>

<правила>
Не давай ответов и не подсказывай выводы — только вопросы. Цель: пользователь сам обнаруживает слабые
места в мысли. Через 4-5 обменов мягко подведи итог, но и итог сформулируй как вопрос, а не утверждение.
</правила>

<формат>Один вопрос за раз. Максимум 2-3 предложения. Тепло, по-русски.</формат>""",
    }

    system = system_prompts.get(mode, system_prompts["chat"])
    messages = [{"role": "system", "content": system}]

    if mode == "chat":
        for msg in data["history"][-10:]:
            messages.append(msg)

    messages.append({"role": "user", "content": user_message})

    response = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=500,
        # Temperature понижена с 0.7 до 0.45 — для терапевтического контекста модель должна быть
        # стабильнее в следовании правилам (не соскальзывать в советы вместо вопросов)
        temperature=0.45,
    )

    reply = response.choices[0].message.content

    if mode == "chat":
        data["history"].append({"role": "user", "content": user_message})
        data["history"].append({"role": "assistant", "content": reply})
        if len(data["history"]) > 20:
            data["history"] = data["history"][-20:]

    return reply

# =====================
# KEYBOARDS
# =====================

def main_keyboard():
    keyboard = [
        [KeyboardButton("｡ﾟ Поговорить с ботом"), KeyboardButton("✦ Дневник мыслей")],
        [KeyboardButton("࿔ Сократовский диалог"), KeyboardButton("･ﾟ Дефузия")],
        [KeyboardButton("⊹ Мой прогресс"), KeyboardButton("ﾟ｡ Кризисная помощь")],
        [KeyboardButton("⭐ Поддержать проект")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def distortions_keyboard(show_info=False):
    buttons = []
    if not show_info:
        buttons.append([InlineKeyboardButton("· · · Что такое искажения? · · ·", callback_data="dist_info")])
    for name in COGNITIVE_DISTORTIONS.keys():
        buttons.append([InlineKeyboardButton(name, callback_data=f"dist_{name[:30]}")])
    buttons.append([InlineKeyboardButton("❓ Не знаю / Пропустить", callback_data="dist_skip")])
    return InlineKeyboardMarkup(buttons)

def defusion_keyboard():
    buttons = []
    for name in DEFUSION_TECHNIQUES.keys():
        buttons.append([InlineKeyboardButton(name, callback_data=f"def_{name[:30]}")])
    return InlineKeyboardMarkup(buttons)

def back_keyboard():
    keyboard = [[KeyboardButton("ﾟ✦ Главное меню")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# =====================
# HANDLERS
# =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_increment_sessions(user.id)

    welcome = (
        f"Привет, {user.first_name} ✧\n\n"
        "Это пространство для работы с мыслями, что давят, "
        "крутятся по кругу или не дают покоя.\n\n"
        "Я не терапевт и не замена живому человеку. Но я умею помогать "
        "разбираться в мыслях, которые мешают — с помощью научно обоснованных техник.\n\n"
        "Пишите что угодно. Я здесь ｡ﾟ"
    )
    await update.message.reply_text(welcome, reply_markup=main_keyboard())
    return MAIN_MENU

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data.clear()

    if text == "｡ﾟ Поговорить с ботом":
        await update.message.reply_text(
            "Расскажите что происходит. Я здесь и слушаю ✧\n\n"
            "_(Нажмите «ﾟ✦ Главное меню» чтобы вернуться)_",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )
        return AI_CHAT

    elif text == "✦ Дневник мыслей":
        await update.message.reply_text(
            "📓 *Дневник мыслей*\n\n"
            "Запишем ситуацию, эмоции и мысль — и найдём более сбалансированный взгляд.\n\n"
            "Опишите ситуацию, которая вас беспокоит:",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )
        return THOUGHT_DIARY_SITUATION

    elif text == "࿔ Сократовский диалог":
        await update.message.reply_text(
            "🧠 *Сократовский диалог*\n\n"
            "Напишите мысль, которую хотите исследовать.\n"
            "_(Например: «Я никогда не справлюсь», «Все меня осуждают»)_",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )
        context.user_data['mode'] = 'socratic'
        return AI_CHAT

    elif text == "･ﾟ Дефузия":
        await update.message.reply_text(
            "🌊 *Когнитивная дефузия*\n\n"
            "Напишите мысль, которая вас беспокоит:",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )
        return DEFUSION_THOUGHT

    elif text == "⊹ Мой прогресс":
        user_id = update.effective_user.id
        diary_count = db_get_entry_count(user_id)
        sessions = db_get_sessions(user_id)

        text_progress = (
            f"📊 *Ваш прогресс*\n\n"
            f"• Сессий: {sessions}\n"
            f"• Записей в дневнике: {diary_count}\n\n"
        )
        if diary_count >= 5:
            text_progress += "｡ﾟ Уже столько записей. Ты молодец!"
        elif diary_count >= 1:
            text_progress += "✦ Начало есть. Иногда это самое сложное."
        else:
            text_progress += "Пока записей нет. Дневник мыслей — хорошее место чтобы начать ･ﾟ"

        buttons = []
        if diary_count > 0:
            buttons.append([InlineKeyboardButton("📋 Мои записи", callback_data="show_diary")])
        subscribed = db_is_subscribed(user_id)
        sub_label = "🔕 Выключить ежедневные сообщения" if subscribed else "🔔 Включить ежедневные сообщения"
        buttons.append([InlineKeyboardButton(sub_label, callback_data="toggle_subscription")])
        buttons.append([InlineKeyboardButton("ℹ️ Как хранятся мои данные", callback_data="privacy_info")])
        buttons.append([InlineKeyboardButton("🗑 Удалить мои данные", callback_data="delete_data_ask")])

        await update.message.reply_text(
            text_progress,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return MAIN_MENU

    elif text == "ﾟ｡ Кризисная помощь":
        crisis = (
            "🆘 *Кризисная помощь*\n\n"
            "Если вы в критическом моменте прямо сейчас:\n\n"
            "🇷🇺 *Россия:* 8-800-2000-122 (бесплатно)\n"
            "🌍 *Международная помощь:* findahelpline.com\n\n"
            "Если есть мысли о самоповреждении — это медицинская ситуация. "
            "Пожалуйста, позвоните на горячую линию.\n\n"
            "Я здесь, если хотите поговорить 🤍"
        )
        await update.message.reply_text(crisis, parse_mode='Markdown', reply_markup=main_keyboard())
        return MAIN_MENU

    elif text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    elif text == "⭐ Поддержать проект":
        return await donate(update, context)

    else:
        await update.message.reply_text(
            "Выберите из меню или нажмите «｡ﾟ Поговорить с ботом» чтобы просто написать что беспокоит.",
            reply_markup=main_keyboard()
        )
        return MAIN_MENU

async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    mode = context.user_data.get('mode', 'chat')
    socratic_thought = context.user_data.get('socratic_thought', '')

    if mode == 'socratic' and not socratic_thought:
        context.user_data['socratic_thought'] = text
        context.user_data['socratic_history'] = []

    await update.message.chat.send_action("typing")

    try:
        if mode == 'socratic':
            context_data = {'thought': context.user_data.get('socratic_thought', text)}
            reply = await get_ai_response(user_id, text, mode='socratic', context_data=context_data)
        else:
            reply = await get_ai_response(user_id, text, mode='chat', context_data={})

        await update.message.reply_text(reply, reply_markup=back_keyboard())
    except Exception as e:
        logger.error(f"Groq error: {e}")
        await update.message.reply_text(
            "Что-то пошло не так. Попробуйте ещё раз или вернитесь в меню.",
            reply_markup=back_keyboard()
        )

    return AI_CHAT

# === THOUGHT DIARY ===

async def thought_diary_emotion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU
    context.user_data['td_situation'] = update.message.text
    await update.message.reply_text(
        "Какие эмоции вы испытывали? И насколько сильно (0-100%)?\n\n"
        "_(Например: тревога 70%, стыд 40%)_",
        parse_mode='Markdown',
        reply_markup=back_keyboard()
    )
    return THOUGHT_DIARY_EMOTION

async def thought_diary_thought(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU
    context.user_data['td_emotion'] = update.message.text
    await update.message.reply_text(
        "Что промелькнуло в голове в тот момент?\n\n"
        "Попробуйте поймать *автоматическую мысль* — первую реакцию до анализа.\n\n"
        "_(Например: «Я опять всё испортил», «Они точно думают плохо обо мне»)_",
        parse_mode='Markdown',
        reply_markup=back_keyboard()
    )
    return THOUGHT_DIARY_THOUGHT

async def thought_diary_distortion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU
    context.user_data['td_thought'] = update.message.text
    await update.message.reply_text(
        "Когнитивные искажения — это автоматические ошибки мышления которые искажают реальность.\n\n"
        "Узнаёте что-то похожее в своей мысли?",
        reply_markup=distortions_keyboard()
    )
    return THOUGHT_DIARY_DISTORTION

async def handle_distortion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "dist_info":
        info_text = (
            "💡 *Когнитивные искажения* — это автоматические ошибки мышления:\n\n"
            "🔮 *Чтение мыслей* — думаю что знаю мысли других\n"
            "🌑 *Катастрофизация* — жду худшего как неизбежного\n"
            "🏷 *Навешивание ярлыков* — вешаю глобальный ярлык\n"
            "🔬 *Сверхобобщение* — один случай = правило навсегда\n"
            "👁 *Фильтрация* — вижу только плохое\n"
            "⚫ *Чёрно-белое мышление* — всё или ничего\n"
            "⚡ *Долженствование* — жёсткие правила «должен»\n"
            "🔗 *Персонализация* — беру на себя чужую ответственность\n"
            "📉 *Обесценивание* — хорошее не считается\n"
            "💭 *Эмоциональное мышление* — раз чувствую — значит правда"
        )
        await query.edit_message_text(
            info_text + "\n\nТеперь выберите что похоже на вашу мысль:",
            parse_mode='Markdown',
            reply_markup=distortions_keyboard(show_info=True)
        )
        return THOUGHT_DIARY_DISTORTION

    chosen_key = query.data[5:]

    if chosen_key == "skip":
        context.user_data['td_distortion'] = "Не определено"
        distortion_info = ""
    else:
        full_key = next((k for k in COGNITIVE_DISTORTIONS if k[:30] == chosen_key), chosen_key)
        context.user_data['td_distortion'] = full_key
        distortion_info = f"\n\n💡 _{COGNITIVE_DISTORTIONS.get(full_key, '')}_"

    situation = context.user_data.get('td_situation', '')
    emotion = context.user_data.get('td_emotion', '')
    thought = context.user_data.get('td_thought', '')
    distortion = context.user_data.get('td_distortion', '')

    await query.edit_message_text(
        f"Искажение: *{distortion}*{distortion_info}\n\n"
        "Сейчас я помогу найти более сбалансированную мысль...",
        parse_mode='Markdown'
    )

    try:
        context_data = {
            'situation': situation,
            'emotion': emotion,
            'thought': thought,
            'distortion': distortion,
        }
        ai_question = await get_ai_response(
            query.from_user.id,
            f"Помоги мне переосмыслить мысль: {thought}",
            mode='diary_reframe',
            context_data=context_data
        )
        await query.message.reply_text(ai_question, reply_markup=back_keyboard())
    except Exception as e:
        logger.error(f"Groq error: {e}")
        await query.message.reply_text(
            "Напишите альтернативную, более сбалансированную мысль:",
            reply_markup=back_keyboard()
        )

    return THOUGHT_DIARY_REFRAME

async def thought_diary_reframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь прислал альтернативную мысль. Вместо немедленного сохранения —
    задаём шаг переоценки эмоции (reframe_check), опора: Judith S. Beck."""
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    user_id = update.effective_user.id
    context.user_data['td_reframe'] = update.message.text

    try:
        recheck_question = await get_ai_response(
            user_id,
            "Спроси про переоценку эмоции",
            mode='reframe_check',
            context_data={}
        )
        await update.message.reply_text(recheck_question, reply_markup=back_keyboard())
    except Exception as e:
        logger.error(f"Groq error: {e}")
        await update.message.reply_text(
            "Оцени ещё раз силу первоначальной эмоции (0-100%) — изменилась ли она сейчас?",
            reply_markup=back_keyboard()
        )

    return THOUGHT_DIARY_EMOTION_RECHECK

async def thought_diary_emotion_recheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финальный шаг: сохраняем запись вместе с переоценённой силой эмоции."""
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    user_id = update.effective_user.id
    context.user_data['td_emotion_after'] = update.message.text

    entry = {
        "situation": context.user_data.get('td_situation', ''),
        "emotion": context.user_data.get('td_emotion', ''),
        "thought": context.user_data.get('td_thought', ''),
        "distortion": context.user_data.get('td_distortion', ''),
        "reframe": context.user_data.get('td_reframe', ''),
        "emotion_after": context.user_data.get('td_emotion_after', ''),
    }
    db_save_entry(user_id, entry)

    summary = (
        "✓︎ *Запись сохранена*\n\n"
        f"♒︎ Исходная мысль: _{entry['thought'][:100]}_\n"
        f"ヅ︎ Альтернатива: _{entry['reframe'][:150]}_\n"
        f"↺ Эмоция до/после: _{entry['emotion']}_ → _{entry['emotion_after']}_\n\n"
        "Отличная работа. Каждая такая запись постепенно меняет нейронные паттерны."
    )
    await update.message.reply_text(summary, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

# === DEFUSION ===

async def defusion_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU
    context.user_data['defusion_thought'] = update.message.text
    await update.message.reply_text(
        f"Работаем с мыслью: _\"{update.message.text}\"_\n\nВыберите технику:",
        parse_mode='Markdown',
        reply_markup=defusion_keyboard()
    )
    return DEFUSION_TECHNIQUE

async def handle_defusion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chosen_key = query.data[4:]
    thought = context.user_data.get('defusion_thought', 'ваша мысль')

    full_key = next((k for k in DEFUSION_TECHNIQUES if k[:30] == chosen_key), chosen_key)
    technique_text = DEFUSION_TECHNIQUES.get(full_key, "")

    await query.edit_message_text(
        f"*{full_key}*\n\n"
        f"Ваша мысль: _\"{thought}\"_\n\n"
        f"{technique_text}\n\n"
        "Побудьте с этим 1-2 минуты. Как вы себя чувствуете после?",
        parse_mode='Markdown'
    )
    await query.message.reply_text("Главное меню:", reply_markup=main_keyboard())
    return MAIN_MENU

# === ЕЖЕДНЕВНЫЕ СООБЩЕНИЯ: ПЛАНИРОВЩИК ===

def _random_time_in_window(window: tuple) -> tuple:
    """Возвращает случайные (час, минута) внутри окна (h1, m1, h2, m2)."""
    h1, m1, h2, m2 = window
    start_minutes = h1 * 60 + m1
    end_minutes = h2 * 60 + m2
    chosen = random.randint(start_minutes, end_minutes)
    return chosen // 60, chosen % 60

def _schedule_user_today(job_queue, user_id: int, timezone: str = DEFAULT_TZ):
    """Планирует утреннее и вечернее сообщение для одного пользователя на сегодня —
    если окно уже прошло, просто пропускает его (сообщение придёт завтра по общему расписанию)."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)

    for window, label in ((MORNING_WINDOW, "morning"), (EVENING_WINDOW, "evening")):
        h, m = _random_time_in_window(window)
        run_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if run_at <= now:
            continue
        job_queue.run_once(
            send_prompt_job,
            when=run_at,
            data={"user_id": user_id},
            name=f"prompt_{user_id}_{label}_{run_at.date()}"
        )

async def send_prompt_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    try:
        await context.bot.send_message(chat_id=user_id, text=get_random_prompt())
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение подписчику {user_id}: {e}")

async def schedule_all_subscribers_daily(context: ContextTypes.DEFAULT_TYPE):
    """Запускается раз в сутки (00:05) — планирует новое случайное утреннее и вечернее
    сообщение на сегодня для каждого подписчика."""
    for user_id, timezone in db_get_all_subscribers():
        _schedule_user_today(context.job_queue, user_id, timezone or DEFAULT_TZ)

async def toggle_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if db_is_subscribed(user_id):
        db_unsubscribe(user_id)
        await query.message.reply_text(
            "🔕 Ежедневные сообщения выключены. Включить их снова можно в любой момент "
            "в разделе «⊹ Мой прогресс».",
            reply_markup=main_keyboard()
        )
    else:
        db_subscribe(user_id)
        _schedule_user_today(context.job_queue, user_id)
        await query.message.reply_text(
            "🔔 Готово — буду присылать пару сообщений в день, утром и вечером, "
            "время каждый раз немного случайное.\n\n"
            "Небольшая деталь про приватность: чтобы присылать сообщения именно вам, боту "
            "нужно знать ваш Telegram ID — в отличие от дневника мыслей, эта часть не анонимна. "
            "Выключить можно в любой момент здесь же, кнопкой.",
            reply_markup=main_keyboard()
        )
    return MAIN_MENU

async def auto_subscribe_on_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Молча подписывает пользователя на ежедневные сообщения при первом же обращении
    к боту — без отдельного согласия. Выключить всё ещё можно кнопкой в «Мой прогресс».

    Проверяем именно «видели ли этого пользователя раньше», а не «подписан ли он сейчас» —
    иначе явный отказ через кнопку тут же затирался бы следующим же сообщением боту."""
    user = update.effective_user
    if user is None:
        return
    if not db_has_ever_seen_subscriber(user.id):
        db_subscribe(user.id)
        _schedule_user_today(context.job_queue, user.id)

# === DONATE ===

async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Спасибо что вы здесь ✦\n\n"
        "Поддержать проект можно двумя способами:\n\n"
        "💳 *СБП по номеру телефона:*\n"
        "`+79910234966`\n"
        "_(сбербанк или яндекс банк, без комиссии 🤍)_",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Telegram Stars", callback_data="donate_stars")],
        ])
    )
    return MAIN_MENU

async def donate_stars_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_invoice(
        title="Поддержать GoNeuralShift",
        description="Спасибо что вы здесь. Ваша поддержка помогает развивать проект ✦",
        payload="donate",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice("Донат", 1)],
    )

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Спасибо ⭐ Это очень важно и приятно.\nВы помогаете проекту жить дальше ✦",
        reply_markup=main_keyboard()
    )
    return MAIN_MENU

# === DIARY VIEW ===

async def show_diary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    entries = db_get_entries(user_id, limit=5)

    if not entries:
        await query.message.reply_text("Записей пока нет.", reply_markup=main_keyboard())
        return MAIN_MENU

    text = "📋 *Ваши последние записи:*\n\n"
    for i, entry in enumerate(entries, 1):
        text += (
            f"*{i}. {entry.get('date', '')}*\n"
            f"Ситуация: _{entry.get('situation', '')[:80]}_\n"
            f"Мысль: _{entry.get('thought', '')[:80]}_\n"
            f"Альтернатива: _{entry.get('reframe', '')[:80]}_\n"
        )
        if entry.get('emotion_after'):
            text += f"Эмоция до/после: _{entry.get('emotion', '')}_ → _{entry.get('emotion_after', '')}_\n"
        text += "\n"

    await query.message.reply_text(text, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

# === PRIVACY / DATA CONTROL ===

async def privacy_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    info = (
        "ℹ️ *Как хранятся ваши данные*\n\n"
        "• Записи дневника мыслей и статистика сессий привязаны не к вашему Telegram ID "
        "напрямую, а к необратимому хэшу — связать их с конкретным человеком нельзя, "
        "даже владельцу бота.\n\n"
        "• Текст записей дневника мыслей хранится в базе — это нужно, чтобы вы могли "
        "посмотреть свою историю в разделе «Мои записи».\n\n"
        "• Чтобы присылать вам утренние и вечерние сообщения с вопросами для размышления, "
        "бот хранит ваш настоящий Telegram ID (не хэш) — это единственное исключение, "
        "потому что для отправки сообщений Telegram требует настоящий ID. Отключить это "
        "можно в любой момент кнопкой «Выключить ежедневные сообщения» в «Мой прогресс».\n\n"
        "• История переписки с ИИ хранится только в оперативной памяти сервера и "
        "полностью исчезает при перезапуске бота — на диск она не сохраняется.\n\n"
        "• Ваши сообщения на несколько секунд передаются во внешний ИИ-сервис (Groq) "
        "для генерации ответа — без этого бот не может отвечать.\n\n"
        "• Вы можете удалить все свои данные из базы в любой момент — кнопкой ниже."
    )
    await query.message.reply_text(info, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

async def delete_data_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "⚠️ Точно удалить все свои данные?\n\n"
        "Будут безвозвратно удалены все записи дневника мыслей и статистика сессий. "
        "Отменить это действие нельзя.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, удалить", callback_data="delete_data_execute"),
            InlineKeyboardButton("Отмена", callback_data="delete_data_cancel"),
        ]])
    )
    return MAIN_MENU

async def delete_data_execute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db_delete_user_data(query.from_user.id)
    await query.edit_message_text("🗑 Все ваши данные удалены.")
    await query.message.reply_text("Главное меню:", reply_markup=main_keyboard())
    return MAIN_MENU

async def delete_data_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Отменено. Ваши данные остались нетронуты.")
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
    return MAIN_MENU

# === VOICE ===

def _transcribe_file(tmp_path: str) -> str:
    with open(tmp_path, 'rb') as audio:
        transcription = groq_client.audio.transcriptions.create(
            file=("voice.ogg", audio, "audio/ogg"),
            model="whisper-large-v3-turbo",
            language="ru",
        )
    return transcription.text

async def transcribe_voice(voice_file) -> str:
    with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tmp:
        tmp_path = tmp.name
        await voice_file.download_to_drive(tmp_path)
    try:
        return await asyncio.to_thread(_transcribe_file, tmp_path)
    finally:
        os.unlink(tmp_path)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        voice = await update.message.voice.get_file()
        text = await transcribe_voice(voice)

        if not text.strip():
            await update.message.reply_text(
                "Не удалось распознать голосовое. Попробуйте говорить чётче или напишите текстом.",
                reply_markup=back_keyboard()
            )
            return

        await update.message.reply_text(f"🎤 _{text}_", parse_mode='Markdown')

        user_id = update.effective_user.id
        mode = context.user_data.get('mode', 'chat')

        if mode == 'socratic' and not context.user_data.get('socratic_thought'):
            context.user_data['socratic_thought'] = text

        if mode == 'socratic':
            context_data = {'thought': context.user_data.get('socratic_thought', text)}
            reply = await get_ai_response(user_id, text, mode='socratic', context_data=context_data)
        else:
            reply = await get_ai_response(user_id, text, mode='chat', context_data={})

        await update.message.reply_text(reply, reply_markup=back_keyboard())

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(
            "Не удалось обработать голосовое. Попробуйте написать текстом.",
            reply_markup=back_keyboard()
        )

# =====================
# MAIN
# =====================

def main():
    init_db()

    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            MessageHandler(filters.VOICE, handle_voice),
        ],
        states={
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu),
                MessageHandler(filters.VOICE, handle_voice),
                MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment),
                CallbackQueryHandler(donate_stars_callback, pattern='^donate_stars$'),
                CallbackQueryHandler(show_diary_callback, pattern='^show_diary$'),
                CallbackQueryHandler(privacy_info_callback, pattern='^privacy_info$'),
                CallbackQueryHandler(delete_data_ask_callback, pattern='^delete_data_ask$'),
                CallbackQueryHandler(delete_data_execute_callback, pattern='^delete_data_execute$'),
                CallbackQueryHandler(delete_data_cancel_callback, pattern='^delete_data_cancel$'),
                CallbackQueryHandler(toggle_subscription_callback, pattern='^toggle_subscription$'),
            ],
            AI_CHAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ai_chat),
                MessageHandler(filters.VOICE, handle_voice),
            ],
            THOUGHT_DIARY_SITUATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_emotion),
                MessageHandler(filters.VOICE, handle_voice),
            ],
            THOUGHT_DIARY_EMOTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_thought),
                MessageHandler(filters.VOICE, handle_voice),
            ],
            THOUGHT_DIARY_THOUGHT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_distortion),
                MessageHandler(filters.VOICE, handle_voice),
            ],
            THOUGHT_DIARY_DISTORTION: [
                CallbackQueryHandler(handle_distortion_callback, pattern='^dist_'),
            ],
            THOUGHT_DIARY_REFRAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_reframe),
                MessageHandler(filters.VOICE, handle_voice),
            ],
            THOUGHT_DIARY_EMOTION_RECHECK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_emotion_recheck),
                MessageHandler(filters.VOICE, handle_voice),
            ],
            DEFUSION_THOUGHT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, defusion_choose),
                MessageHandler(filters.VOICE, handle_voice),
            ],
            DEFUSION_TECHNIQUE: [
                CallbackQueryHandler(handle_defusion_callback, pattern='^def_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu),
                MessageHandler(filters.VOICE, handle_voice),
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('start', start),
        ],
    )

    # Молчаливая авто-подписка на ежедневные сообщения — срабатывает раньше остальных
    # обработчиков (group=-1) на любое сообщение или нажатие кнопки.
    application.add_handler(MessageHandler(filters.ALL, auto_subscribe_on_interaction), group=-1)
    application.add_handler(CallbackQueryHandler(auto_subscribe_on_interaction), group=-1)

    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    application.add_handler(conv_handler)

    # Планировщик ежедневных сообщений:
    # 1) каждый день в 00:05 по МСК заново раскидывает случайное утреннее/вечернее время
    #    для всех текущих подписчиков;
    # 2) сразу при старте бота досчитывает окна на сегодня — иначе после рестарта бота
    #    в середине дня подписчики не получили бы сегодняшние сообщения.
    application.job_queue.run_daily(
        schedule_all_subscribers_daily,
        time=dt_time(hour=0, minute=5, tzinfo=ZoneInfo(DEFAULT_TZ))
    )
    for sub_user_id, sub_timezone in db_get_all_subscribers():
        _schedule_user_today(application.job_queue, sub_user_id, sub_timezone or DEFAULT_TZ)

    print("🤖 GoNeuralShift запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
