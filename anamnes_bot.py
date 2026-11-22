import asyncio
import random
import json
import datetime
import os
from collections import defaultdict
from functools import partial
from hashlib import sha256
from pathlib import Path
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramForbiddenError
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
    await send(QUOTES_BY_ID[quote_id], reply_markup=keyboard)
    settings.setdefault("seen_quote_ids", []).append(quote_id)
    settings["quotes_sent"] = settings.get("quotes_sent", 0) + 1
    now = datetime.datetime.now().isoformat()
    if scheduled:
        settings["last_sent"] = now
        if scheduled_override == settings.get("next_quote_at"):
            settings.pop("next_quote_at", None)
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
        "Настроить рассылку можно в /settings. Число подписчиков — /stats."
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
        [InlineKeyboardButton(text="Время следующего отрывка", callback_data="schedule_next")],
        [InlineKeyboardButton(text="Отписаться", callback_data="freq_off")]
    ])
    await message.answer("Выберите, как часто вы хотите получать цитаты:", reply_markup=keyboard)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        subscribers, _, _ = get_subscription_counts()
        await message.answer(f"Подписчиков: {sum(subscribers.values())}")
        return
    
    stats_text = get_user_stats_summary()
    await message.answer(stats_text)

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
    user_id = str(callback_query.from_user.id)
    freq_map = {
        'freq_hourly': 'hourly',
        'freq_daily': 'daily',
        'freq_2days': '2days',
        'freq_weekly': 'weekly',
        'freq_off': 'off'
    }
    new_freq = freq_map.get(callback_query.data, 'daily')
    
    if user_id not in user_settings:
        user_settings[user_id] = {'frequency': 'daily', 'last_sent': None, 'quotes_sent': 0}
    user_settings[user_id]['frequency'] = new_freq
    if new_freq == 'off':
        user_settings[user_id].pop('next_quote_at', None)
    user_settings[user_id]['last_activity'] = datetime.datetime.now().isoformat()
    save_data(user_settings)

    await callback_query.answer()
    await callback_query.message.edit_text(
        f"✅ Настройка сохранена! Частота рассылки: {FREQUENCY_LABELS[new_freq]}."
    )

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

async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_quote_sender, IntervalTrigger(minutes=1))
    scheduler.start()
    print("🚀 Планировщик запущен!")
    print(f"📖 Загружено отрывков: {len(QUOTES)}, уникальных: {len(QUOTES_BY_ID)} ({QUOTES_FILE})")
    subscribers, off, unreachable = get_subscription_counts()
    print(
        f"📊 Загружено пользователей: {len(user_settings)} ({DATA_FILE}). "
        f"Подписчиков: {sum(subscribers.values())}, выключили рассылку: {off}, "
        f"недоступны в Telegram: {unreachable}."
    )
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
