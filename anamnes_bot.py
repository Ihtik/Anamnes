import asyncio
import random
import json
import datetime
import os
import re
from collections import defaultdict
from functools import partial
from hashlib import sha256
from pathlib import Path
from time import monotonic
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ['BOT_TOKEN']
DATA_DIR = Path(os.environ.get('DATA_DIR') or '.')
QUOTES_FILE = DATA_DIR / 'quotes_anamnes.txt'
if not QUOTES_FILE.is_file():
    QUOTES_FILE = Path('quotes_anamnes.txt')
ADMIN_ID = 6944462724
DATA_FILE = DATA_DIR / 'users_data.json'
INTERVALS = {
    'hourly': 3600,
    'daily': 86400,
    '2days': 2 * 86400,
    'weekly': 7 * 86400,
}
FREQUENCY_LABELS = {
    'hourly': 'каждый час',
    'daily': 'каждый день',
    '2days': 'раз в 2 дня',
    'weekly': 'раз в неделю',
    'off': 'отключена',
}
UTC = datetime.timezone.utc
DEFAULT_SEND_OFFSET = "+05:00"
SEND_TIME_PROMPT = "Ответьте временем в формате ЧЧ:ММ, например 18:00 или 15:34."
FREQUENCY_TIME_PROMPTS = {
    "hourly": (
        "Каждый час. Ответьте минутой от 00 до 59, например 34. Можно также написать 15:34."
    ),
    "daily": f"Каждый день. {SEND_TIME_PROMPT}",
    "2days": f"Раз в 2 дня. {SEND_TIME_PROMPT}",
    "weekly": f"Раз в неделю. {SEND_TIME_PROMPT}",
}
FREQUENCY_BY_TIME_PROMPT = {prompt: frequency for frequency, prompt in FREQUENCY_TIME_PROMPTS.items()}
FREQUENCY_BY_TIME_PROMPT[SEND_TIME_PROMPT] = None
FEEDBACK_PROMPT = "Напишите автору текстовое сообщение."
FEEDBACK_REPLY_PREFIX = "Напишите ответ подписчику.\nНомер обращения: "
FEEDBACK_CALLBACK_PREFIX = "feedback_reply:"
FEEDBACK_ROUTE_PATTERN = re.compile(r"([1-9][0-9]{0,15}):([1-9][0-9]{0,9})")
FEEDBACK_DRAFTS = {}
FEEDBACK_DRAFT_LIFETIME = 10 * 60
# -------------------------------------

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- РАБОТА С ДАННЫМИ ---

def load_data():
    """Загружает настройки, сохраняя исходный файл при ошибке чтения."""
    try:
        with DATA_FILE.open(encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or any(not isinstance(user, dict) for user in data.values()):
        raise ValueError(f"Некорректный формат данных пользователей в {DATA_FILE}")
    return data


def save_data(data):
    """Атомарно заменяет предыдущую версию настроек."""
    temp_file = DATA_FILE.with_suffix(".json.tmp")
    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    temp_file.replace(DATA_FILE)

# Загружаем данные при старте
user_settings = load_data()

# --- ЗАГРУЗКА ЦИТАТ ---

def load_quotes():
    with QUOTES_FILE.open(encoding="utf-8-sig") as file:
        quotes = [line.strip() for line in file if line.strip()]
    if not quotes:
        raise ValueError(f"В {QUOTES_FILE} нет отрывков")
    return quotes

QUOTES = load_quotes()
QUOTES_BY_ID = {sha256(quote.encode("utf-8")).hexdigest(): quote for quote in QUOTES}
QUOTE_LOCKS = defaultdict(asyncio.Lock)


def remaining_quote_ids(settings):
    seen = set(settings.get("seen_quote_ids", []))
    return [quote_id for quote_id in QUOTES_BY_ID if quote_id not in seen]

def get_subscription_counts():
    by_frequency = dict.fromkeys(INTERVALS, 0)
    off = unreachable = 0
    for settings in user_settings.values():
        frequency = settings.get("frequency")
        if frequency not in by_frequency:
            off += 1
        elif settings.get("telegram_unreachable"):
            unreachable += 1
        else:
            by_frequency[frequency] += 1
    return by_frequency, off, unreachable


def get_user_stats_summary():
    subscribers, off, unreachable = get_subscription_counts()
    sent = sum(settings.get("quotes_sent", 0) for settings in user_settings.values())
    frequency_lines = [
        f"  • {FREQUENCY_LABELS[frequency]}: {count}"
        for frequency, count in subscribers.items() if count
    ]
    lines = [
        "📊 Статистика подписчиков",
        "",
        f"Подписчиков: {sum(subscribers.values())}",
        f"Рассылка выключена: {off}",
        f"Рассылка включена, но бот недоступен: {unreachable}",
        f"Всего пользователей в базе: {len(user_settings)}",
        f"Всего отправлено отрывков: {sent}",
    ]
    if frequency_lines:
        lines.extend(["", "Частота подписок:", *frequency_lines])
    return "\n".join(lines)


# --- ОБРАБОТЧИКИ КОМАНД ---

def set_next_regular_delivery(settings, now):
    send_time = settings.get("send_time")
    frequency = settings.get("frequency")
    if send_time and frequency in INTERVALS:
        clock_time = datetime.time.fromisoformat(send_time)
        local_now = now.astimezone(clock_time.tzinfo)
        if frequency == "hourly":
            due_at = local_now.replace(minute=clock_time.minute, second=0, microsecond=0)
            step = datetime.timedelta(hours=1)
        else:
            due_at = datetime.datetime.combine(local_now.date(), clock_time)
            step = datetime.timedelta(days=1)
        if due_at <= local_now:
            due_at += step
        settings["next_regular_at"] = due_at.astimezone(UTC).isoformat()
    else:
        settings.pop("next_regular_at", None)


async def send_round_complete(send, settings):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать новый круг", callback_data="restart_round")]
    ])
    await send(
        "Вы получили весь роман целиком. Чтобы начать получать отрывки заново, нажмите кнопку ниже.",
        reply_markup=keyboard,
    )
    if not settings.get("round_notice_sent"):
        settings["round_notice_sent"] = True
        save_data(user_settings)


async def deliver_quote(send, settings, remaining, *, scheduled=False, private=True):
    quote_id = random.choice(remaining)
    keyboard = None
    if len(remaining) > 1:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Следующий отрывок", callback_data="next_quote")]
        ])
    scheduled_override = settings.get("next_quote_at")
    scheduled_regular = settings.get("next_regular_at")
    interval = INTERVALS.get(settings.get("frequency"))
    await send(QUOTES_BY_ID[quote_id], reply_markup=keyboard)
    settings.setdefault("seen_quote_ids", []).append(quote_id)
    settings["quotes_sent"] = settings.get("quotes_sent", 0) + 1
    now = datetime.datetime.now().isoformat()
    if scheduled:
        settings["last_sent"] = now
        if scheduled_override == settings.get("next_quote_at"):
            settings.pop("next_quote_at", None)
        if scheduled_regular and interval and scheduled_regular == settings.get("next_regular_at"):
            due_at = datetime.datetime.fromisoformat(scheduled_regular)
            utc_now = datetime.datetime.now(UTC)
            if due_at <= utc_now:
                step = datetime.timedelta(seconds=interval)
                missed_steps = (utc_now - due_at) // step
                settings["next_regular_at"] = (due_at + (missed_steps + 1) * step).isoformat()
    else:
        settings["last_activity"] = now
        if private:
            settings.pop("telegram_unreachable", None)
    if len(remaining) == 1 and scheduled_override == settings.get("next_quote_at"):
        settings.pop("next_quote_at", None)
    save_data(user_settings)
    if len(remaining) == 1:
        await send_round_complete(send, settings)


async def send_quote(message: types.Message, user_id: str):
    async with QUOTE_LOCKS[user_id]:
        settings = user_settings.setdefault(user_id, {})
        remaining = remaining_quote_ids(settings)
        if not remaining:
            await send_round_complete(message.answer, settings)
            return
        await deliver_quote(message.answer, settings, remaining, private=message.chat.type == "private")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = str(message.from_user.id)
    now = datetime.datetime.now().isoformat()
    
    settings = user_settings.setdefault(user_id, {})
    if 'frequency' not in settings:
        # Новый пользователь
        settings.update({
            'first_seen': now,
            'last_activity': now,
            'frequency': 'daily',
            # Отсчёт до первой рассылки начинается с приветственного отрывка.
            'last_sent': now,
            'quotes_sent': settings.get('quotes_sent', 0),
            'username': message.from_user.username,
            'first_name': message.from_user.first_name,
            'last_name': message.from_user.last_name,
            'language_code': message.from_user.language_code
        })
        save_data(user_settings)
        print(f"🆕 Новый подписчик: {message.from_user.first_name} (@{message.from_user.username})")
    else:
        # Обновляем активность
        user_settings[user_id]['last_activity'] = now
        user_settings[user_id]['username'] = message.from_user.username
        save_data(user_settings)
    
    await message.answer(
        "«Анамнез» — книга Якупа Шапиро. Я присылаю из неё случайные отрывки.\n"
        "Настроить рассылку можно в /settings."
    )
    await send_quote(message, user_id)

@dp.message(Command("quote"))
async def cmd_quote(message: types.Message):
    await send_quote(message, str(message.from_user.id))

@dp.callback_query(lambda callback: callback.data == "next_quote")
async def process_next_quote(callback: CallbackQuery):
    await callback.answer()
    if isinstance(callback.message, types.Message):
        await send_quote(callback.message, str(callback.from_user.id))


@dp.callback_query(lambda callback: callback.data == "restart_round")
async def process_restart_round(callback: CallbackQuery):
    if not isinstance(callback.message, types.Message):
        await callback.answer()
        return
    user_id = str(callback.from_user.id)
    async with QUOTE_LOCKS[user_id]:
        settings = user_settings.get(user_id)
        if settings is None or remaining_quote_ids(settings):
            await callback.answer("Круг ещё не завершён.")
            return
        await callback.answer()
        settings["seen_quote_ids"] = []
        settings.pop("round_notice_sent", None)
        settings["last_sent"] = datetime.datetime.now().isoformat()
        set_next_regular_delivery(settings, datetime.datetime.now(UTC))
        save_data(user_settings)
        await deliver_quote(
            callback.message.answer, settings, list(QUOTES_BY_ID),
            private=callback.message.chat.type == "private",
        )


@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    user_id = str(message.from_user.id)
    if user_id in user_settings:
        user_settings[user_id]['last_activity'] = datetime.datetime.now().isoformat()
        save_data(user_settings)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Каждый час", callback_data="freq_hourly")],
        [InlineKeyboardButton(text="Каждый день", callback_data="freq_daily")],
        [InlineKeyboardButton(text="Раз в 2 дня", callback_data="freq_2days")],
        [InlineKeyboardButton(text="Раз в неделю", callback_data="freq_weekly")],
        [InlineKeyboardButton(text="Время рассылки", callback_data="schedule_regular")],
        [InlineKeyboardButton(text="Отписаться", callback_data="freq_off")]
    ])
    await message.answer("Настройте частоту и время рассылки:", reply_markup=keyboard)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        subscribers, _, _ = get_subscription_counts()
        await message.answer(f"Подписчиков: {sum(subscribers.values())}")
        return
    
    stats_text = get_user_stats_summary()
    await message.answer(stats_text)


def private_bot_prompt(message: types.Message):
    original = message.reply_to_message
    if (
        message.chat.type == "private"
        and original
        and original.from_user
        and original.from_user.id == bot.id
    ):
        return original.text or ""
    return ""


def pending_feedback(message: types.Message):
    if message.reply_to_message or not message.text or message.text.startswith("/"):
        return None
    draft = FEEDBACK_DRAFTS.get(message.chat.id)
    if draft and draft[0] <= monotonic():
        FEEDBACK_DRAFTS.pop(message.chat.id, None)
        return None
    return draft


def is_feedback_submission(message: types.Message):
    if message.chat.type != "private":
        return False
    if private_bot_prompt(message) == FEEDBACK_PROMPT:
        return True
    draft = pending_feedback(message)
    return draft is not None and draft[1] is None


@dp.message(Command("feedback"))
async def cmd_feedback(message: types.Message):
    if message.chat.type != "private":
        await message.answer("Написать автору можно в личном чате с ботом.")
        return
    FEEDBACK_DRAFTS[message.chat.id] = (monotonic() + FEEDBACK_DRAFT_LIFETIME, None)
    await message.answer(FEEDBACK_PROMPT, reply_markup=types.ForceReply())


@dp.message(is_feedback_submission)
async def process_feedback(message: types.Message):
    if not is_feedback_submission(message):
        return
    if not message.text:
        await cmd_feedback(message)
        return
    draft = FEEDBACK_DRAFTS.get(message.chat.id)
    if draft is not None and draft[1] is None:
        FEEDBACK_DRAFTS.pop(message.chat.id, None)
    route = f"{message.chat.id}:{message.message_id}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ответить", callback_data=FEEDBACK_CALLBACK_PREFIX + route)]
    ])
    text = f"Сообщение автору\n\n{message.text}"
    if len(text.encode("utf-16-le")) // 2 > 4096:
        text = message.text
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=keyboard)
    except TelegramAPIError as error:
        if draft is not None and draft[1] is None:
            FEEDBACK_DRAFTS.setdefault(message.chat.id, draft)
        print(f"Не удалось доставить обращение автору: {type(error).__name__}", flush=True)
        await message.answer("Не удалось отправить сообщение автору. Попробуйте позже.")
        return
    await message.answer("Сообщение отправлено автору.")


@dp.callback_query(lambda callback: (callback.data or "").startswith(FEEDBACK_CALLBACK_PREFIX))
async def process_feedback_reply_button(callback: CallbackQuery):
    message = callback.message
    if (
        callback.from_user.id != ADMIN_ID
        or not isinstance(message, types.Message)
        or message.chat.id != ADMIN_ID
    ):
        await callback.answer("Эта кнопка доступна автору.")
        return
    route = callback.data.removeprefix(FEEDBACK_CALLBACK_PREFIX)
    if not FEEDBACK_ROUTE_PATTERN.fullmatch(route):
        await callback.answer("Не удалось открыть обращение.")
        return
    await callback.answer()
    FEEDBACK_DRAFTS[ADMIN_ID] = (monotonic() + FEEDBACK_DRAFT_LIFETIME, route)
    await message.answer(
        FEEDBACK_REPLY_PREFIX + route,
        reply_markup=types.ForceReply(),
        reply_parameters=types.ReplyParameters(message_id=message.message_id),
    )


def author_reply_route(message: types.Message):
    if message.chat.id != ADMIN_ID or not message.from_user or message.from_user.id != ADMIN_ID:
        return None
    prompt = private_bot_prompt(message)
    if prompt.startswith(FEEDBACK_REPLY_PREFIX):
        return prompt.removeprefix(FEEDBACK_REPLY_PREFIX)
    draft = pending_feedback(message)
    return draft[1] if draft is not None else None


@dp.message(lambda message: author_reply_route(message) is not None)
async def process_author_reply(message: types.Message):
    route_text = author_reply_route(message)
    route = FEEDBACK_ROUTE_PATTERN.fullmatch(route_text or "")
    if not route:
        await message.answer("Не удалось найти обращение.")
        return
    if not message.text:
        await message.answer(
            FEEDBACK_REPLY_PREFIX + route_text,
            reply_markup=types.ForceReply(input_field_placeholder="Введите текст ответа"),
        )
        return
    draft = FEEDBACK_DRAFTS.get(ADMIN_ID)
    if draft is not None and draft[1] == route_text:
        FEEDBACK_DRAFTS.pop(ADMIN_ID, None)
    reader_id, original_message_id = map(int, route.groups())
    try:
        await bot.send_message(
            reader_id, message.text,
            reply_parameters=types.ReplyParameters(
                message_id=original_message_id, allow_sending_without_reply=True
            ),
        )
    except TelegramAPIError as error:
        if draft is not None and draft[1] == route_text:
            FEEDBACK_DRAFTS.setdefault(ADMIN_ID, draft)
        print(f"Не удалось доставить ответ подписчику: {type(error).__name__}", flush=True)
        await message.answer("Не удалось доставить ответ подписчику.")
        return
    await message.answer("Ответ отправлен подписчику.")


async def ask_send_time(message: types.Message, frequency=None):
    if message.chat.type != "private":
        await message.answer("Время рассылки можно настроить в личном чате с ботом.")
        return
    prompt = FREQUENCY_TIME_PROMPTS.get(frequency, SEND_TIME_PROMPT)
    placeholder = "34" if frequency == "hourly" else "18:00"
    await message.answer(prompt, reply_markup=types.ForceReply(input_field_placeholder=placeholder))


@dp.callback_query(lambda callback: callback.data == "schedule_regular")
async def process_schedule_regular(callback: CallbackQuery):
    await callback.answer()
    if isinstance(callback.message, types.Message):
        await ask_send_time(callback.message)


@dp.message(Command("time"))
async def cmd_time(message: types.Message):
    fields = message.text.split(maxsplit=1)
    if len(fields) == 1:
        await ask_send_time(message)
        return
    await store_regular_send_time(message, fields[1])


@dp.message(
    lambda message: message.text
    and message.reply_to_message
    and message.reply_to_message.from_user
    and message.reply_to_message.from_user.is_bot
    and message.reply_to_message.text
    and message.reply_to_message.text.partition("\n")[0] in FREQUENCY_BY_TIME_PROMPT
)
async def process_send_time_reply(message: types.Message):
    prompt = message.reply_to_message.text.partition("\n")[0]
    frequency = FREQUENCY_BY_TIME_PROMPT[prompt]
    await store_regular_send_time(message, message.text, frequency)


async def store_regular_send_time(message: types.Message, text: str, frequency=None):
    if message.chat.type != "private":
        await ask_send_time(message)
        return
    user_id = str(message.from_user.id)
    previous = user_settings.get(user_id, {})
    effective_frequency = frequency or previous.get("frequency")
    time_text = text.strip()
    minute = re.fullmatch(r"([0-5]?\d)(\s+[+-]\d{2}:[0-5]\d)?", time_text)
    if effective_frequency == "hourly" and minute:
        time_text = f"00:{int(minute[1]):02d}{minute[2] or ''}"
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)(?:\s+([+-]\d{2}:[0-5]\d))?", time_text)
    if not match:
        await ask_send_time(message, frequency)
        return
    stored_time = previous.get("send_time")
    offset = match[3] or (stored_time[5:] if stored_time else DEFAULT_SEND_OFFSET)
    try:
        clock_time = datetime.time.fromisoformat(f"{match[1]}:{match[2]}{offset}")
    except ValueError:
        await ask_send_time(message, frequency)
        return
    settings = user_settings.setdefault(user_id, {})
    if frequency:
        settings["frequency"] = frequency
    settings["send_time"] = clock_time.isoformat(timespec="minutes")
    settings["last_activity"] = datetime.datetime.now().isoformat()
    settings.pop("telegram_unreachable", None)
    set_next_regular_delivery(settings, datetime.datetime.now(UTC))
    save_data(user_settings)

    if effective_frequency == "hourly":
        response = f"Рассылка каждый час в {clock_time.minute:02d}-ю минуту (UTC{offset})."
    else:
        response = f"Время рассылки сохранено: {clock_time:%H:%M} (UTC{offset})."
    if "next_regular_at" in settings:
        next_regular = datetime.datetime.fromisoformat(settings["next_regular_at"]).astimezone(clock_time.tzinfo)
        response += f"\nБлижайшая отправка по этому расписанию: {next_regular:%d.%m.%Y в %H:%M}."
    else:
        response += "\nЧтобы включить рассылку, выберите частоту в /settings."
    await message.answer(response)


def next_quote_instructions():
    tomorrow = (datetime.datetime.now(UTC) + datetime.timedelta(days=1)).strftime("%d.%m.%Y")
    return (
        "Укажите дату, время и разницу с UTC. Например:\n"
        f"/next {tomorrow} 20:30 +05:00\n\n"
        "Чтобы отменить выбранное время, отправьте /next cancel."
    )


@dp.callback_query(lambda callback: callback.data == "schedule_next")
async def process_schedule_next(callback: CallbackQuery):
    await callback.answer()
    if isinstance(callback.message, types.Message):
        await callback.message.answer(next_quote_instructions())


@dp.message(Command("next"))
async def cmd_next(message: types.Message):
    if message.chat.type != "private":
        await message.answer("Выбрать время можно в личном чате с ботом.")
        return
    user_id = str(message.from_user.id)
    fields = message.text.split()[1:]
    settings = user_settings.get(user_id)
    if fields == ["cancel"]:
        if settings is not None and "next_quote_at" in settings:
            settings.pop("next_quote_at")
            save_data(user_settings)
        await message.answer("Выбранное время отменено.")
        return
    if len(fields) != 3:
        await message.answer(next_quote_instructions())
        return
    try:
        when = datetime.datetime.strptime(" ".join(fields), "%d.%m.%Y %H:%M %z")
    except ValueError:
        await message.answer(next_quote_instructions())
        return
    if when <= datetime.datetime.now(UTC):
        await message.answer("Выберите время в будущем.")
        return

    if settings is not None and not remaining_quote_ids(settings):
        await send_round_complete(message.answer, settings)
        return
    if settings is None:
        settings = {"frequency": "off", "last_sent": None, "quotes_sent": 0}
        user_settings[user_id] = settings
    settings["next_quote_at"] = when.astimezone(UTC).isoformat()
    settings["last_activity"] = datetime.datetime.now().isoformat()
    settings.pop("telegram_unreachable", None)
    save_data(user_settings)
    await message.answer(
        f"Следующий отрывок придёт {when:%d.%m.%Y} в {when:%H:%M} (UTC{fields[2]})."
    )


@dp.my_chat_member()
async def process_bot_membership(event: types.ChatMemberUpdated):
    if event.chat.type != "private":
        return
    settings = user_settings.get(str(event.chat.id))
    status = event.new_chat_member.status
    if settings is None or status not in ("kicked", "member"):
        return
    unreachable = status == "kicked"
    if unreachable != settings.get("telegram_unreachable", False):
        settings["telegram_unreachable"] = unreachable
        save_data(user_settings)


@dp.callback_query(lambda c: c.data and c.data.startswith("freq_"))
async def process_frequency_callback(callback_query: CallbackQuery):
    await callback_query.answer()
    message = callback_query.message
    if not isinstance(message, types.Message):
        return
    frequency = callback_query.data.removeprefix("freq_")
    if frequency in INTERVALS:
        await ask_send_time(message, frequency)
    elif frequency == "off":
        user_id = str(callback_query.from_user.id)
        settings = user_settings.setdefault(user_id, {})
        settings["frequency"] = "off"
        settings.pop("next_quote_at", None)
        settings.pop("next_regular_at", None)
        settings["last_activity"] = datetime.datetime.now().isoformat()
        save_data(user_settings)
        await message.edit_text("Рассылка отключена.")

# --- РАССЫЛКА ПО РАСПИСАНИЮ ---

async def scheduled_quote_sender():
    """Отправляет выбранный вручную или очередной отрывок по расписанию."""
    now = datetime.datetime.now()
    utc_now = datetime.datetime.now(UTC)
    for user_id, settings in tuple(user_settings.items()):
        async with QUOTE_LOCKS[user_id]:
            if settings.get("telegram_unreachable"):
                continue
            interval = INTERVALS.get(settings.get("frequency"))
            override = settings.get("next_quote_at")
            regular_at = settings.get("next_regular_at")
            if interval is None and not override:
                continue
            send = partial(bot.send_message, int(user_id))
            try:
                remaining = remaining_quote_ids(settings)
                if not remaining:
                    if not settings.get("round_notice_sent"):
                        await send_round_complete(send, settings)
                    continue
                if override:
                    if datetime.datetime.fromisoformat(override) > utc_now:
                        continue
                elif regular_at:
                    if datetime.datetime.fromisoformat(regular_at) > utc_now:
                        continue
                elif not interval_has_elapsed(settings.get("last_sent"), now, interval):
                    continue
                await deliver_quote(send, settings, remaining, scheduled=True)
            except TelegramForbiddenError:
                settings["telegram_unreachable"] = True
                save_data(user_settings)
            except Exception as error:
                print(f"❌ Не удалось отправить {user_id}: {error}")


def interval_has_elapsed(last_sent, now, interval):
    if not last_sent:
        return True
    try:
        return (now - datetime.datetime.fromisoformat(last_sent)).total_seconds() >= interval
    except (ValueError, TypeError):
        return True

# --- ЗАПУСК БОТА ---

def remove_contact_note(description):
    lines = description.splitlines()
    updated_lines = []
    skip_address = False
    for line in lines:
        if skip_address and re.fullmatch(r"(?:@|https?://t\.me/)[A-Za-z0-9_]+", line.strip()):
            skip_address = False
            continue
        skip_address = False
        note = re.search(r"(?:^|(?<=[.!?]))[^\w]*для[ \t]+обратной[ \t]+связи\b", line, re.IGNORECASE)
        if note is None:
            updated_lines.append(line)
            continue
        prefix = line[:note.start()].rstrip()
        if prefix:
            updated_lines.append(prefix)
        skip_address = not line[note.end():].strip(" \t:–—-")
    if lines == updated_lines:
        return description
    return "\n".join(updated_lines).strip()


async def update_telegram_descriptions():
    fields = (
        ("description", bot.get_my_description, bot.set_my_description),
        ("short_description", bot.get_my_short_description, bot.set_my_short_description),
    )
    verified_changes = 0
    for field, read_description, write_description in fields:
        for language_code in (None, "ru"):
            current = getattr(await read_description(language_code=language_code), field)
            updated = remove_contact_note(current)
            if updated == current:
                continue
            await write_description(**{field: updated}, language_code=language_code)
            confirmed = getattr(await read_description(language_code=language_code), field)
            if confirmed == updated:
                verified_changes += 1
    return verified_changes


async def register_bot_commands():
    managed_commands = (
        types.BotCommand(command="stats", description="Число подписчиков"),
        types.BotCommand(command="feedback", description="Написать автору"),
    )
    for language_code in (None, "ru"):
        commands = await bot.get_my_commands(language_code=language_code)
        if language_code and not commands:
            continue
        by_name = {command.command: command for command in commands}
        by_name.update((command.command, command) for command in managed_commands)
        updated_commands = list(by_name.values())
        if updated_commands != commands:
            await bot.set_my_commands(updated_commands, language_code=language_code)


async def main():
    try:
        profile_changes = await update_telegram_descriptions()
        print(f"📝 Telegram: подтверждено изменений описания — {profile_changes}", flush=True)
    except TelegramAPIError as error:
        print(f"❌ Не удалось обновить описание Telegram: {error}", flush=True)
    try:
        await register_bot_commands()
        print("📋 Меню команд Telegram: /stats — Число подписчиков, /feedback — Написать автору", flush=True)
    except TelegramAPIError as error:
        print(f"❌ Не удалось обновить меню команд Telegram: {error}", flush=True)
    scheduler = AsyncIOScheduler()
    first_tick = datetime.datetime.now(UTC).replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    scheduler.add_job(scheduled_quote_sender, IntervalTrigger(minutes=1, start_date=first_tick, timezone=UTC))
    scheduler.start()
    print("🚀 Планировщик запущен!")
    print(f"📖 Загружено отрывков: {len(QUOTES)}, уникальных: {len(QUOTES_BY_ID)} ({QUOTES_FILE})")
    subscribers, off, unreachable = get_subscription_counts()
    print(
        f"📊 Загружено пользователей: {len(user_settings)} ({DATA_FILE}). "
        f"Подписчиков: {sum(subscribers.values())}, выключили рассылку: {off}, "
        f"недоступны в Telegram: {unreachable}.",
        flush=True,
    )
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
