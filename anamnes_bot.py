import asyncio
import random
import json
import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = '8549796511:AAF6yKk3dd-gUW7JksFcyxm0xY9tNE69WVU'  # Вставьте ваш токен
QUOTES_FILE = 'quotes_anamnes.txt'     # Имя вашего файла с цитатами
ADMIN_ID = 6944462724                   # Ваш Chat ID для доступа к /stats
DATA_FILE = 'users_data.json'           # Файл для хранения статистики
# -------------------------------------

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- РАБОТА С ДАННЫМИ (СОХРАНЕНИЕ И ЗАГРУЗКА) ---

def load_data():
    """Загружает данные пользователей из JSON-файла"""
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_data(data):
    """Сохраняет данные пользователей в JSON-файл"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# Загружаем данные при старте
user_settings = load_data()

# --- ЗАГРУЗКА ЦИТАТ ---

def load_quotes():
    try:
        with open(QUOTES_FILE, 'r', encoding='utf-8') as f:
            quotes = [line.strip() for line in f if line.strip()]
        return quotes if quotes else ["Цитата не найдена. Добавьте цитаты в файл."]
    except FileNotFoundError:
        print(f"Ошибка: Файл {QUOTES_FILE} не найден!")
        return ["Цитата не найдена. Добавьте цитаты в файл."]

QUOTES = load_quotes()

def get_random_quote():
    return random.choice(QUOTES)

def get_user_stats_summary():
    """Собирает сводку по всем пользователям"""
    total = len(user_settings)
    if total == 0:
        return "📊 Пока нет подписчиков."
    
    now = datetime.datetime.now()
    active_today = 0
    active_week = 0
    freq_counts = {'hourly': 0, 'daily': 0, '2days': 0, 'weekly': 0, 'off': 0}
    total_quotes_sent = 0
    
    for uid, data in user_settings.items():
        # Активность за сегодня
        last_act = data.get('last_activity')
        if last_act:
            try:
                last_date = datetime.datetime.fromisoformat(last_act)
                if (now - last_date).total_seconds() < 86400:
                    active_today += 1
                if (now - last_date).total_seconds() < 86400 * 7:
                    active_week += 1
            except:
                pass
        
        # Частота
        freq = data.get('frequency', 'daily')
        if freq in freq_counts:
            freq_counts[freq] += 1
        
        # Всего отправленных цитат
        total_quotes_sent += data.get('quotes_sent', 0)
    
    freq_text = {
        'hourly': 'каждый час',
        'daily': 'каждый день',
        '2days': 'раз в 2 дня',
        'weekly': 'раз в неделю',
        'off': 'отключена'
    }
    freq_line = "\n".join([f"  • {freq_text.get(k, k)}: {v}" for k, v in freq_counts.items() if v > 0])
    
    return (
        f"📊 **Статистика подписчиков**\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"👥 **Всего:** {total}\n"
        f"🟢 **Активны сегодня:** {active_today}\n"
        f"🟡 **Активны за неделю:** {active_week}\n"
        f"📜 **Всего отправлено цитат:** {total_quotes_sent}\n"
        f"\n**Распределение по частоте:**\n{freq_line}"
    )

# --- ОБРАБОТЧИКИ КОМАНД ---

def log_activity(user_id, action, extra=None):
    """Записывает действие пользователя в его данные"""
    if str(user_id) not in user_settings:
        return
    user_settings[str(user_id)]['last_activity'] = datetime.datetime.now().isoformat()
    save_data(user_settings)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = str(message.from_user.id)
    now = datetime.datetime.now().isoformat()
    
    if user_id not in user_settings:
        # Новый пользователь
        user_settings[user_id] = {
            'first_seen': now,
            'last_activity': now,
            'frequency': 'daily',
            'last_sent': None,
            'quotes_sent': 0,
            'username': message.from_user.username,
            'first_name': message.from_user.first_name,
            'last_name': message.from_user.last_name,
            'language_code': message.from_user.language_code
        }
        save_data(user_settings)
        print(f"🆕 Новый подписчик: {message.from_user.first_name} (@{message.from_user.username})")
    else:
        # Обновляем активность
        user_settings[user_id]['last_activity'] = now
        user_settings[user_id]['username'] = message.from_user.username
        user_settings[user_id]['first_name'] = message.from_user.first_name
        save_data(user_settings)
    
    await message.answer(
        "Добро пожаловать! 📖\n"
        "Я присылаю случайные цитаты из книги Якупа Шапиро 'Анамнез'.\n"
        "Используйте /settings, чтобы настроить частоту получения цитат.\n"
        "Или нажмите /quote, чтобы получить цитату прямо сейчас."
    )

@dp.message(Command("quote"))
async def cmd_quote(message: types.Message):
    user_id = str(message.from_user.id)
    quote = get_random_quote()
    await message.answer(quote)
    
    # Логируем получение цитаты
    if user_id in user_settings:
        user_settings[user_id]['last_activity'] = datetime.datetime.now().isoformat()
        user_settings[user_id]['quotes_sent'] = user_settings[user_id].get('quotes_sent', 0) + 1
        save_data(user_settings)

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
        [InlineKeyboardButton(text="Отписаться", callback_data="freq_off")]
    ])
    await message.answer("Выберите, как часто вы хотите получать цитаты:", reply_markup=keyboard)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Отправляет статистику только администратору"""
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Эта команда доступна только администратору.")
        return
    
    stats_text = get_user_stats_summary()
    await message.answer(stats_text, parse_mode="Markdown")

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
    user_settings[user_id]['last_activity'] = datetime.datetime.now().isoformat()
    save_data(user_settings)

    freq_text = {
        'hourly': 'каждый час',
        'daily': 'каждый день',
        '2days': 'раз в 2 дня',
        'weekly': 'раз в неделю',
        'off': 'отключена'
    }.get(new_freq, 'каждый день')
    
    await callback_query.answer()
    await callback_query.message.edit_text(f"✅ Настройка сохранена! Цитаты будут приходить: {freq_text}.")

# --- РАССЫЛКА ПО РАСПИСАНИЮ ---

async def scheduled_quote_sender():
    """Проверяет всех пользователей и отправляет цитаты по расписанию."""
    now = datetime.datetime.now()
    for user_id, settings in user_settings.items():
        freq = settings.get('frequency')
        last_sent_str = settings.get('last_sent')
        last_sent = None
        if last_sent_str:
            try:
                last_sent = datetime.datetime.fromisoformat(last_sent_str)
            except:
                last_sent = None
        
        should_send = False
        if freq == 'hourly':
            if last_sent is None or (now - last_sent).total_seconds() >= 3600:
                should_send = True
        elif freq == 'daily':
            if last_sent is None or (now - last_sent).total_seconds() >= 86400:
                should_send = True
        elif freq == '2days':
            if last_sent is None or (now - last_sent).total_seconds() >= 86400 * 2:
                should_send = True
        elif freq == 'weekly':
            if last_sent is None or (now - last_sent).total_seconds() >= 86400 * 7:
                should_send = True
        elif freq == 'off':
            should_send = False
        
        if should_send:
            try:
                await bot.send_message(int(user_id), get_random_quote())
                user_settings[user_id]['last_sent'] = now.isoformat()
                user_settings[user_id]['last_activity'] = now.isoformat()
                user_settings[user_id]['quotes_sent'] = settings.get('quotes_sent', 0) + 1
                save_data(user_settings)
                print(f"✅ Цитата отправлена пользователю {user_id}")
            except Exception as e:
                print(f"❌ Не удалось отправить {user_id}: {e}")

# --- ЗАПУСК БОТА ---

async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_quote_sender, IntervalTrigger(minutes=1))
    scheduler.start()
    print("🚀 Планировщик запущен!")
    print(f"📊 Загружено пользователей: {len(user_settings)}")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())