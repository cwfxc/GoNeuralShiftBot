# -*- coding: utf-8 -*-
# рассылка утро/вечер + ротация без повторов

import random
import datetime
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

TZ = ZoneInfo("Europe/Moscow")  # таймзона старой рассылки бота

# окна отправки (час, мин, час, мин)
MORNING_WINDOW = (8, 30, 10, 0)
EVENING_WINDOW = (20, 30, 22, 0)


MORNING_MESSAGES = [
    "Какие решения приблизили бы тебя к самому себе?",
    "Какие качества твоей души стремятся проявиться сильнее?",
    "Если бы сегодняшний день прошёл в согласии с тем, что для тебя важно — как бы он выглядел?",
    "Что из происходящего сейчас под твоим контролем, а что — нет?",
    "Есть что-то, что ты откладываешь из страха, а не потому что это правда не нужно?",
    "Интересно, какое неожиданное везение ждёт меня сегодня?",
    "Я выбираю видеть свои шаги, а не чужие вершины.",
    "Каждый вдох приносит мне энергию и гармонию.",
    "Я выбираю здоровые привычки, которые укрепляют меня.",
    "Я выбираю свободу вместо страха.",
    "Я достоин(а) быть здоровым(ой) и чувствовать себя хорошо.",
    "Я открыт(а) для любви и с благодарностью принимаю её.",
    "Каждый день я укрепляю связи с теми, кто близок моему сердцу.",
    "Я могу доверять себе и своим чувствам.",
    "Я отпускаю тревогу и доверяю жизни.",
    "Я могу опираться на себя и позволять другим быть рядом.",
    "Тебе не обязательно чувствовать себя готовым, чтобы начать.",
    "Можно одновременно тревожиться и всё равно сделать шаг.",
    "Ты не обязан справляться со всем сразу. Достаточно следующего маленького шага.",
    "Ты уже сталкивался с трудным раньше. Это не гарантия на сегодня, но это факт о тебе.",
]

EVENING_MESSAGES = [
    "Что для тебя является проявлением любви к себе?",
    "Какие роли ты играешь чаще всего — и какие из них тебе всё ещё нужны?",
    "Какие ожидания окружающих до сих пор влияют на твои решения?",
    "В какие моменты у тебя появляется ощущение внутренней целостности?",
    "Кто ты без необходимости производить впечатление?",
    "Какие изменения в жизни заставляли тебя переосмыслить себя?",
    "Какая часть тебя просит свободы?",
    "Что жизнь пытается показать тебе через трудности?",
    "Какая мысль сегодня возвращалась чаще всего? Она тебе помогает или мешает?",
    "Что бы ты сказал другу, если бы он оказался в твоей сегодняшней ситуации?",
    "Какую мысль ты сегодня принял за факт, хотя это была просто мысль?",
    "Если убрать оценку «хорошо/плохо» — что просто есть в твоём дне сегодня?",
    "Я отпускаю потребность быть идеальным(ой).",
    "Каждый человек имеет право на свой путь и свои ошибки.",
    "Моя ценность не определяется чьим-то вниманием или любовью.",
    "Я больше не ищу одобрения — я одобряю себя сам(а).",
    "Прогресс редко выглядит как прямая линия — трудный день не отменяет то, что уже пройдено.",
    "Даже если сегодня кажется, что всё стоит на месте — что-то внутри всё равно меняется.",
    "Я в безопасности. Моё тело расслабляется. Мой ум спокоен.",
]


# состояние ротации по пользователям:
# {chat_id: {"morning": {"deck": [...], "last": ...}, "evening": {...}}}
_rotation_state = {}


def get_next_message(chat_id, slot):
    # тасуем список как колоду и раздаём по одному,
    # когда кончился - тасуем заново и идём по новому кругу
    source = MORNING_MESSAGES if slot == "morning" else EVENING_MESSAGES

    user_state = _rotation_state.setdefault(chat_id, {})
    state = user_state.setdefault(slot, {"deck": [], "last": None})

    if not state["deck"]:
        deck = source.copy()
        random.shuffle(deck)
        # чтобы на стыке кругов не пришло одно и то же два раза подряд
        if state["last"] is not None and deck[0] == state["last"] and len(deck) > 1:
            deck[0], deck[-1] = deck[-1], deck[0]
        state["deck"] = deck

    msg = state["deck"].pop(0)
    state["last"] = msg
    return msg


def _norm(text):
    """Нормализуем текст для устойчивого сравнения: убираем лишние пробелы,
    регистр и различие ё/е — чтобы мелкие правки формулировки не ломали исключение."""
    return " ".join(text.strip().casefold().replace("ё", "е").split())


# Вопросы, к которым НЕ нужно прикреплять кнопку «Ответить», хотя они оканчиваются на «?»
# (риторические / настрой на день, а не приглашение к рефлексии).
# Сравнение идёт по нормализованному тексту (см. _norm), поэтому пробелы/регистр/ё-е не важны.
# Если полностью поменяете формулировку вопроса — обновите строку и здесь; при рассинхроне
# бот НЕ сломается, но напечатает предупреждение в лог при старте (см. _check_no_answer_button).
_NO_ANSWER_BUTTON_SOURCE = [
    "Интересно, какое неожиданное везение ждёт меня сегодня?",
]
NO_ANSWER_BUTTON = {_norm(t) for t in _NO_ANSWER_BUTTON_SOURCE}


def _reply_markup_for(text):
    """Вопросы (текст оканчивается на «?») получают кнопку «Ответить»;
    аффирмации и вопросы из NO_ANSWER_BUTTON — без клавиатуры."""
    t = text.strip()
    if t.endswith("?") and _norm(t) not in NO_ANSWER_BUTTON:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("💬 Ответить", callback_data="reflect_answer")]]
        )
    return None


def _check_no_answer_button():
    """Сверяет список исключений с реальными сообщениями. Если какую-то формулировку
    изменили и забыли обновить исключение — печатает предупреждение (не роняя бот)."""
    all_norm = {_norm(m) for m in MORNING_MESSAGES + EVENING_MESSAGES}
    missing = [t for t in _NO_ANSWER_BUTTON_SOURCE if _norm(t) not in all_norm]
    for t in missing:
        print(f"WARNING: NO_ANSWER_BUTTON — фраза не найдена среди сообщений, "
              f"кнопка не будет скрыта: {t!r}")
    return missing


_check_no_answer_button()


def random_time_in_window(h1, m1, h2, m2):
    start = h1 * 60 + m1
    end = h2 * 60 + m2
    minute = random.randint(start, end)
    return datetime.time(minute // 60, minute % 60, tzinfo=TZ)


async def send_morning(context):
    chat_id = context.job.chat_id
    text = get_next_message(chat_id, "morning")
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=_reply_markup_for(text))
    schedule_daily_message(context.job_queue, chat_id, "morning")  # перепланируем на завтра


async def send_evening(context):
    chat_id = context.job.chat_id
    text = get_next_message(chat_id, "evening")
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=_reply_markup_for(text))
    schedule_daily_message(context.job_queue, chat_id, "evening")


def schedule_daily_message(job_queue, chat_id, slot):
    # run_once вместо run_daily, чтобы время каждый день было разным
    if slot == "morning":
        window, callback = MORNING_WINDOW, send_morning
    else:
        window, callback = EVENING_WINDOW, send_evening

    t = random_time_in_window(*window)
    now = datetime.datetime.now(TZ)
    target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)

    job_queue.run_once(callback, when=target, chat_id=chat_id, name=f"{slot}_{chat_id}")


def setup_user_schedule(job_queue, chat_id):
    # вызывать при /start, старые джобы убираем чтобы не задвоились
    for slot in ("morning", "evening"):
        for job in job_queue.get_jobs_by_name(f"{slot}_{chat_id}"):
            job.schedule_removal()
        schedule_daily_message(job_queue, chat_id, slot)
