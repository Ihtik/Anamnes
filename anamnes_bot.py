import asyncio
import random
import os
import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# --- КОНФИГУРАЦИЯ (ЗАМЕНИТЕ ТОКЕН!) ---
BOT_TOKEN = '8549796511:AAF6yKk3dd-gUW7JksFcyxm0xY9tNE69WVU'  # Вставьте ваш токен
QUOTES_FILE = 'quotes_anamnes.txt'     # Имя вашего файла с цитатами
# -------------------------------------

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Хранилище настроек пользователей (в реальном проекте используйте БД)
user_settings = {}

# Загрузка цитат из файла
def load_quotes():
    try:
        with open(QUOTES_FILE, 'r', encoding='utf-8') as f:
            quotes = [line.strip() for line in f if line.strip()]
        return quotes
    except FileNotFoundError:
        print(f"Ошибка: Файл {QUOTES_FILE} не найден!")
        return ["Цитата не найдена. Добавьте цитаты в файл."]

QUOTES = load_quotes()

def get_random_quote():
    return random.choice(QUOTES)

# --- ОБРАБОТЧИКИ КОМАНД ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    # Устанавливаем настройки по умолчанию (каждый день)
    user_settings[user_id] = {'frequency': 'daily', 'last_sent': None}
    await message.answer(
        "Добро пожаловать! 📖\n"
        "Я присылаю случайные цитаты из книги Якупа Шапиро "Анамнез".\n"
        "Используйте /settings, чтобы настроить частоту получения цитат.\n"
        "Или нажмите /quote, чтобы получить цитату прямо сейчас."
    )

@dp.message(Command("quote"))
async def cmd_quote(message: types.Message):
    await message.answer(f"✨ {get_random_quote()}")

@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Каждый час", callback_data="freq_hourly")],
        [InlineKeyboardButton(text="Каждый день", callback_data="freq_daily")],
        [InlineKeyboardButton(text="Раз в 2 дня", callback_data="freq_2days")],
        [InlineKeyboardButton(text="Раз в неделю", callback_data="freq_weekly")],
        [InlineKeyboardButton(text="Отписаться", callback_data="freq_off")]
    ])
    await message.answer("Выберите, как часто вы хотите получать цитаты:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data and c.data.startswith("freq_"))
async def process_frequency_callback(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    freq_map = {
        'freq_hourly': 'hourly',
        'freq_daily': 'daily',
        'freq_2days': '2days',
        'freq_weekly': 'weekly',
        'freq_off': 'off'
    }
    new_freq = freq_map.get(callback_query.data, 'daily')
    
    if user_id not in user_settings:
        user_settings[user_id] = {'frequency': 'daily', 'last_sent': None}
    user_settings[user_id]['frequency'] = new_freq

    freq_text = {
        'hourly': 'каждый час',
        'daily': 'каждый день',
        '2days': 'раз в 2 дня',
        'weekly': 'раз в неделю',
        'off': 'отключена'
    }.get(new_freq, 'каждый день')
    
    await callback_query.answer()
    await callback_query.message.edit_text(f"✅ Настройка сохранена! Цитаты будут приходить: {freq_text}.")

# --- ФУНКЦИЯ ДЛЯ РАССЫЛКИ (ВЫЗЫВАЕТСЯ ПЛАНИРОВЩИКОМ) ---
async def scheduled_quote_sender():
    """Проверяет всех пользователей и отправляет цитаты по расписанию."""
    now = datetime.datetime.now()
    for user_id, settings in user_settings.items():
        freq = settings.get('frequency')
        last_sent = settings.get('last_sent')
        
        # Проверяем, нужно ли отправить цитату прямо сейчас
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
                await bot.send_message(user_id, f"📜 {get_random_quote()}")
                user_settings[user_id]['last_sent'] = now
                print(f"Цитата отправлена пользователю {user_id}")
            except Exception as e:
                print(f"Не удалось отправить {user_id}: {e}")

# --- ЗАПУСК БОТА С ПЛАНИРОВЩИКОМ ---
async def main():
    # Настраиваем планировщик
    scheduler = AsyncIOScheduler()
    # Запускаем проверку каждую минуту
    scheduler.add_job(scheduled_quote_sender, IntervalTrigger(minutes=1))
    scheduler.start()
    print("Планировщик запущен!")
    
    # Запускаем бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())