import os
import re
import asyncio
import logging
import tempfile
import sqlite3
import hashlib
import json
import random
from collections import OrderedDict
from telegram.ext import PreCheckoutQueryHandler
from datetime import datetime
from telegram import LabeledPrice
from groq import Groq
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes, ApplicationHandlerStop,
    PicklePersistence
)
from messages_rotation import setup_user_schedule, set_sent_tracker, TZ as SCHEDULE_TZ

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Приглушаем шумные сторонние логгеры. Важно: httpx на уровне INFO пишет полный URL
# запроса к Telegram, а в нём — токен бота; поднимаем порог до WARNING, чтобы токен
# не попадал в логи. Оставляем только предупреждения и ошибки.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
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

# Telegram ID администратора (для команды /stats). Задаётся переменной окружения ADMIN_ID.
ADMIN_ID = os.getenv("ADMIN_ID")

# States
# Добавлено новое состояние THOUGHT_DIARY_EMOTION_RECHECK — шаг переоценки эмоции
# после переформулировки мысли (опора: Judith S. Beck, "Cognitive Behavior Therapy: Basics and Beyond")
(MAIN_MENU, AI_CHAT,
 THOUGHT_DIARY_SITUATION, THOUGHT_DIARY_EMOTION, THOUGHT_DIARY_THOUGHT,
 THOUGHT_DIARY_DISTORTION, THOUGHT_DIARY_REFRAME, THOUGHT_DIARY_EMOTION_RECHECK,
 DEFUSION_THOUGHT, DEFUSION_TECHNIQUE, DEFUSION_REFLECT,
 THOUGHT_DIARY_MEANING) = range(12)

COGNITIVE_DISTORTIONS = {
    "🔮 Чтение мыслей": "Убеждённость в том, что знаешь мысли других без оснований.",
    "🌑 Катастрофизация": "Ожидание худшего исхода как неизбежного.",
    "🏷 Навешивание ярлыков": "Глобальный ярлык вместо описания конкретного поведения.",
    "🔬 Сверхобобщение": "Широкий вывод на основе единичного случая.",
    "👁 Фильтрация": "Фокус только на негативных деталях, игнорирование позитивных.",
    "⚫ Чёрно-белое мышление": "Видение всего в крайностях, без полутонов.",
    "⚡ Долженствование": "Жёсткие правила: 'должен', 'обязан', 'необходимо'.",
    "🔗 Персонализация": "Ответственность за вещи вне твоей власти.",
    "📉 Обесценивание": "Отвержение позитивного опыта как незначительного.",
    "💭 Эмоциональное мышление": "Убеждённость в чём-то только потому, что так чувствуешь.",
}

# Те же искажения, названные словами самого человека, а не термином. Термин требует
# теоретической подготовки, которой у пользователя нет; такую формулировку можно узнать
# в себе, ничего не изучив. Используются на кнопках ручного выбора и как запасное
# объяснение, если модель вернула только название без пояснения.
# Формулировки намеренно без родовых окончаний.
DISTORTION_PLAIN = {
    "🔮 Чтение мыслей": "Я решаю, что думают обо мне другие",
    "🌑 Катастрофизация": "Я жду худшего исхода",
    "🏷 Навешивание ярлыков": "Я вешаю на себя ярлык",
    "🔬 Сверхобобщение": "Один случай — значит, так всегда",
    "👁 Фильтрация": "Я вижу только плохое",
    "⚫ Чёрно-белое мышление": "Или идеально, или провал",
    "⚡ Долженствование": "Слишком много «должен» и «обязан»",
    "🔗 Персонализация": "Я виню себя в том, что не в моей власти",
    "📉 Обесценивание": "Хорошее не считается",
    "💭 Эмоциональное мышление": "Раз я так чувствую — значит, так и есть",
}

# Техники дефузии основаны на ACT-протоколах:
# Hayes, S. C. — "Acceptance and Commitment Therapy: An Experiential Approach to Behavior Change"
# Harris, R. — "The Happiness Trap"

# Таймзона по умолчанию для подписчиков (используется как значение колонки timezone в БД).
# Само расписание утро/вечер и тексты сообщений вынесены в messages_rotation.py.
DEFAULT_TZ = "Europe/Moscow"

DEFUSION_TECHNIQUES = {
    "｡ﾟ Листья на воде": (
        "Устройся поудобнее. Можно закрыть глаза, если хочется.\n\n"
        "Представь тихий ручей — не обязательно красивый, просто спокойный. "
        "По воде медленно плывут листья.\n\n"
        "Возьми эту мысль — ту самую — и просто положи её на один из листьев. "
        "Не пытайся её исправить или прогнать. Просто положи и смотри, как уплывает.\n\n"
        "Ты на берегу. Мысль — на воде. Между вами есть расстояние.\n\n"
        "Побудь здесь минуту. Это и есть практика — не избавиться от мысли, а не быть внутри неё."
    ),
    "✦ Наблюдатель": (
        "Попробуй вот что — это немного странно, но работает.\n\n"
        "Представь, что где-то внутри тебя есть очень тихая, очень спокойная часть. "
        "Она всегда была там. Она видела всё, что с тобой происходило — и просто наблюдала.\n\n"
        "Эта часть сейчас тоже видит эту мысль. Без паники, без оценок.\n\n"
        "Попробуй смотреть на мысль её глазами — как на облако, которое проплывает мимо.\n\n"
        "Не обязательно в неё верить. Она просто есть — и это нормально."
    ),
    "･ﾟ Персонаж": (
        "Маленький, но очень ощутимый сдвиг.\n\n"
        "Попробуй добавить в начало фразу:\n"
        "*«У меня есть мысль о том, что...»*\n\n"
        "Например:\n"
        "Было → «Я со всем этим не справлюсь»\n"
        "Стало → «У меня есть мысль о том, что я со всем этим не справлюсь»\n\n"
        "Чувствуешь разницу? Ты как будто делаешь шаг назад и смотришь на мысль, "
        "а не смотришь на мир через неё.\n\n"
        "Мысль — это не факт. Это просто мысль. И ты — это не она."
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
    # last_morning / last_evening — дата (ГГГГ-ММ-ДД по МСК) последней отправки слота.
    # Нужны, чтобы редеплой/рестарт среди дня не отправил сообщение повторно.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            subscribed_at TEXT,
            timezone TEXT DEFAULT 'Europe/Moscow',
            active INTEGER DEFAULT 1,
            last_morning TEXT,
            last_evening TEXT
        )
    """)
    # Миграции для баз, созданных до появления новых колонок.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(subscribers)")]
    if "active" not in cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN active INTEGER DEFAULT 1")
    if "last_morning" not in cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN last_morning TEXT")
    if "last_evening" not in cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN last_evening TEXT")
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

def _today_str() -> str:
    """Сегодняшняя дата по таймзоне рассылки (МСК), формат ГГГГ-ММ-ДД."""
    return datetime.now(SCHEDULE_TZ).date().isoformat()

def db_already_sent_today(user_id: int, slot: str) -> bool:
    """Отправляли ли пользователю слот (morning/evening) сегодня — защита от дублей при рестарте."""
    column = "last_morning" if slot == "morning" else "last_evening"
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            f"SELECT {column} FROM subscribers WHERE user_id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    return bool(row and row[0] == _today_str())

def db_mark_sent_today(user_id: int, slot: str):
    """Отмечает, что слот отправлен сегодня."""
    column = "last_morning" if slot == "morning" else "last_evening"
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            f"UPDATE subscribers SET {column}=? WHERE user_id=?", (_today_str(), user_id)
        )
        conn.commit()
    finally:
        conn.close()

def db_stats() -> str:
    """Агрегированная статистика для админской команды /stats (без персональных данных)."""
    conn = sqlite3.connect(DB_PATH)
    def one(sql):
        try:
            return conn.execute(sql).fetchone()[0]
        except Exception:
            return 0
    total = one("SELECT COUNT(*) FROM subscribers")        # все, кто хоть раз писал боту
    started = one("SELECT COUNT(*) FROM users")             # делали /start
    diary_users = one("SELECT COUNT(DISTINCT user_hash) FROM diary")
    entries = one("SELECT COUNT(*) FROM diary")
    active_subs = one("SELECT COUNT(*) FROM subscribers WHERE active=1")
    try:
        rows = conn.execute("SELECT user_hash, COUNT(*) FROM diary GROUP BY user_hash").fetchall()
    except Exception:
        rows = []
    b1 = sum(1 for _h, n in rows if n == 1)
    b24 = sum(1 for _h, n in rows if 2 <= n <= 4)
    b5 = sum(1 for _h, n in rows if n >= 5)
    now = datetime.now()
    d7 = d30 = 0
    dates = []
    try:
        for (ds,) in conn.execute("SELECT date FROM diary"):
            try:
                dt = datetime.strptime(ds, "%d.%m.%Y %H:%M")
            except Exception:
                continue
            dates.append(dt)
            age = (now - dt).days
            if age <= 7:
                d7 += 1
            if age <= 30:
                d30 += 1
    except Exception:
        pass
    avg = one("SELECT COALESCE(AVG(sessions_count), 0) FROM users")
    conn.close()

    pct = (diary_users / total * 100) if total else 0
    lines = [
        "📊 *Статистика*",
        f"Всего пользователей (взаимодействовали): {total}",
        f"Из них делали /start: {started}",
        f"Пользуются дневником: {diary_users} ({pct:.0f}% от всех)",
        f"Всего записей: {entries}  (1: {b1} · 2–4: {b24} · 5+: {b5})",
        f"Активных подписчиков рассылки: {active_subs}",
    ]
    if dates:
        lines.append(f"Записей за 7 дней: {d7} · за 30 дней: {d30}")
        lines.append(f"Период записей: {min(dates):%d.%m.%Y} – {max(dates):%d.%m.%Y}")
    lines.append(f"Среднее число сессий на пользователя: {avg:.1f}")
    return "\n".join(lines)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админская команда /stats. Доступна только пользователю с ID = ADMIN_ID.
    Если ADMIN_ID не задан — подсказывает админу его собственный ID для настройки."""
    user_id = update.effective_user.id
    if not ADMIN_ID:
        await update.message.reply_text(
            f"ADMIN_ID не задан. Твой Telegram ID: {user_id}\n"
            "Задай переменную ADMIN_ID в Railway этим числом, чтобы включить /stats."
        )
        return
    if str(user_id) != str(ADMIN_ID):
        return  # не админ — молча игнорируем
    await update.message.reply_text(db_stats(), parse_mode='Markdown')

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

# Общие правила, добавляются ко ВСЕМ промптам: обращение на «ты», только русский язык,
# и определение рода ПО КОНТЕКСТУ (как пользователь сам говорит о себе).
GENDER_INSTRUCTION = (
    "Всегда обращайся к человеку на «ты», никогда на «вы». "
    "Пиши СТРОГО на русском языке и только кириллицей. Категорически запрещены иероглифы, "
    "китайские, японские и корейские символы, а также латиница — если просится иностранное "
    "слово, замени его русским. "
    "Про род: определяй род пользователя по тому, как он сам говорит "
    "о себе в переписке (например, «я устала», «я сделала», «я сама» → женский род; "
    "«я устал», «я сделал», «я сам» → мужской), и строго обращайся к нему в этом роде во всех "
    "окончаниях («какая ты есть», «ты сделала» — для женского). "
    "Пока род из переписки не ясен, используй нейтральные формулировки без родовых окончаний "
    "и не строй догадок. Никогда не используй мужской род по умолчанию. Если род уже проявился "
    "в разговоре раньше — придерживайся его дальше."
)

# Символы явно «не тех» письменностей (CJK, кана, хангыль, арабица, иврит) — признак того,
# что модель сорвалась в другой язык. Используется для перегенерации ответа.
_FOREIGN_SCRIPT_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿가-힯؀-ۿ֐-׿]"
)


async def get_ai_response(user_id, user_message, mode="chat", context_data=None,
                          extra_system=None, history=None):
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

<инструменты>
У тебя есть три структурированных инструмента, которые можно ПРЕДЛОЖИТЬ, когда это уместно:
— «дневник» — пошагово разобрать конкретную ситуацию и мысль (подходит, когда есть чёткая ситуация,
  которую хочется распутать по полочкам);
— «сократ» — исследовать одну навязчивую мысль через вопросы (подходит, когда человек зациклился на
  одной мысли-убеждении вроде «я ни на что не гожусь»);
— «дефузия» — короткое упражнение, чтобы отстраниться от тяжёлой мысли, а не спорить с ней (подходит,
  когда мысль слишком болезненна для разбора).
Если по ходу разговора один из них действительно поможет — в САМОМ КОНЦЕ ответа добавь ровно одну метку
в двойных квадратных скобках: [[дневник]], [[сократ]] или [[дефузия]]. Пользователь метку не увидит —
она превратится в кнопку с предложением. Добавляй метку РЕДКО и только когда она к месту, не в каждом
сообщении, и не чаще одной за раз. Сначала контакт и разговор — инструмент это лишь мягкое предложение.
ВАЖНО: если человек в кризисе или ему остро плохо (уровень 2 или 3 выше) — НЕ предлагай инструменты и
НЕ добавляй метку. Только бережный разговор и, если нужно, кризисная помощь.
</инструменты>

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

        "diary_distortion": f"""<роль>Ты — КПТ-терапевт.</роль>

<контекст>
Ситуация: {context_data.get('situation', '') if context_data else ''}
Эмоции: {context_data.get('emotion', '') if context_data else ''}
Автоматическая мысль: {context_data.get('thought', '') if context_data else ''}
</контекст>

<задача>
Реши, есть ли в автоматической мысли когнитивное искажение — и только если есть, назови ОДНО,
самое выраженное, из этого списка:
{chr(10).join('— ' + k for k in COGNITIVE_DISTORTIONS)}
</задача>

<когда_искажения_НЕТ>
Ответ «НЕТ» — такой же нормальный и частый результат, как название искажения. Многие тяжёлые мысли
совершенно здоровы. Отвечай НЕТ, если верно хотя бы одно:
— Реакция соразмерна тому, что реально произошло (событие действительно неприятное, а не додуманное).
— Это моральный дискомфорт: человеку не по себе от того, что расходится с его ценностями. Стыд или
  злость из-за чужого нечестного поступка — здоровая реакция совести, а НЕ ошибка мышления.
— Нарушена граница человека, или речь о реальном конфликте, потере, несправедливости.
— Человек описал ЧУВСТВО или ЖЕЛАНИЕ («мне некомфортно», «я не хочу здесь быть»), а не убеждение
  о себе или мире. Проверять на искажение можно только утверждение, которое сопоставимо с фактами.
Искажение — это интерпретация, выходящая ЗА пределы фактов. Если фактов достаточно, чтобы так
себя чувствовать, искажения нет. Не подгоняй мысль под ярлык, чтобы дать ответ.
</когда_искажения_НЕТ>

<формат>
Первая строка — РОВНО одно название из списка, без эмодзи, без кавычек, без пояснений,
либо РОВНО слово: НЕТ
Со второй строки (только если назвал искажение) — 1-2 предложения о том, что именно в мысли на это
указывает. Обращайся на «ты», тепло, своими словами.
ЗАПРЕЩЕНО: пересказывать слова человека его же словами; оценивать его («ты не рассматриваешь…»,
«ты не учитываешь…»); настаивать на своей правоте. Это наблюдение, которое легко отклонить.
</формат>""",

        "diary_no_distortion": f"""<роль>Ты — КПТ/ACT-терапевт.</роль>

<контекст>
Ситуация: {context_data.get('situation', '') if context_data else ''}
Эмоции: {context_data.get('emotion', '') if context_data else ''}
Мысль: {context_data.get('thought', '') if context_data else ''}
</контекст>

<задача>
В этой записи когнитивного искажения нет: реакция человека соразмерна произошедшему.
Признай чувство прямо и коротко — без «зато» и без утешений в духе «всё не так плохо».
Затем задай ОДИН вопрос о том, что стоит за этим чувством: какая ценность оказалась задета или
что человеку сейчас нужно.
</задача>

<запрещено>
Предлагать переформулировать мысль или найти более сбалансированный взгляд — балансировать здесь
нечего. Давать советы. Объяснять человеку его же чувства.
</запрещено>

<формат>2-3 предложения. Тепло, по-русски, без терминов.</формат>""",

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
    # Гендерная инструкция применяется ко всем режимам.
    system = GENDER_INSTRUCTION + "\n\n" + system
    if extra_system:
        # Дополняем системный промпт, НЕ заменяя его — так вся логика бережной реакции
        # на тревожные сообщения из базового промпта продолжает действовать.
        system = system + "\n\n" + extra_system
    messages = [{"role": "system", "content": system}]

    # История диалога. Для чата — общая история пользователя; для остальных многоходовых
    # режимов (сократ) — переданная вызывающим. Без неё модель не помнит разговор и зацикливается.
    if mode == "chat":
        for msg in data["history"][-10:]:
            messages.append(msg)
    elif history:
        for msg in history[-10:]:
            messages.append(msg)

    messages.append({"role": "user", "content": user_message})

    async def _call():
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model="llama-3.3-70b-versatile",
            messages=messages,
            max_tokens=500,
            # Temperature понижена с 0.7 до 0.45 — для терапевтического контекста модель должна быть
            # стабильнее в следовании правилам (не соскальзывать в советы вместо вопросов)
            temperature=0.45,
        )
        return response.choices[0].message.content

    reply = await _call()
    # Страховка: если модель сорвалась в иероглифы/другой алфавит — одна повторная попытка.
    if _FOREIGN_SCRIPT_RE.search(reply or ""):
        retry = await _call()
        if not _FOREIGN_SCRIPT_RE.search(retry or ""):
            reply = retry

    if mode == "chat":
        # В историю кладём ответ без скрытой метки инструмента, чтобы модель не видела
        # свои прошлые метки (сам возвращаемый текст остаётся с меткой — её разбирает вызывающий).
        clean_reply = _TOOL_TAG_RE.sub("", reply).strip()
        data["history"].append({"role": "user", "content": user_message})
        data["history"].append({"role": "assistant", "content": clean_reply})
        if len(data["history"]) > 20:
            data["history"] = data["history"][-20:]

    return reply

async def _typing_delay(chat, text=None):
    """Показывает «печатает…» и делает паузу в несколько секунд перед ответом —
    чтобы общение ощущалось естественнее, а не как мгновенный автоответ.
    Длительность слегка зависит от длины ответа, но ограничена (индикатор
    «печатает» в Telegram живёт ~5 секунд)."""
    seconds = 2.5 if not text else min(4.0, 1.8 + len(text) / 80.0)
    seconds += random.uniform(0.0, 0.6)
    try:
        await chat.send_action("typing")
    except Exception:
        pass
    await asyncio.sleep(seconds)

# =====================
# KEYBOARDS
# =====================

def main_keyboard():
    """Домашняя клавиатура. Главный экран — это разговор: человек просто пишет.
    Инструменты (дневник/сократ/дефузия) спрятаны под «Упражнения и техники»."""
    keyboard = [
        [KeyboardButton("Упражнения и техники")],
        [KeyboardButton("⊹ Мой прогресс"), KeyboardButton("ﾟ｡ Кризисная помощь")],
        [KeyboardButton("⭐ Поддержать проект")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Инструменты: единый источник — название кнопки, описание, callback, состояние запуска.
TOOLS_MENU = [
    ("💭 Дневник мыслей", "tool_diary",
     "пошагово разобрать ситуацию и найти более сбалансированный взгляд"),
    ("🧠 Сократовский диалог", "tool_socratic",
     "исследовать одну навязчивую мысль через вопросы"),
    ("🌊 Дефузия", "tool_defusion",
     "упражнение, чтобы отстраниться от тяжёлой мысли"),
]

def tools_inline_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(title, callback_data=cb)]
                                 for title, cb, _desc in TOOLS_MENU])

# Контекстное предложение конкретного инструмента (одна инлайн-кнопка под ответом в чате).
_OFFER_BUTTON = {
    "diary": ("💭 Разобрать в дневнике", "tool_diary"),
    "socratic": ("🧠 Исследовать эту мысль", "tool_socratic"),
    "defusion": ("🌊 Упражнение на дистанцию", "tool_defusion"),
}

def offer_tool_keyboard(tool: str):
    item = _OFFER_BUTTON.get(tool)
    if not item:
        return None
    title, cb = item
    return InlineKeyboardMarkup([[InlineKeyboardButton(title, callback_data=cb)]])

# Скрытая метка инструмента, которую модель ставит в конце ответа. Бот её вырезает
# (пользователь не видит) и превращает в кнопку-предложение.
_TOOL_TAGS = {"дневник": "diary", "сократ": "socratic", "дефузия": "defusion"}
_TOOL_TAG_RE = re.compile(r"\[\[\s*(дневник|сократ|дефузия)\s*\]\]", re.IGNORECASE)

def _extract_tool_tag(text: str):
    """Возвращает (текст_без_метки, tool|None). Если модель поставила метку —
    вырезаем её из видимого текста."""
    if not text:
        return text, None
    m = _TOOL_TAG_RE.search(text)
    if not m:
        return text.strip(), None
    tool = _TOOL_TAGS[m.group(1).lower()]
    clean = (text[:m.start()] + text[m.end():]).strip()
    return clean, tool

def distortions_keyboard():
    """Ручной выбор искажения — запасной путь, если догадка бота не подошла.
    На кнопках стоят формулировки от первого лица, а не термины: узнать себя в
    «Я жду худшего исхода» можно без подготовки, в «Катастрофизации» — нет.
    В callback_data — индекс, а не название: короче и не зависит от текста."""
    buttons = []
    for i, name in enumerate(COGNITIVE_DISTORTIONS):
        emoji = name.split()[0]
        buttons.append([InlineKeyboardButton(
            f"{emoji} {DISTORTION_PLAIN[name]}", callback_data=f"dist_i{i}"
        )])
    buttons.append([InlineKeyboardButton("❓ Ничего из этого", callback_data="dist_skip")])
    return InlineKeyboardMarkup(buttons)

def _norm_ru(text):
    """Только буквы и пробелы, нижний регистр, ё→е — для устойчивого сравнения
    названия искажения, которое вернула модель."""
    t = (text or "").lower().replace("ё", "е")
    return " ".join("".join(ch if ch.isalpha() or ch.isspace() else " " for ch in t).split())

def _match_distortion(line):
    """Название искажения из первой строки ответа модели → ключ COGNITIVE_DISTORTIONS."""
    n = _norm_ru(line)
    if not n or n.startswith("нет"):
        return None
    for key in COGNITIVE_DISTORTIONS:
        if _norm_ru(key) in n:
            return key
    return None

async def _guess_distortion(user_id, ud):
    """Просит модель назвать искажение по записи дневника.
    Возвращает (ключ_искажения, пояснение) или (None, None) — тогда показываем
    ручной список. Ошибка модели не должна ломать сценарий."""
    try:
        raw = await get_ai_response(
            user_id,
            f"Автоматическая мысль: {ud.get('td_thought', '')}",
            mode='diary_distortion',
            context_data={
                'situation': ud.get('td_situation', ''),
                'emotion': ud.get('td_emotion', ''),
                'thought': ud.get('td_thought', ''),
            },
        )
    except Exception as e:
        logger.error(f"Groq error (distortion guess): {e}")
        return None, None

    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    if not lines:
        return None, None
    key = _match_distortion(lines[0])
    if not key:
        return None, None
    # Пояснение модели необязательно: если его нет, берём формулировку от первого лица.
    explanation = " ".join(lines[1:]).strip()
    if not explanation:
        explanation = f"Смотри, что я замечаю в этой мысли: {DISTORTION_PLAIN[key].lower()}."
    return key, explanation

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
    context.user_data.clear()

    welcome = (
        f"Привет, {user.first_name} ✧\n\n"
        "Здесь можно выгрузить то, что крутится в голове, "
        "и вместе посмотреть на это спокойнее.\n\n"
        "Расскажи, что тебя привело или выбери технику в меню, "
        "если проще начать оттуда.\n\n"
        "Я бот, не терапевт. Но я рядом в любой момент, чтобы вместе разобраться."
    )
    await update.message.reply_text(welcome, reply_markup=main_keyboard())
    return MAIN_MENU

async def _chat_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    """Один ход обычного разговора: ответ ИИ + (если модель предложила) кнопка-инструмент.
    Используется и для текста, и для расшифрованного голоса на домашнем экране."""
    user_id = update.effective_user.id
    await update.message.chat.send_action("typing")
    try:
        reply = await get_ai_response(user_id, user_text, mode="chat", context_data={})
    except Exception as e:
        logger.error(f"Groq error: {e}")
        await update.message.reply_text(
            "Что-то пошло не так. Попробуй ещё раз.", reply_markup=main_keyboard()
        )
        return
    reply, tool = _extract_tool_tag(reply)
    await _typing_delay(update.message.chat, reply)
    offer = offer_tool_keyboard(tool) if tool else None
    # Если предлагаем инструмент — отправляем инлайн-кнопку (нижняя клавиатура остаётся видимой).
    # Иначе просто держим домашнюю клавиатуру.
    await update.message.reply_text(reply, reply_markup=offer or main_keyboard())

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Домашний экран = разговор. Кнопки-меню открывают инструменты/прогресс/помощь/донат,
    любой другой текст — это обычный разговор с ботом."""
    text = update.message.text

    if text == "Упражнения и техники":
        context.user_data.clear()
        await update.message.reply_text(
            "*Упражнения и техники*\n\n"
            "💭 *Дневник мыслей* — пошагово разобрать ситуацию и найти более сбалансированный взгляд.\n\n"
            "🧠 *Сократовский диалог* — исследовать одну навязчивую мысль через вопросы.\n\n"
            "🌊 *Дефузия* — упражнение, чтобы отстраниться от тяжёлой мысли.",
            parse_mode='Markdown',
            reply_markup=tools_inline_keyboard()
        )
        return MAIN_MENU

    elif text == "ﾟ✦ Главное меню":
        context.user_data.clear()
        await update.message.reply_text(
            "Я здесь. О чём хочешь поговорить? ｡ﾟ",
            reply_markup=main_keyboard()
        )
        return MAIN_MENU

    elif text == "⊹ Мой прогресс":
        user_id = update.effective_user.id
        diary_count = db_get_entry_count(user_id)
        sessions = db_get_sessions(user_id)

        text_progress = (
            f"📊 *Твой прогресс*\n\n"
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
            "Если ты в критическом моменте прямо сейчас:\n\n"
            "🇷🇺 *Россия:* 8-800-2000-122 (бесплатно)\n"
            "🌍 *Международная помощь:* findahelpline.com\n\n"
            "Если есть мысли о самоповреждении — это медицинская ситуация. "
            "Пожалуйста, позвони на горячую линию.\n\n"
            "Я здесь, если хочешь поговорить 🤍"
        )
        await update.message.reply_text(crisis, parse_mode='Markdown', reply_markup=main_keyboard())
        return MAIN_MENU

    elif text == "⭐ Поддержать проект":
        return await donate(update, context)

    else:
        # Любой другой текст — это обычный разговор (главный режим бота).
        await _chat_and_reply(update, context, text)
        return MAIN_MENU

async def tool_launch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск инструмента из инлайн-кнопки (меню «Упражнения и техники» или контекстного
    предложения в чате). Переводит разговор в соответствующий сценарий."""
    query = update.callback_query
    await query.answer()
    tool = query.data  # tool_diary / tool_socratic / tool_defusion
    # чистим черновики предыдущих сценариев.
    # reflection_mode здесь обязателен: он снимается только текстовыми кнопками меню,
    # а инлайн-кнопка инструмента их минует. Залипший режим перехватывал бы шаги
    # сценария в group=-2, и дневник было бы не пройти.
    for k in ('td_situation', 'td_emotion', 'td_thought', 'td_distortion', 'td_reframe',
              'td_emotion_after', 'mode', 'socratic_thought', 'socratic_history', 'defusion_thought',
              'reflection_mode', 'reflection_question'):
        context.user_data.pop(k, None)

    if tool == "tool_diary":
        await query.message.reply_text(
            "📓 *Дневник мыслей*\n\n"
            "Запишем ситуацию, эмоции и мысль — и найдём более сбалансированный взгляд.\n\n"
            "Опиши ситуацию, которая тебя беспокоит:",
            parse_mode='Markdown', reply_markup=back_keyboard()
        )
        return THOUGHT_DIARY_SITUATION

    if tool == "tool_socratic":
        context.user_data['mode'] = 'socratic'
        await query.message.reply_text(
            "🧠 *Сократовский диалог*\n\n"
            "Напиши мысль, которую хочешь исследовать.\n"
            "_(Например: «Я никогда не справлюсь», «Все меня осуждают»)_",
            parse_mode='Markdown', reply_markup=back_keyboard()
        )
        return AI_CHAT

    if tool == "tool_defusion":
        await query.message.reply_text(
            "🌊 *Когнитивная дефузия*\n\n"
            "Напиши мысль, которая тебя беспокоит:",
            parse_mode='Markdown', reply_markup=back_keyboard()
        )
        return DEFUSION_THOUGHT

    return MAIN_MENU

async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "ﾟ✦ Главное меню":
        # Выходим из сократа домой — обязательно чистим режим/черновики, иначе залипший
        # mode='socratic' на домашнем экране увёл бы голосовые сообщения в сократ.
        context.user_data.clear()
        await update.message.reply_text(
            "Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard()
        )
        return MAIN_MENU

    mode = context.user_data.get('mode', 'chat')
    socratic_thought = context.user_data.get('socratic_thought', '')

    if mode == 'socratic' and not socratic_thought:
        context.user_data['socratic_thought'] = text
        context.user_data['socratic_history'] = []

    await update.message.chat.send_action("typing")

    try:
        if mode == 'socratic':
            socratic_history = context.user_data.setdefault('socratic_history', [])
            context_data = {'thought': context.user_data.get('socratic_thought', text)}
            reply = await get_ai_response(
                user_id, text, mode='socratic', context_data=context_data, history=socratic_history
            )
            # Копим историю сократовского диалога, иначе следующий ход снова «с нуля».
            socratic_history.append({"role": "user", "content": text})
            socratic_history.append({"role": "assistant", "content": reply})
            if len(socratic_history) > 20:
                del socratic_history[:-20]
        else:
            reply = await get_ai_response(user_id, text, mode='chat', context_data={})

        await _typing_delay(update.message.chat, reply)
        await update.message.reply_text(reply, reply_markup=back_keyboard())
    except Exception as e:
        logger.error(f"Groq error: {e}")
        await update.message.reply_text(
            "Что-то пошло не так. Попробуй ещё раз или вернись в меню.",
            reply_markup=back_keyboard()
        )

    return AI_CHAT

# === THOUGHT DIARY ===

async def thought_diary_emotion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard())
        return MAIN_MENU
    context.user_data['td_situation'] = update.message.text
    await update.message.reply_text(
        "Какие эмоции при этом были — и насколько сильно (0-100%)?\n\n"
        "_(Например: тревога 70%, стыд 40%)_",
        parse_mode='Markdown',
        reply_markup=back_keyboard()
    )
    return THOUGHT_DIARY_EMOTION

async def thought_diary_thought(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard())
        return MAIN_MENU
    context.user_data['td_emotion'] = update.message.text
    await update.message.reply_text(
        "Что промелькнуло в голове в тот момент?\n\n"
        "Попробуй поймать *автоматическую мысль* — первую реакцию до анализа.\n\n"
        "_(Например: «Опять ничего не вышло», «Они точно думают обо мне плохо»)_",
        parse_mode='Markdown',
        reply_markup=back_keyboard()
    )
    return THOUGHT_DIARY_THOUGHT

async def thought_diary_distortion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard())
        return MAIN_MENU
    context.user_data['td_thought'] = update.message.text

    # Искажение предполагает бот, а не пользователь. Самодиагностика по списку терминов —
    # работа терапевта: человек в тяжёлом состоянии не обязан знать классификацию Burns.
    # Не согласиться можно всегда — кнопка «Не совсем» равноправна с «Да, похоже».
    await update.message.chat.send_action("typing")
    guess, explanation = await _guess_distortion(update.effective_user.id, context.user_data)

    if not guess:
        # Раньше здесь сразу открывался список — то есть та же самодиагностика, только
        # с другой стороны. Отсутствие искажения — нормальный результат, и говорить о нём
        # надо прямо: не каждая тяжёлая мысль искажена.
        await update.message.reply_text(
            "Здесь я не вижу ошибки мышления — то, что ты описываешь, выглядит соразмерным тому, "
            "что произошло.\n\n"
            "Не каждую тяжёлую мысль нужно переубеждать.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Идти дальше", callback_data="dist_none")],
                [InlineKeyboardButton("Всё-таки посмотреть список", callback_data="dist_show")],
            ])
        )
        return THOUGHT_DIARY_DISTORTION

    context.user_data['td_distortion_guess'] = guess
    term = guess.split(maxsplit=1)[1]  # название без эмодзи
    await update.message.reply_text(
        f"{explanation}\n\n"
        f"В КПТ это называют «{term.lower()}» — {COGNITIVE_DISTORTIONS[guess][0].lower()}"
        f"{COGNITIVE_DISTORTIONS[guess][1:]}\n\n"
        "Похоже на твой случай?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Да, похоже", callback_data="dist_yes")],
            [InlineKeyboardButton("Не совсем — покажи другие", callback_data="dist_other")],
            [InlineKeyboardButton("Пропустить этот шаг", callback_data="dist_skip")],
        ])
    )
    return THOUGHT_DIARY_DISTORTION

async def handle_distortion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = query.data[5:]  # то, что после 'dist_'

    # Искажения нет — ведём по отдельной ветке: спрашивать «более сбалансированную мысль»
    # здесь неуместно, балансировать нечего.
    if action == "none":
        context.user_data['td_distortion'] = "Искажения нет"
        await query.edit_message_reply_markup(reply_markup=None)
        try:
            question = await get_ai_response(
                query.from_user.id,
                "Признай чувство и спроси, что за ним стоит",
                mode='diary_no_distortion',
                context_data={
                    'situation': context.user_data.get('td_situation', ''),
                    'emotion': context.user_data.get('td_emotion', ''),
                    'thought': context.user_data.get('td_thought', ''),
                },
            )
        except Exception as e:
            logger.error(f"Groq error (no distortion): {e}")
            question = ("Понятно, почему тебе было так. Что для тебя было важно в этот момент — "
                        "что оказалось задето?")
        await query.message.reply_text(question, reply_markup=back_keyboard())
        return THOUGHT_DIARY_MEANING

    # «Всё-таки посмотреть список» / «Не совсем» — показываем ручной выбор.
    if action == "show":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Хорошо, посмотри сама — узнаёшь что-то из этого?",
            reply_markup=distortions_keyboard()
        )
        return THOUGHT_DIARY_DISTORTION

    if action == "other":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Хорошо, тебе виднее. Может, ближе что-то из этого?",
            reply_markup=distortions_keyboard()
        )
        return THOUGHT_DIARY_DISTORTION

    if action == "skip":
        context.user_data['td_distortion'] = "Не определено"
        distortion_info = ""
    else:
        if action == "yes":
            full_key = context.user_data.get('td_distortion_guess')
        else:
            # dist_i<индекс> — ручной выбор из списка
            keys = list(COGNITIVE_DISTORTIONS)
            try:
                full_key = keys[int(action[1:])]
            except (ValueError, IndexError):
                full_key = None
        if not full_key:
            # Догадка потерялась (например, после перезапуска) — шаг не блокируем.
            full_key = "Не определено"
        context.user_data['td_distortion'] = full_key
        distortion_info = (f"\n\n💡 _{COGNITIVE_DISTORTIONS[full_key]}_"
                           if full_key in COGNITIVE_DISTORTIONS else "")

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
            "Напиши альтернативную, более сбалансированную мысль:",
            reply_markup=back_keyboard()
        )

    return THOUGHT_DIARY_REFRAME

async def thought_diary_reframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь прислал альтернативную мысль. Вместо немедленного сохранения —
    задаём шаг переоценки эмоции (reframe_check), опора: Judith S. Beck."""
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard())
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

async def thought_diary_meaning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ветка «искажения нет»: человек ответил, что за чувством стоит. Переформулирование
    мысли здесь пропускаем — вместо него сохраняем то, что оказалось важно."""
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard())
        return MAIN_MENU

    context.user_data['td_meaning'] = update.message.text
    # Вопрос статичный: формулировка reframe_check («после того как ты переформулировал мысль»)
    # для этой ветки не подходит.
    await update.message.reply_text(
        "Как сейчас ощущается та эмоция, с которой всё началось — от 0 до 100%?",
        reply_markup=back_keyboard()
    )
    return THOUGHT_DIARY_EMOTION_RECHECK

async def thought_diary_emotion_recheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финальный шаг: сохраняем запись вместе с переоценённой силой эмоции."""
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard())
        return MAIN_MENU

    user_id = update.effective_user.id
    context.user_data['td_emotion_after'] = update.message.text

    entry = {
        "situation": context.user_data.get('td_situation', ''),
        "emotion": context.user_data.get('td_emotion', ''),
        "thought": context.user_data.get('td_thought', ''),
        "distortion": context.user_data.get('td_distortion', ''),
        "reframe": context.user_data.get('td_reframe', ''),
        # Ветка «искажения нет» сохраняет не альтернативу, а то, что оказалось важно:
        # подписывать это «альтернативой» было бы неверно по смыслу.
        "meaning": context.user_data.get('td_meaning', ''),
        "emotion_after": context.user_data.get('td_emotion_after', ''),
    }
    db_save_entry(user_id, entry)

    if entry['meaning']:
        outcome = f"ヅ︎ Что оказалось важно: _{entry['meaning'][:150]}_"
        closing = "Заметить, что именно было задето, — уже работа. Не всё нужно исправлять."
    else:
        outcome = f"ヅ︎ Альтернатива: _{entry['reframe'][:150]}_"
        closing = "Отличная работа. Каждая такая запись постепенно меняет нейронные паттерны."

    summary = (
        "✓︎ *Запись сохранена*\n\n"
        f"♒︎ Исходная мысль: _{entry['thought'][:100]}_\n"
        f"{outcome}\n"
        f"↺ Эмоция до/после: _{entry['emotion']}_ → _{entry['emotion_after']}_\n\n"
        f"{closing}"
    )
    await update.message.reply_text(summary, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

# === DEFUSION ===

async def defusion_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard())
        return MAIN_MENU
    context.user_data['defusion_thought'] = update.message.text
    await update.message.reply_text(
        "Можно попробовать любую технику — правильного выбора нет, "
        "выбирай по настроению:\n\n"
        "｡ﾟ *Листья на воде* — представить мысль плывущей по воде и мягко отпустить.\n\n"
        "✦ *Наблюдатель* — посмотреть на мысль со стороны, спокойно, без оценок.\n\n"
        "･ﾟ *Персонаж* — добавить «у меня есть мысль, что…» и почувствовать дистанцию.",
        parse_mode='Markdown',
        reply_markup=defusion_keyboard()
    )
    return DEFUSION_TECHNIQUE

async def handle_defusion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chosen_key = query.data[4:]

    full_key = next((k for k in DEFUSION_TECHNIQUES if k[:30] == chosen_key), chosen_key)
    technique_text = DEFUSION_TECHNIQUES.get(full_key, "")

    # Убираем кнопки выбора у исходного сообщения, чтобы техника не выбиралась повторно.
    await query.edit_message_text(f"🌊 *{full_key}*", parse_mode='Markdown')
    # Саму технику шлём отдельным сообщением: нужна нижняя клавиатура (edit_message_text
    # её выставить не может), и вопрос «как ты себя чувствуешь» должен остаться последним.
    await query.message.reply_text(
        f"{technique_text}\n\n"
        "Побудь с этим 1-2 минуты. Как ты себя чувствуешь после?",
        parse_mode='Markdown',
        reply_markup=back_keyboard()
    )
    return DEFUSION_REFLECT

async def defusion_reflect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ответ на «как ты себя чувствуешь после упражнения». Раньше бот здесь сразу
    перебивал себя фразой про главное меню — теперь ждёт ответ и отвечает на него."""
    if update.message.text == "ﾟ✦ Главное меню":
        context.user_data.clear()
        await update.message.reply_text(
            "Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard()
        )
        return MAIN_MENU

    context.user_data.pop('defusion_thought', None)
    await _chat_and_reply(update, context, update.message.text)
    return MAIN_MENU

# === ЕЖЕДНЕВНЫЕ СООБЩЕНИЯ: ПЛАНИРОВЩИК ===
# Само расписание (утро/вечер, случайное время в окне, ротация без повторов) и тексты
# сообщений живут в messages_rotation.py. Здесь — только подписка/восстановление.

async def toggle_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # reply_markup намеренно не задаём: кнопка доступна из любого места переписки,
    # и подмена нижней клавиатуры выбросила бы пользователя из текущего сценария.
    if db_is_subscribed(user_id):
        db_unsubscribe(user_id)
        await query.message.reply_text(
            "🔕 Ежедневные сообщения выключены. Включить их снова можно в любой момент "
            "в разделе «⊹ Мой прогресс»."
        )
    else:
        db_subscribe(user_id)
        setup_user_schedule(context.job_queue, user_id)
        await query.message.reply_text(
            "🔔 Готово — буду присылать пару сообщений в день, утром и вечером, "
            "время каждый раз немного случайное."
        )
    # None — состояние диалога не меняем (см. комментарий выше).

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
        setup_user_schedule(context.job_queue, user.id)

# === ОТВЕТ НА ВОПРОС ДНЯ (кнопка «💬 Ответить» под утренними/вечерними вопросами) ===

# Добавка к системному промпту режима "chat" — НЕ заменяет его, а дополняет, поэтому
# вся логика бережной реакции на тревожные сообщения (<безопасность_уровни> в промпте "chat")
# продолжает действовать и в этой ветке.
REFLECTION_SYSTEM_SUFFIX = (
    "Пользователь размышляет над вопросом дня: «{question}».\n"
    "Поддерживай разговор в духе ACT: валидируй сказанное, помогай мягко углублять "
    "размышление — по одному вопросу за раз. Не оценивай ответ как правильный или "
    "неправильный, не давай советов, если их не просят. Отвечай тепло и коротко "
    "(2–4 предложения). Разговор может продолжаться столько, сколько нужно человеку — "
    "не обрывай его и не подталкивай к завершению. Если сообщение пользователя явно "
    "не связано с вопросом — просто ответь на него как в обычном диалоге."
)

# Кнопки основного меню — на них выходим из режима рефлексии обратно в обычный поток бота.
MENU_BUTTONS = {
    "Упражнения и техники", "⊹ Мой прогресс", "ﾟ｡ Кризисная помощь",
    "⭐ Поддержать проект", "ﾟ✦ Главное меню",
}

async def reflect_answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка «Ответить» под вопросом дня. Запоминаем текст вопроса (из самого
    сообщения) и включаем режим рефлексии — он держится, пока пользователь не выйдет
    кнопкой меню, так что говорить об этом можно сколько нужно."""
    query = update.callback_query
    await query.answer()
    context.user_data["reflection_mode"] = True
    context.user_data["reflection_question"] = query.message.text
    await query.message.reply_text(
        "Слушаю тебя ✧",
        reply_markup=back_keyboard(),
    )

async def _do_reflection_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Отправляет ответ пользователя на вопрос дня в Groq и отвечает. Общая логика
    для текстовых и голосовых сообщений в режиме рефлексии.
    Приватность: текст ответа пользователя нигде не логируется."""
    question = context.user_data.get("reflection_question", "")
    user_id = update.effective_user.id
    await update.message.chat.send_action("typing")
    try:
        reply = await get_ai_response(
            user_id,
            text,
            mode="chat",
            context_data={},
            extra_system=REFLECTION_SYSTEM_SUFFIX.format(question=question),
        )
        await _typing_delay(update.message.chat, reply)
        await update.message.reply_text(reply, reply_markup=back_keyboard())
    except Exception as e:
        logger.error(f"Groq error (reflection): {e}")  # логируем только факт ошибки, без текста
        await update.message.reply_text(
            "Что-то пошло не так. Попробуй ещё раз или нажми «ﾟ✦ Главное меню», чтобы вернуться.",
            reply_markup=back_keyboard(),
        )

async def reflection_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный текстовый хендлер (group=-2). Пока включён режим рефлексии, каждое
    текстовое сообщение пользователя идёт в Groq как продолжение разговора о вопросе дня.
    Режим НЕ одноразовый: выход — только по кнопке меню (см. MENU_BUTTONS)."""
    if not context.user_data.get("reflection_mode"):
        return  # обычное сообщение — пусть обрабатывается штатными хендлерами (conv handler)

    text = update.message.text
    # Выход из режима: любая кнопка меню возвращает в обычный поток. НЕ перехватываем —
    # даём ConversationHandler обработать нажатие штатно.
    if text in MENU_BUTTONS:
        context.user_data.pop("reflection_mode", None)
        context.user_data.pop("reflection_question", None)
        return

    await _do_reflection_reply(update, context, text)
    # Остаёмся в режиме рефлексии (многоходовой). Это сообщение обработано здесь —
    # не пускаем его в ConversationHandler.
    raise ApplicationHandlerStop

async def reflection_voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Голосовой хендлер (group=-2). В режиме рефлексии распознаёт голосовое и обрабатывает
    его как ответ на вопрос дня — чтобы рефлексию можно было продолжать и голосом."""
    if not context.user_data.get("reflection_mode"):
        return  # не в рефлексии — пусть голос обрабатывается штатно (conv handler)

    await update.message.chat.send_action("typing")
    try:
        voice = await update.message.voice.get_file()
        text = await transcribe_voice(voice)
    except Exception as e:
        logger.error(f"Voice error (reflection): {e}")
        await update.message.reply_text(
            "Не удалось обработать голосовое. Попробуй написать текстом.",
            reply_markup=back_keyboard(),
        )
        raise ApplicationHandlerStop

    if not text.strip():
        await update.message.reply_text(
            "Не удалось распознать голосовое. Попробуй ещё раз или напиши текстом.",
            reply_markup=back_keyboard(),
        )
        raise ApplicationHandlerStop

    await update.message.reply_text(f"🎤 _{text}_", parse_mode='Markdown')
    await _do_reflection_reply(update, context, text)
    raise ApplicationHandlerStop

# === DONATE ===

async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Спасибо, что ты здесь ✦\n\n"
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
        description="Спасибо, что ты здесь. Твоя поддержка помогает развивать проект ✦",
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
        "Спасибо ⭐ Это очень важно и приятно.\nТы помогаешь проекту жить дальше ✦",
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
        await query.message.reply_text("Записей пока нет.")
        return

    text = "📋 *Твои последние записи:*\n\n"
    for i, entry in enumerate(entries, 1):
        text += (
            f"*{i}. {entry.get('date', '')}*\n"
            f"Ситуация: _{entry.get('situation', '')[:80]}_\n"
            f"Мысль: _{entry.get('thought', '')[:80]}_\n"
        )
        # Записи из ветки «искажения нет» хранят не альтернативу, а то, что было важно.
        if entry.get('meaning'):
            text += f"Что оказалось важно: _{entry['meaning'][:80]}_\n"
        elif entry.get('reframe'):
            text += f"Альтернатива: _{entry['reframe'][:80]}_\n"
        if entry.get('emotion_after'):
            text += f"Эмоция до/после: _{entry.get('emotion', '')}_ → _{entry.get('emotion_after', '')}_\n"
        text += "\n"

    # Показ записей — справочное действие: нижнюю клавиатуру и состояние не трогаем.
    await query.message.reply_text(text, parse_mode='Markdown')

# === PRIVACY / DATA CONTROL ===

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
    # Это только запрос подтверждения — состояние диалога не меняем.

async def delete_data_execute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db_delete_user_data(query.from_user.id)
    # Здесь состояние сбрасываем осознанно: после удаления данных незаконченный
    # сценарий с черновиками в user_data продолжать нельзя.
    context.user_data.clear()
    await query.edit_message_text("🗑 Все твои данные удалены.")
    await query.message.reply_text("Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard())
    return MAIN_MENU

async def delete_data_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Отменено. Твои данные остались нетронуты.")
    # Состояние не меняем — пользователь возвращается туда, где был.

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я здесь. О чём хочешь поговорить? ｡ﾟ", reply_markup=main_keyboard())
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
                "Не удалось распознать голосовое. Попробуй говорить чётче или напиши текстом.",
                reply_markup=back_keyboard()
            )
            return

        await update.message.reply_text(f"🎤 _{text}_", parse_mode='Markdown')

        user_id = update.effective_user.id
        mode = context.user_data.get('mode', 'chat')

        if mode == 'socratic':
            if not context.user_data.get('socratic_thought'):
                context.user_data['socratic_thought'] = text
            socratic_history = context.user_data.setdefault('socratic_history', [])
            context_data = {'thought': context.user_data.get('socratic_thought', text)}
            reply = await get_ai_response(
                user_id, text, mode='socratic', context_data=context_data, history=socratic_history
            )
            socratic_history.append({"role": "user", "content": text})
            socratic_history.append({"role": "assistant", "content": reply})
            if len(socratic_history) > 20:
                del socratic_history[:-20]
            await _typing_delay(update.message.chat, reply)
            await update.message.reply_text(reply, reply_markup=back_keyboard())
        else:
            # Обычный разговор голосом на домашнем экране — с возможным предложением инструмента.
            reply = await get_ai_response(user_id, text, mode='chat', context_data={})
            reply, tool = _extract_tool_tag(reply)
            await _typing_delay(update.message.chat, reply)
            offer = offer_tool_keyboard(tool) if tool else None
            await update.message.reply_text(reply, reply_markup=offer or main_keyboard())

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(
            "Не удалось обработать голосовое. Попробуй написать текстом.",
            reply_markup=back_keyboard()
        )

# =====================
# MAIN
# =====================

def main():
    init_db()

    # Персистентность: состояние диалога и user_data (шаг сценария, режим рефлексии,
    # прогресс дневника) сохраняются на диск и переживают перезапуск процесса.
    # Файл кладём рядом с БД — на том же примонтированном томе /app/data.
    persistence = PicklePersistence(
        filepath=os.path.join(os.path.dirname(DB_PATH), "bot_state.pickle")
    )
    application = Application.builder().token(TOKEN).persistence(persistence).build()

    # Инлайн-кнопки, которые должны работать из ЛЮБОГО места переписки: пользователь
    # может прокрутить историю вверх и нажать кнопку под старым сообщением. Раньше они
    # висели только в MAIN_MENU/AI_CHAT, и нажатие посреди сценария не делало ничего —
    # даже индикатор загрузки в Telegram не гас, потому что query.answer() не вызывался.
    # Глобальным слоем (group=-2) это не решается: оттуда нельзя вернуть состояние,
    # а tool_* обязан переключать сценарий. Поэтому подмешиваем список в каждое состояние.
    def common_callbacks():
        return [
            CallbackQueryHandler(tool_launch_callback, pattern='^tool_'),
            CallbackQueryHandler(donate_stars_callback, pattern='^donate_stars$'),
            CallbackQueryHandler(show_diary_callback, pattern='^show_diary$'),
            CallbackQueryHandler(delete_data_ask_callback, pattern='^delete_data_ask$'),
            CallbackQueryHandler(delete_data_execute_callback, pattern='^delete_data_execute$'),
            CallbackQueryHandler(delete_data_cancel_callback, pattern='^delete_data_cancel$'),
            CallbackQueryHandler(toggle_subscription_callback, pattern='^toggle_subscription$'),
        ]

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            MessageHandler(filters.VOICE, handle_voice),
            # Любой текст тоже входная точка: состояние диалога хранится в памяти и теряется
            # при перезапуске процесса (Railway перезапускает при деплое). Без этого после
            # рестарта кнопки меню «не работали» до повторного /start. Теперь любое сообщение
            # заново входит в диалог через handle_main_menu.
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu),
        ],
        states={
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu),
                MessageHandler(filters.VOICE, handle_voice),
                MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment),
            ] + common_callbacks(),
            AI_CHAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ai_chat),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
            THOUGHT_DIARY_SITUATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_emotion),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
            THOUGHT_DIARY_EMOTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_thought),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
            THOUGHT_DIARY_THOUGHT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_distortion),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
            THOUGHT_DIARY_DISTORTION: [
                CallbackQueryHandler(handle_distortion_callback, pattern='^dist_'),
                # На шаге выбора искажения кнопка «Главное меню» (и любой текст/голос) тоже
                # должна работать — иначе пользователь застревал бы, если не нажал инлайн-кнопку.
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
            THOUGHT_DIARY_REFRAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_reframe),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
            THOUGHT_DIARY_EMOTION_RECHECK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_emotion_recheck),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
            DEFUSION_THOUGHT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, defusion_choose),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
            DEFUSION_TECHNIQUE: [
                CallbackQueryHandler(handle_defusion_callback, pattern='^def_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
            # Ветка «искажения нет»: вместо переформулирования мысли — что оказалось важно.
            THOUGHT_DIARY_MEANING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_meaning),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
            # Шаг «как ты себя чувствуешь после упражнения»: ждём ответ, а не бросаем в меню.
            DEFUSION_REFLECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, defusion_reflect),
                MessageHandler(filters.VOICE, handle_voice),
            ] + common_callbacks(),
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('start', start),
        ],
        name="main_conversation",
        persistent=True,
    )

    # Админская команда /stats — работает в любом состоянии.
    application.add_handler(CommandHandler('stats', stats_command), group=-2)

    # Ответ на вопрос дня — самый приоритетный слой (group=-2): если пользователь нажал
    # «Ответить», его следующий текст/голос перехватывается здесь и не уходит в ConversationHandler.
    application.add_handler(CallbackQueryHandler(reflect_answer_callback, pattern='^reflect_answer$'), group=-2)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reflection_reply_handler), group=-2)
    application.add_handler(MessageHandler(filters.VOICE, reflection_voice_handler), group=-2)

    # Молчаливая авто-подписка на ежедневные сообщения — срабатывает раньше conv-хендлера
    # (group=-1) на любое сообщение или нажатие кнопки.
    application.add_handler(MessageHandler(filters.ALL, auto_subscribe_on_interaction), group=-1)
    application.add_handler(CallbackQueryHandler(auto_subscribe_on_interaction), group=-1)

    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    application.add_handler(conv_handler)

    # Регистрируем в модуле рассылки трекер «уже отправлено сегодня» (на основе БД) —
    # чтобы редеплой/рестарт среди дня не отправил сообщение повторно.
    set_sent_tracker(db_already_sent_today, db_mark_sent_today)

    # Восстановление рассылки после рестарта: run_once-джобы теряются при перезапуске
    # процесса (Railway перезапускает при деплое), поэтому при старте заново планируем
    # утро/вечер для каждого подписчика из БД. Само перепланирование на последующие дни
    # делают колбэки send_morning/send_evening внутри messages_rotation.py.
    #
    # ВАЖНО: при старте НЕ досылаем сегодняшние слоты — помечаем оба как отправленные
    # сегодня, чтобы setup_user_schedule запланировал их на завтра. Это защита от дублей:
    # иначе редеплой среди дня мог бы прислать сообщение ещё раз. Рассылка возобновляется
    # со следующего дня. (Новых подписчиков это не касается — они подписываются через
    # кнопку и получают сегодняшние слоты штатно.)
    for sub_user_id, _sub_timezone in db_get_all_subscribers():
        try:
            db_mark_sent_today(sub_user_id, "morning")
            db_mark_sent_today(sub_user_id, "evening")
            setup_user_schedule(application.job_queue, sub_user_id)
        except Exception as e:
            logger.error(f"Не удалось восстановить расписание для {sub_user_id}: {e}")

    print("🤖 GoNeuralShift запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
