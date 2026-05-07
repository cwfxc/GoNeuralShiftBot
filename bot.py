import os
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TOKEN_HERE")

# Conversation states
(MAIN_MENU, CHOOSING_TECHNIQUE, 
 ABC_SITUATION, ABC_BELIEF, ABC_CONSEQUENCE, ABC_DISPUTE, ABC_EFFECT,
 THOUGHT_DIARY_SITUATION, THOUGHT_DIARY_EMOTION, THOUGHT_DIARY_THOUGHT, 
 THOUGHT_DIARY_DISTORTION, THOUGHT_DIARY_REFRAME,
 SOCRATIC_START, SOCRATIC_QUESTION, SOCRATIC_ANSWER,
 BEHAVIORAL_GOAL, BEHAVIORAL_OBSTACLE, BEHAVIORAL_PLAN,
 DEFUSION_THOUGHT, DEFUSION_TECHNIQUE,
 FREE_CHAT) = range(21)

# CBT data
COGNITIVE_DISTORTIONS = {
    "🔮 Чтение мыслей": "Вы убеждены, что знаете, что думают другие, без достаточных оснований.",
    "🌑 Катастрофизация": "Вы ожидаете худшего исхода и убеждены, что он неизбежен.",
    "🏷 Навешивание ярлыков": "Вы используете глобальный ярлык вместо описания конкретного поведения.",
    "🔬 Сверхобобщение": "Вы делаете широкий вывод на основе единичного случая.",
    "👁 Фильтрация": "Вы фокусируетесь только на негативных деталях, игнорируя позитивные.",
    "⚫ Чёрно-белое мышление": "Вы видите всё в крайностях, без полутонов.",
    "⚡ Долженствование": "Вы используете жёсткие правила ('должен', 'обязан', 'необходимо').",
    "🔗 Персонализация": "Вы берёте на себя ответственность за вещи, которые не в вашей власти.",
    "📉 Обесценивание": "Вы отвергаете позитивный опыт, считая его незначительным.",
    "💭 Эмоциональное мышление": "Вы верите, что что-то истинно только потому, что так чувствуете.",
}

SОКРАТIC_QUESTIONS = [
    "Какие доказательства подтверждают эту мысль?",
    "Какие доказательства опровергают эту мысль?",
    "Что бы вы сказали другу с такой же мыслью?",
    "Какова вероятность того, что ваш прогноз сбудется?",
    "Даже если это произойдёт — насколько это будет катастрофично через год?",
    "Есть ли другое объяснение этой ситуации?",
    "Что самое плохое может случиться? Сможете ли вы с этим справиться?",
]

DEFUSION_TECHNIQUES = {
    "🍃 Листья на воде": (
        "Представьте, что вы сидите у спокойного ручья. Листья медленно плывут по воде.\n\n"
        "Возьмите свою мысль и поместите её на лист.\n"
        "Наблюдайте, как лист медленно уплывает вниз по течению.\n\n"
        "Вы не боретесь с водой. Вы не гонитесь за листом.\n"
        "Вы просто наблюдаете.\n\n"
        "Мысль — это просто мысль. Она приходит и уходит."
    ),
    "📻 Радиостанция": (
        "Представьте, что ваши мысли — это радиостанция, которая играет в фоне.\n\n"
        "Вы можете слышать её. Но вы не обязаны верить каждому слову.\n"
        "Радио может говорить 'всё плохо' — а вы можете продолжать жить.\n\n"
        "Назовите вашу станцию: например, 'Радио тревоги' или 'FM Катастроф'.\n"
        "Это просто передача. Вы можете её слушать или переключить внимание."
    ),
    "🎭 Персонаж": (
        "Добавьте к мысли фразу:\n\n"
        "*'У меня есть мысль о том, что...'*\n\n"
        "Например: вместо 'Я неудачник' → 'У меня есть мысль о том, что я неудачник'.\n\n"
        "Заметьте разницу? Мысль стала отдельной от вас.\n"
        "Вы — не ваши мысли. Вы — тот, кто их наблюдает."
    ),
}

# User data storage (in production - use database)
user_sessions = {}

def get_user_data(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "thought_diary": [],
            "current_session": {},
            "sessions_count": 0,
        }
    return user_sessions[user_id]

def save_diary_entry(user_id, entry):
    data = get_user_data(user_id)
    entry["date"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    data["thought_diary"].append(entry)

# Keyboards
def main_keyboard():
    keyboard = [
        [KeyboardButton("📓 Дневник мыслей"), KeyboardButton("🔄 ABC-анализ")],
        [KeyboardButton("🧠 Сократовский диалог"), KeyboardButton("🌊 Дефузия")],
        [KeyboardButton("🎯 Поведенческая активация"), KeyboardButton("📊 Мой прогресс")],
        [KeyboardButton("ℹ️ О методиках"), KeyboardButton("🆘 Кризисная помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def distortions_keyboard():
    buttons = []
    for name in COGNITIVE_DISTORTIONS.keys():
        buttons.append([InlineKeyboardButton(name, callback_data=f"dist_{name}")])
    buttons.append([InlineKeyboardButton("❓ Не знаю / Пропустить", callback_data="dist_skip")])
    return InlineKeyboardMarkup(buttons)

def defusion_keyboard():
    buttons = []
    for name in DEFUSION_TECHNIQUES.keys():
        buttons.append([InlineKeyboardButton(name, callback_data=f"defusion_{name}")])
    return InlineKeyboardMarkup(buttons)

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user_data(user.id)
    data["sessions_count"] += 1
    
    welcome = (
        f"Привет, {user.first_name} 👋\n\n"
        "Я — ваш КПТ-ассистент. Помогаю работать с деструктивными мыслями "
        "с помощью доказанных методик когнитивно-поведенческой терапии.\n\n"
        "⚠️ *Важно:* Я не замена профессиональному психотерапевту. "
        "При серьёзных симптомах обратитесь к специалисту.\n\n"
        "Выберите, с чего хотите начать:"
    )
    await update.message.reply_text(welcome, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    context.user_data.clear()

    if text == "📓 Дневник мыслей":
        await update.message.reply_text(
            "📓 *Дневник мыслей*\n\n"
            "Это один из ключевых инструментов КПТ. Мы запишем ситуацию, "
            "ваши мысли, эмоции и найдём более сбалансированный взгляд.\n\n"
            "Опишите ситуацию, которая вас беспокоит (что именно произошло?):",
            parse_mode='Markdown'
        )
        return THOUGHT_DIARY_SITUATION

    elif text == "🔄 ABC-анализ":
        await update.message.reply_text(
            "🔄 *ABC-анализ (модель Эллиса)*\n\n"
            "*A* — Activating event (ситуация-триггер)\n"
            "*B* — Beliefs (убеждения и мысли об этом)\n"
            "*C* — Consequences (эмоции и поведение)\n\n"
            "Мы разберём, как именно ваши мысли (B) влияют на то, что вы чувствуете (C), "
            "а не сама ситуация (A).\n\n"
            "Опишите ситуацию-триггер:",
            parse_mode='Markdown'
        )
        return ABC_SITUATION

    elif text == "🧠 Сократовский диалог":
        await update.message.reply_text(
            "🧠 *Сократовский диалог*\n\n"
            "Я буду задавать вопросы, которые помогут вам самостоятельно "
            "проверить обоснованность деструктивной мысли.\n\n"
            "Напишите мысль, которую хотите исследовать\n"
            "_(например: 'Я никогда не справлюсь', 'Все меня осуждают')_:",
            parse_mode='Markdown'
        )
        return SOCRATIC_START

    elif text == "🌊 Дефузия":
        await update.message.reply_text(
            "🌊 *Когнитивная дефузия* (из АКТ)\n\n"
            "Дефузия — это техника, которая помогает *дистанцироваться* от мыслей. "
            "Вместо того чтобы сливаться с мыслью и верить ей, вы учитесь наблюдать за ней.\n\n"
            "Напишите мысль, которая вас беспокоит:",
            parse_mode='Markdown'
        )
        return DEFUSION_THOUGHT

    elif text == "🎯 Поведенческая активация":
        await update.message.reply_text(
            "🎯 *Поведенческая активация*\n\n"
            "Депрессия и тревога часто заставляют нас избегать действий. "
            "Но именно действия восстанавливают ощущение жизни.\n\n"
            "Какую небольшую активность вы хотели бы добавить в свою жизнь? "
            "_(прогулка, звонок другу, хобби — что угодно малое и конкретное)_",
            parse_mode='Markdown'
        )
        return BEHAVIORAL_GOAL

    elif text == "📊 Мой прогресс":
        data = get_user_data(user_id)
        diary_count = len(data["thought_diary"])
        sessions = data["sessions_count"]
        
        progress_text = (
            f"📊 *Ваш прогресс*\n\n"
            f"• Сессий начато: {sessions}\n"
            f"• Записей в дневнике: {diary_count}\n\n"
        )
        
        if diary_count > 0:
            last = data["thought_diary"][-1]
            progress_text += f"*Последняя запись:* {last.get('date', '')}\n"
            progress_text += f"Ситуация: _{last.get('situation', '')[:80]}..._\n"
        
        if diary_count >= 5:
            progress_text += "\n🌟 Отличная работа! Регулярность — ключ к изменениям."
        elif diary_count >= 1:
            progress_text += "\n💪 Вы уже начали. Продолжайте — каждая запись имеет значение."
        else:
            progress_text += "\n🌱 Начните с дневника мыслей — это самый важный первый шаг."
        
        await update.message.reply_text(progress_text, parse_mode='Markdown', reply_markup=main_keyboard())
        return MAIN_MENU

    elif text == "ℹ️ О методиках":
        info = (
            "ℹ️ *О методиках*\n\n"
            "*Когнитивно-поведенческая терапия (КПТ)* — один из наиболее изученных методов "
            "психотерапии с доказанной эффективностью при тревоге, депрессии, ОКР и других состояниях.\n\n"
            "📓 *Дневник мыслей* — базовая техника КПТ. Помогает увидеть связь мысли→эмоция.\n\n"
            "🔄 *ABC-анализ* — модель Альберта Эллиса (РЭПТ). Показывает, что не события, "
            "а убеждения порождают страдание.\n\n"
            "🧠 *Сократовский диалог* — оспаривание мыслей через вопросы, а не прямые аргументы.\n\n"
            "🌊 *Дефузия* — из Терапии принятия и ответственности (АКТ). Учит наблюдать за мыслями, "
            "не сливаясь с ними.\n\n"
            "🎯 *Поведенческая активация* — техника при депрессии. Малые действия восстанавливают ресурс."
        )
        await update.message.reply_text(info, parse_mode='Markdown', reply_markup=main_keyboard())
        return MAIN_MENU

    elif text == "🆘 Кризисная помощь":
        crisis = (
            "🆘 *Кризисная помощь*\n\n"
            "Если вы в кризисе прямо сейчас — пожалуйста, обратитесь за помощью:\n\n"
            "🇷🇺 *Россия:*\n"
            "• Телефон доверия: *8-800-2000-122* (бесплатно)\n"
            "• Скорая помощь: *103*\n\n"
            "🌍 *Международная помощь:*\n"
            "• findahelpline.com — поиск линии помощи в вашей стране\n\n"
            "Если мысли о самоповреждении или суициде — это медицинская ситуация. "
            "Пожалуйста, позвоните на горячую линию или в экстренную службу.\n\n"
            "Я здесь, если хотите поговорить 🤍"
        )
        await update.message.reply_text(crisis, parse_mode='Markdown', reply_markup=main_keyboard())
        return MAIN_MENU

    return MAIN_MENU

# === THOUGHT DIARY ===
async def thought_diary_emotion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['td_situation'] = update.message.text
    await update.message.reply_text(
        "Хорошо. Теперь — *какие эмоции* вы испытывали? "
        "И насколько сильно (0-100%)?\n\n"
        "_(Например: тревога 70%, стыд 40%, злость 30%)_",
        parse_mode='Markdown'
    )
    return THOUGHT_DIARY_EMOTION

async def thought_diary_thought(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['td_emotion'] = update.message.text
    await update.message.reply_text(
        "Что именно промелькнуло в голове в тот момент?\n\n"
        "Попробуйте поймать *автоматическую мысль* — ту первую реакцию, "
        "что возникла до того, как вы начали анализировать.\n\n"
        "_(Например: 'Я опять всё испортил', 'Они точно думают плохо обо мне')_",
        parse_mode='Markdown'
    )
    return THOUGHT_DIARY_THOUGHT

async def thought_diary_distortion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['td_thought'] = update.message.text
    await update.message.reply_text(
        "Похоже ли это на одно из *когнитивных искажений*?\n\n"
        "Выберите, если узнаёте:",
        parse_mode='Markdown',
        reply_markup=distortions_keyboard()
    )
    return THOUGHT_DIARY_DISTORTION

async def handle_distortion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("dist_"):
        chosen = query.data[5:]
        
        if chosen == "skip":
            context.user_data['td_distortion'] = "Не определено"
            distortion_info = ""
        elif chosen in COGNITIVE_DISTORTIONS:
            context.user_data['td_distortion'] = chosen
            distortion_info = f"\n\n💡 *{chosen}*\n_{COGNITIVE_DISTORTIONS[chosen]}_"
        else:
            context.user_data['td_distortion'] = chosen
            distortion_info = ""
        
        await query.edit_message_text(
            f"Вы отметили: {context.user_data['td_distortion']}{distortion_info}\n\n"
            "Теперь давайте попробуем сформулировать *более сбалансированную мысль*.\n\n"
            "Спросите себя:\n"
            "• Какие факты за и против этой мысли?\n"
            "• Что бы я сказал другу в такой ситуации?\n"
            "• Есть ли другое объяснение?\n\n"
            "Напишите альтернативную, более реалистичную мысль:",
            parse_mode='Markdown'
        )
        return THOUGHT_DIARY_REFRAME

async def thought_diary_reframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['td_reframe'] = update.message.text
    user_id = update.effective_user.id
    
    # Save entry
    entry = {
        "situation": context.user_data.get('td_situation', ''),
        "emotion": context.user_data.get('td_emotion', ''),
        "thought": context.user_data.get('td_thought', ''),
        "distortion": context.user_data.get('td_distortion', ''),
        "reframe": context.user_data.get('td_reframe', ''),
    }
    save_diary_entry(user_id, entry)
    
    summary = (
        "✅ *Запись в дневник сохранена!*\n\n"
        f"📌 *Ситуация:* {entry['situation'][:100]}\n"
        f"💭 *Исходная мысль:* {entry['thought'][:100]}\n"
        f"🔍 *Искажение:* {entry['distortion']}\n"
        f"🌱 *Альтернативная мысль:* {entry['reframe'][:150]}\n\n"
        "Отлично поработали. Регулярная практика дневника мыслей — это и есть КПТ в действии.\n\n"
        "Хотите продолжить работу с другой техникой?"
    )
    await update.message.reply_text(summary, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

# === ABC ===
async def abc_belief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['abc_a'] = update.message.text
    await update.message.reply_text(
        "📝 Ситуация записана.\n\n"
        "Теперь — *B (Убеждения)*: что вы подумали об этой ситуации?\n\n"
        "Это могут быть интерпретации, оценки, предсказания.\n"
        "_(Например: 'Это значит, что я неудачник', 'Они специально так делают')_",
        parse_mode='Markdown'
    )
    return ABC_BELIEF

async def abc_consequence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['abc_b'] = update.message.text
    await update.message.reply_text(
        "Теперь *C (Последствия)*: что вы почувствовали и как себя повели?\n\n"
        "_(Например: расстроился, избегал контакта, грустил весь день)_",
        parse_mode='Markdown'
    )
    return ABC_CONSEQUENCE

async def abc_dispute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['abc_c'] = update.message.text
    b = context.user_data.get('abc_b', '')
    await update.message.reply_text(
        f"*D (Оспаривание убеждения)*\n\n"
        f"Вы написали: _\"{b}\"_\n\n"
        "Давайте оспорим это убеждение. Ответьте на один вопрос:\n\n"
        "🤔 *Это убеждение — факт или интерпретация?* Какие есть доказательства ЗА и ПРОТИВ него?",
        parse_mode='Markdown'
    )
    return ABC_DISPUTE

async def abc_effect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['abc_d'] = update.message.text
    await update.message.reply_text(
        "*E (Эффективное новое убеждение)*\n\n"
        "Отлично! Теперь сформулируйте более сбалансированное убеждение, "
        "которое учитывает всё, что вы только что разобрали.\n\n"
        "Оно должно быть *реалистичным*, не просто «позитивным».\n"
        "_(Например: 'Я сделал ошибку, это неприятно, но это не делает меня неудачником в целом')_",
        parse_mode='Markdown'
    )
    return ABC_EFFECT

async def abc_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['abc_e'] = update.message.text
    user_id = update.effective_user.id
    
    a = context.user_data.get('abc_a', '')
    b = context.user_data.get('abc_b', '')
    c = context.user_data.get('abc_c', '')
    d = context.user_data.get('abc_d', '')
    e = context.user_data.get('abc_e', '')
    
    entry = {"type": "ABC", "situation": a, "thought": b, "emotion": c, "reframe": e}
    save_diary_entry(user_id, entry)
    
    result = (
        "✅ *ABC-анализ завершён!*\n\n"
        f"*A* — _{a[:80]}_\n"
        f"*B* — _{b[:80]}_\n"
        f"*C* — _{c[:80]}_\n"
        f"*D* — _{d[:80]}_\n"
        f"*E* — _{e[:100]}_\n\n"
        "💡 Запомните: ситуация (A) сама по себе не вызывает ваши чувства. "
        "Это делают ваши убеждения (B). Изменив B — вы меняете C.\n\n"
        "Продолжайте работу:"
    )
    await update.message.reply_text(result, parse_mode='Markdown', reply_markup=main_keyboard())
    return MAIN_MENU

# === SOCRATIC DIALOGUE ===
async def socratic_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thought = update.message.text
    context.user_data['socratic_thought'] = thought
    context.user_data['socratic_q_index'] = 0
    
    first_q = SОКРАТIC_QUESTIONS[0]
    
    await update.message.reply_text(
        f"Работаем с мыслью:\n_\"{thought}\"_\n\n"
        f"Вопрос 1 из {len(SОКРАТIC_QUESTIONS)}:\n\n"
        f"*{first_q}*",
        parse_mode='Markdown'
    )
    return SOCRATIC_QUESTION

async def socratic_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text
    idx = context.user_data.get('socratic_q_index', 0)
    
    if 'socratic_answers' not in context.user_data:
        context.user_data['socratic_answers'] = []
    context.user_data['socratic_answers'].append(answer)
    
    next_idx = idx + 1
    context.user_data['socratic_q_index'] = next_idx
    
    if next_idx < min(4, len(SОКРАТIC_QUESTIONS)):  # 4 questions max
        next_q = SОКРАТIC_QUESTIONS[next_idx]
        await update.message.reply_text(
            f"Хорошо. Двигаемся дальше.\n\n"
            f"Вопрос {next_idx + 1}:\n\n"
            f"*{next_q}*",
            parse_mode='Markdown'
        )
        return SOCRATIC_QUESTION
    else:
        # Finish
        thought = context.user_data.get('socratic_thought', '')
        answers = context.user_data.get('socratic_answers', [])
        
        await update.message.reply_text(
            f"🧠 *Сократовский диалог завершён*\n\n"
            f"Вы исследовали мысль: _\"{thought}\"_\n\n"
            "Через эти вопросы вы сами нашли аргументы и контраргументы.\n\n"
            "💬 *Как теперь выглядит эта мысль? Изменилась ли ваша уверенность в ней (0-100%)?*\n\n"
            "_(Напишите ваш ответ или просто нажмите «В меню»)_",
            parse_mode='Markdown',
            reply_markup=main_keyboard()
        )
        return MAIN_MENU

# === DEFUSION ===
async def defusion_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['defusion_thought'] = update.message.text
    await update.message.reply_text(
        f"Работаем с мыслью: _\"{update.message.text}\"_\n\n"
        "Выберите технику дефузии:",
        parse_mode='Markdown',
        reply_markup=defusion_keyboard()
    )
    return DEFUSION_TECHNIQUE

async def handle_defusion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("defusion_"):
        chosen = query.data[9:]
        thought = context.user_data.get('defusion_thought', 'ваша мысль')
        
        if chosen in DEFUSION_TECHNIQUES:
            technique_text = DEFUSION_TECHNIQUES[chosen]
            
            await query.edit_message_text(
                f"*{chosen}*\n\n"
                f"Ваша мысль: _\"{thought}\"_\n\n"
                f"{technique_text}\n\n"
                "---\n"
                "Побудьте с этим упражнением 1-2 минуты.\n"
                "Мысль по-прежнему есть — но вы уже не внутри неё.\n\n"
                "Как вы себя чувствуете после упражнения?",
                parse_mode='Markdown'
            )
            return MAIN_MENU

# === BEHAVIORAL ACTIVATION ===
async def behavioral_obstacle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['ba_goal'] = update.message.text
    await update.message.reply_text(
        f"Отличный выбор: *{update.message.text}*\n\n"
        "Что обычно мешает вам это делать? "
        "_(усталость, нет времени, не вижу смысла, тревога...)_",
        parse_mode='Markdown'
    )
    return BEHAVIORAL_OBSTACLE

async def behavioral_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['ba_obstacle'] = update.message.text
    goal = context.user_data.get('ba_goal', '')
    obstacle = update.message.text
    
    await update.message.reply_text(
        f"Помеха: _{obstacle}_\n\n"
        "Теперь составим *микроплан*.\n\n"
        "Ответьте на три вопроса в одном сообщении:\n"
        "1️⃣ *Когда конкретно* вы это сделаете? (день, время)\n"
        "2️⃣ *Где* это произойдёт?\n"
        "3️⃣ *Насколько маленьким* может быть первый шаг?\n\n"
        "_(Пример: «В среду в 18:00, в парке рядом с домом, просто выйду и пройду 10 минут»)_",
        parse_mode='Markdown'
    )
    return BEHAVIORAL_PLAN

async def behavioral_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['ba_plan'] = update.message.text
    goal = context.user_data.get('ba_goal', '')
    plan = update.message.text
    
    await update.message.reply_text(
        f"✅ *Ваш план поведенческой активации*\n\n"
        f"🎯 Активность: _{goal}_\n"
        f"📋 План: _{plan}_\n\n"
        "💡 *Совет:* Не ждите настроения — делайте. Настроение придёт после действия, не до.\n\n"
        "Если план выполнен — вернитесь и расскажите. Каждое маленькое действие меняет нейронные паттерны.\n\n"
        "Удачи! 🌱",
        parse_mode='Markdown',
        reply_markup=main_keyboard()
    )
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Возвращаемся в главное меню.",
        reply_markup=main_keyboard()
    )
    return MAIN_MENU

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выберите технику из меню ниже 👇",
        reply_markup=main_keyboard()
    )
    return MAIN_MENU

def main():
    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu)
            ],
            # Thought Diary
            THOUGHT_DIARY_SITUATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_emotion)],
            THOUGHT_DIARY_EMOTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_thought)],
            THOUGHT_DIARY_THOUGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_distortion)],
            THOUGHT_DIARY_DISTORTION: [CallbackQueryHandler(handle_distortion_callback, pattern='^dist_')],
            THOUGHT_DIARY_REFRAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, thought_diary_reframe)],
            # ABC
            ABC_SITUATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, abc_belief)],
            ABC_BELIEF: [MessageHandler(filters.TEXT & ~filters.COMMAND, abc_consequence)],
            ABC_CONSEQUENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, abc_dispute)],
            ABC_DISPUTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, abc_effect)],
            ABC_EFFECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, abc_finish)],
            # Socratic
            SOCRATIC_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, socratic_start)],
            SOCRATIC_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, socratic_question)],
            # Defusion
            DEFUSION_THOUGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, defusion_choose)],
            DEFUSION_TECHNIQUE: [CallbackQueryHandler(handle_defusion_callback, pattern='^defusion_')],
            # Behavioral
            BEHAVIORAL_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, behavioral_obstacle)],
            BEHAVIORAL_OBSTACLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, behavioral_plan)],
            BEHAVIORAL_PLAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, behavioral_finish)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('start', start),
            MessageHandler(filters.TEXT, unknown),
        ],
    )

    application.add_handler(conv_handler)
    
    print("🤖 КПТ-бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
