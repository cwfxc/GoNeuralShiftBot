import os
import logging
from datetime import datetime
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
    "🍃 Листья на воде": (
        "Представьте, что сидите у спокойного ручья. Листья медленно плывут по воде.\n\n"
        "Возьмите свою мысль и поместите её на лист. Наблюдайте, как он медленно уплывает.\n\n"
        "Вы не боретесь с водой. Вы просто наблюдаете.\n\n"
        "Мысль — это просто мысль. Она приходит и уходит."
    ),
    "📻 Радиостанция": (
        "Ваши мысли — это радиостанция, которая играет в фоне.\n\n"
        "Вы можете слышать её. Но вы не обязаны верить каждому слову.\n\n"
        "Назовите вашу станцию: например, 'Радио тревоги' или 'FM Катастроф'.\n"
        "Это просто передача. Вы можете её слушать — или переключить внимание."
    ),
    "🎭 Персонаж": (
        "Добавьте к мысли фразу:\n\n"
        "*«У меня есть мысль о том, что...»*\n\n"
        "Вместо «Я неудачник» → «У меня есть мысль о том, что я неудачник».\n\n"
        "Заметьте разницу? Мысль стала отдельной от вас.\n"
        "Вы — не ваши мысли. Вы — тот, кто их наблюдает."
    ),
}

user_sessions = {}

def get_user_data(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "history": [],
            "thought_diary": [],
            "sessions_count": 0,
            "name": "",
        }
    return user_sessions[user_id]

def save_diary_entry(user_id, entry):
    data = get_user_data(user_id)
    entry["date"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    data["thought_diary"].append(entry)

def get_ai_response(user_id, user_message, mode="chat", context_data=None):
    data = get_user_data(user_id)

    system_prompts = {
        "chat": """Ты — GoNeuralShift, тёплый и внимательный КПТ-ассистент. 
Ты помогаешь людям работать с деструктивными мыслями используя техники когнитивно-поведенческой терапии (КПТ) и терапии принятия и ответственности (АКТ).

Твои принципы:
- Говори тепло, по-человечески, без канцелярита
- Не давай советов пока не поймёшь ситуацию — сначала задавай уточняющие вопросы
- Используй сократовские вопросы чтобы человек сам приходил к выводам
- Замечай когнитивные искажения и мягко указывай на них
- Никогда не осуждай и не обесценивай переживания
- Если человек в кризисе — мягко направляй к профессиональной помощи
- Отвечай на русском языке
- Ответы короткие и по делу — не более 3-4 предложений за раз
- Ты не замена психотерапевту, помни об этом""",

        "diary_reframe": f"""Ты — КПТ-терапевт. Пользователь заполнил дневник мыслей:
Ситуация: {context_data.get('situation', '')}
Эмоции: {context_data.get('emotion', '')}
Автоматическая мысль: {context_data.get('thought', '')}
Когнитивное искажение: {context_data.get('distortion', '')}

Помоги пользователю сформулировать более сбалансированную альтернативную мысль.
Задай один точный сократовский вопрос который поможет увидеть ситуацию шире.
Отвечай тепло, по-русски, коротко.""",

        "socratic": f"""Ты — КПТ-терапевт ведущий сократовский диалог.
Мысль пользователя: {context_data.get('thought', '')}
История диалога уже есть в сообщениях.

Задавай по одному глубокому вопросу за раз. Не давай ответов — только вопросы.
Цель: помочь человеку самому обнаружить слабые места в деструктивной мысли.
По итогу (через 4-5 обменов) мягко подведи к более сбалансированному взгляду.
Отвечай по-русски, тепло и коротко.""",
    }

    system = system_prompts.get(mode, system_prompts["chat"])

    # Build messages with history
    messages = [{"role": "system", "content": system}]

    if mode == "chat":
        # Add conversation history (last 10 messages)
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

    # Save to history for chat mode
    if mode == "chat":
        data["history"].append({"role": "user", "content": user_message})
        data["history"].append({"role": "assistant", "content": reply})
        # Keep last 20 messages
        if len(data["history"]) > 20:
            data["history"] = data["history"][-20:]

    return reply

# Keyboards
def main_keyboard():
    keyboard = [
        [KeyboardButton("💬 Поговорить с ботом"), KeyboardButton("📓 Дневник мыслей")],
        [KeyboardButton("🧠 Сократовский диалог"), KeyboardButton("🌊 Дефузия")],
        [KeyboardButton("📊 Мой прогресс"), KeyboardButton("🆘 Кризисная помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def distortions_keyboard():
    buttons = []
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
    keyboard = [[KeyboardButton("🏠 Главное меню")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user_data(user.id)
    data["sessions_count"] += 1
    data["name"] = user.first_name

    welcome = (
        f"Привет, {user.first_name} 👋\n\n"
        "Я — GoNeuralShift, ассистент по работе с мыслями.\n\n"
        "Помогаю замечать деструктивные паттерны и перестраивать их — "
        "с помощью техник КПТ и АКТ.\n\n"
        "Можете просто написать что вас беспокоит — или выбрать технику из меню.\n\n"
        "⚠️ Я не замена психотерапевту. При серьёзных симптомах — к специалисту."
    )
    await update.message.reply_text(welcome, reply_markup=main_keyboard())
    return MAIN_MENU

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data.clear()

    if text == "💬 Поговорить с ботом":
        await update.message.reply_text(
            "Расскажите что происходит. Я здесь и слушаю 🤍\n\n"
            "_(Напишите «🏠 Главное меню» чтобы вернуться)_",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )
        return AI_CHAT

    elif text == "📓 Дневник мыслей":
        await update.message.reply_text(
            "📓 *Дневник мыслей*\n\n"
            "Запишем ситуацию, эмоции и мысль — и найдём более сбалансированный взгляд.\n\n"
            "Опишите ситуацию, которая вас беспокоит:",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )
        return THOUGHT_DIARY_SITUATION

    elif text == "🧠 Сократовский диалог":
        await update.message.reply_text(
            "🧠 *Сократовский диалог*\n\n"
            "Напишите мысль, которую хотите исследовать.\n"
            "_(Например: «Я никогда не справлюсь», «Все меня осуждают»)_",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )
        context.user_data['mode'] = 'socratic'
        return AI_CHAT

    elif text == "🌊 Дефузия":
        await update.message.reply_text(
            "🌊 *Когнитивная дефузия*\n\n"
            "Напишите мысль, которая вас беспокоит:",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )
        return DEFUSION_THOUGHT

    elif text == "📊 Мой прогресс":
        user_id = update.effective_user.id
        data = get_user_data(user_id)
        diary_count = len(data["thought_diary"])
        sessions = data["sessions_count"]

        text_progress = (
            f"📊 *Ваш прогресс*\n\n"
            f"• Сессий: {sessions}\n"
            f"• Записей в дневнике: {diary_count}\n\n"
        )
        if diary_count >= 5:
            text_progress += "🌟 Отличная работа! Регулярность — ключ к изменениям."
        elif diary_count >= 1:
            text_progress += "💪 Вы уже начали. Каждая запись имеет значение."
        else:
            text_progress += "🌱 Начните с дневника мыслей — это первый шаг."

        await update.message.reply_text(text_progress, parse_mode='Markdown', reply_markup=main_keyboard())
        return MAIN_MENU

    elif text == "🆘 Кризисная помощь":
        crisis = (
            "🆘 *Кризисная помощь*\n\n"
            "Если вы в кризисе прямо сейчас:\n\n"
            "🇷🇺 *Россия:* 8-800-2000-122 (бесплатно)\n"
            "🌍 *Международно:* findahelpline.com\n\n"
            "Если есть мысли о самоповреждении — это медицинская ситуация. "
            "Пожалуйста, позвоните на горячую линию.\n\n"
            "Я здесь, если хотите поговорить 🤍"
        )
        await update.message.reply_text(crisis, parse_mode='Markdown', reply_markup=main_keyboard())
        return MAIN_MENU

    elif text == "🏠 Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    else:
        # Unknown text in main menu — treat as chat
        await update.message.reply_text(
            "Выберите из меню или нажмите «💬 Поговорить с ботом» чтобы просто написать что беспокоит.",
            reply_markup=main_keyboard()
        )
        return MAIN_MENU

async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "🏠 Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU

    mode = context.user_data.get('mode', 'chat')
    socratic_thought = context.user_data.get('socratic_thought', '')

    # First message in socratic mode — save the thought
    if mode == 'socratic' and not socratic_thought:
        context.user_data['socratic_thought'] = text
        context.user_data['socratic_history'] = []

    await update.message.chat.send_action("typing")

    try:
        if mode == 'socratic':
            context_data = {'thought': context.user_data.get('socratic_thought', text)}
            reply = get_ai_response(user_id, text, mode='socratic', context_data=context_data)
        else:
            reply = get_ai_response(user_id, text, mode='chat')

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
    if update.message.text == "🏠 Главное меню":
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
    if update.message.text == "🏠 Главное меню":
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
    if update.message.text == "🏠 Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
        return MAIN_MENU
    context.user_data['td_thought'] = update.message.text
    await update.message.reply_text(
        "Похоже ли это на одно из *когнитивных искажений*?",
        parse_mode='Markdown',
        reply_markup=distortions_keyboard()
    )
    return THOUGHT_DIARY_DISTORTION

async def handle_distortion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chosen_key = query.data[5:]  # remove "dist_"

    if chosen_key == "skip":
        context.user_data['td_distortion'] = "Не определено"
        distortion_info = ""
    else:
        # Find full key by prefix
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

    # AI generates reframe question
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
    if update.message.text == "🏠 Главное меню":
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
    save_diary_entry(user_id, entry)

    summary = (
        "✅ *Запись сохранена*\n\n"
        f"💭 Исходная мысль: _{entry['thought'][:100]}_\n"
        f"🌱 Альтернатива: _{entry['reframe'][:150]}_\n\n"
        "Отличная работа. Каждая такая запись постепенно меняет нейронные паттерны."
    )
    await update.message.reply_text(summary, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

# === DEFUSION ===
async def defusion_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🏠 Главное меню":
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

    chosen_key = query.data[4:]  # remove "def_"
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

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())
    return MAIN_MENU

def main():
    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu)
            ],
            AI_CHAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ai_chat)
            ],
            THOUGHT_DIARY_SITUATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_emotion)],
            THOUGHT_DIARY_EMOTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_thought)],
            THOUGHT_DIARY_THOUGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_distortion)],
            THOUGHT_DIARY_DISTORTION: [CallbackQueryHandler(handle_distortion_callback, pattern='^dist_')],
            THOUGHT_DIARY_REFRAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_reframe)],
            DEFUSION_THOUGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, defusion_choose)],
            DEFUSION_TECHNIQUE: [CallbackQueryHandler(handle_defusion_callback, pattern='^def_')],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('start', start),
        ],
    )

    application.add_handler(conv_handler)
    print("🤖 GoNeuralShift запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
