import os
import logging
import tempfile
import sqlite3
import hashlib
import json
from telegram.ext import PreCheckoutQueryHandler
from datetime import datetime
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

groq_client = Groq(api_key=GROQ_API_KEY)

# States
(MAIN_MENU, AI_CHAT,
 THOUGHT_DIARY_SITUATION, THOUGHT_DIARY_EMOTION, THOUGHT_DIARY_THOUGHT,
 THOUGHT_DIARY_DISTORTION, THOUGHT_DIARY_REFRAME,
 DEFUSION_THOUGHT, DEFUSION_TECHNIQUE) = range(9)

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

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
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
    conn.commit()
    conn.close()

def get_user_hash(user_id: int) -> str:
    """One-way hash of user_id — owner cannot reverse it to identify user."""
    secret = os.getenv("HASH_SECRET", "goneuralshift_secret")
    return hashlib.sha256(f"{user_id}{secret}".encode()).hexdigest()

def db_get_sessions(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT sessions_count FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else 0

def db_increment_sessions(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO users (user_id, sessions_count, first_seen)
        VALUES (?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET sessions_count = sessions_count + 1
    """, (user_id, datetime.now().strftime("%d.%m.%Y")))
    conn.commit()
    conn.close()

def db_save_entry(user_id: int, entry: dict):
    user_hash = get_user_hash(user_id)
    entry["date"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO diary (user_hash, date, data) VALUES (?, ?, ?)",
        (user_hash, entry["date"], json.dumps(entry, ensure_ascii=False))
    )
    conn.commit()
    conn.close()

def db_get_entries(user_id: int, limit: int = 5) -> list:
    user_hash = get_user_hash(user_id)
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT data FROM diary WHERE user_hash=? ORDER BY id DESC LIMIT ?",
        (user_hash, limit)
    ).fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]

def db_get_entry_count(user_id: int) -> int:
    user_hash = get_user_hash(user_id)
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM diary WHERE user_hash=?", (user_hash,)
    ).fetchone()[0]
    conn.close()
    return count

# =====================
# IN-MEMORY (chat history only)
# =====================

user_sessions = {}

def get_user_data(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = {"history": []}
    return user_sessions[user_id]

def get_ai_response(user_id, user_message, mode="chat", context_data=None):
    data = get_user_data(user_id)

    system_prompts = {
        "chat": """Ты — GoNeuralShift, КПТ-ассистент. Говоришь как живой, тёплый человек которому реально интересно что происходит с собеседником — не как терапевт из учебника.

Как ты работаешь:

— Сначала контакт, потом всё остальное. Не торопись к техникам и переосмыслению — убедись что человек почувствовал что его услышали.
— Ты участвуешь, а не просто отражаешь. У тебя есть своя реакция на то что говорит человек. После отклика — один конкретный вопрос. Не общий («как ты себя чувствуешь?»), а точный («это случилось один раз или так бывает часто?»).
— Замечай то что не сказано. Если человек говорит «всё нормально» но описывает явно тяжёлую ситуацию — это важно. Мягко обрати внимание.
— Валидируй чувства — но не сливайся с деструктивными мыслями. Можно признать что человеку больно и одновременно мягко поставить под сомнение его интерпретацию.
— Разделяй человека и его мысли. Не «ты неудачник» — а «ты думаешь что ты неудачник». Это важно.
— Никогда не говори человеку что ему делать. Только вопросы и наблюдения — пусть сам приходит к выводам.
— Если видишь когнитивное искажение — назови его мягко, как наблюдение, не нотацию.
— Если человек написал мало — не засыпай вопросами. Один вопрос. Дай пространство.

Формат:
— Максимум 2-3 предложения
— Живой язык, никакого канцелярита
— Никаких слов: «безусловно», «конечно», «это важно», «я понимаю»

Если человеку очень плохо — скажи прямо что это серьёзно и предложи кнопку ✴ Кризисная помощь.
Ты не замена психотерапевту.""",

        "diary_reframe": f"""Ты — КПТ-терапевт. Пользователь заполнил дневник мыслей:
Ситуация: {context_data.get('situation', '') if context_data else ''}
Эмоции: {context_data.get('emotion', '') if context_data else ''}
Автоматическая мысль: {context_data.get('thought', '') if context_data else ''}
Когнитивное искажение: {context_data.get('distortion', '') if context_data else ''}

Помоги пользователю сформулировать более сбалансированную альтернативную мысль.
Задай один точный сократовский вопрос который поможет увидеть ситуацию шире.
Отвечай тепло, по-русски, коротко.""",

        "socratic": f"""Ты — КПТ-терапевт ведущий сократовский диалог.
Мысль пользователя: {context_data.get('thought', '') if context_data else ''}
История диалога уже есть в сообщениях.

Задавай по одному глубокому вопросу за раз. Не давай ответов — только вопросы.
Цель: помочь человеку самому обнаружить слабые места в деструктивной мысли.
По итогу (через 4-5 обменов) мягко подведи к более сбалансированному взгляду.
Отвечай по-русски, тепло и коротко.""",
    }

    system = system_prompts.get(mode, system_prompts["chat"])
    messages = [{"role": "system", "content": system}]

    if mode == "chat":
        for msg in data["history"][-10:]:
            messages.append(msg)

    messages.append({"role": "user", "content": user_message})

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=500,
        temperature=0.7,
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
        "Это пространство для работы с мыслями — теми, что давят, "
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
            "Расскажите что происходит. Я здесь и слушаю 🤍\n\n"
            "_(Напишите «ﾟ✦ Главное меню» чтобы вернуться)_",
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

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Мои записи", callback_data="show_diary")]
        ]) if diary_count > 0 else None

        await update.message.reply_text(
            text_progress,
            parse_mode='Markdown',
            reply_markup=keyboard if keyboard else main_keyboard()
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
            reply = get_ai_response(user_id, text, mode='socratic', context_data=context_data)
        else:
            reply = get_ai_response(user_id, text, mode='chat', context_data={})

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
        ai_question = get_ai_response(
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
    if update.message.text == "ﾟ✦ Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    user_id = update.effective_user.id
    context.user_data['td_reframe'] = update.message.text

    entry = {
        "situation": context.user_data.get('td_situation', ''),
        "emotion": context.user_data.get('td_emotion', ''),
        "thought": context.user_data.get('td_thought', ''),
        "distortion": context.user_data.get('td_distortion', ''),
        "reframe": context.user_data.get('td_reframe', ''),
    }
    db_save_entry(user_id, entry)

    summary = (
        "✓︎ *Запись сохранена*\n\n"
        f"♒︎ Исходная мысль: _{entry['thought'][:100]}_\n"
        f"ヅ︎ Альтернатива: _{entry['reframe'][:150]}_\n\n"
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
            f"Альтернатива: _{entry.get('reframe', '')[:80]}_\n\n"
        )

    await query.message.reply_text(text, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
    return MAIN_MENU

# === VOICE ===

async def transcribe_voice(voice_file) -> str:
    with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tmp:
        tmp_path = tmp.name
        await voice_file.download_to_drive(tmp_path)
    try:
        with open(tmp_path, 'rb') as audio:
            transcription = groq_client.audio.transcriptions.create(
                file=("voice.ogg", audio, "audio/ogg"),
                model="whisper-large-v3-turbo",
                language="ru",
            )
        return transcription.text
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
            reply = get_ai_response(user_id, text, mode='socratic', context_data=context_data)
        else:
            reply = get_ai_response(user_id, text, mode='chat', context_data={})

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

    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    application.add_handler(conv_handler)
    print("🤖 GoNeuralShift запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
