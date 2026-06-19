
import asyncio
import random
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage

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
        "Я присылаю случайные цитаты из книги. "
        "Используйте /settings, чтобы настроить частоту получения цитат.\n"
        "Или нажмите /quote, чтобы получить цитату прямо сейчас."
    )

@dp.message(Command("quote"))
async def cmd_quote(message: types.Message):
    """Отправляет случайную цитату по запросу."""
    await message.answer(f"✨ {get_random_quote()}")

@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    """Показывает меню настройки частоты."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Каждый день", callback_data="freq_daily")],
        [InlineKeyboardButton(text="Раз в 2 дня", callback_data="freq_2days")],
        [InlineKeyboardButton(text="Раз в неделю", callback_data="freq_weekly")],
        [InlineKeyboardButton(text="Отписаться", callback_data="freq_off")]
    ])
    await message.answer("Выберите, как часто вы хотите получать цитаты:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data and c.data.startswith("freq_"))
async def process_frequency_callback(callback_query: CallbackQuery):
    """Обрабатывает выбор частоты."""
    user_id = callback_query.from_user.id
    freq_map = {
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
        'daily': 'каждый день',
        '2days': 'раз в 2 дня',
        'weekly': 'раз в неделю',
        'off': 'отключена'
    }.get(new_freq, 'каждый день')
    
    await callback_query.answer()
    await callback_query.message.edit_text(f"✅ Настройка сохранена! Цитаты будут приходить: {freq_text}.")

# --- ФУНКЦИЯ ДЛЯ РАССЫЛКИ (ЕЁ ВЫЗЫВАЕТ ПЛАНИРОВЩИК) ---
async def scheduled_quote_sender():
    """Проверяет всех пользователей и отправляет цитаты по расписанию."""
    # Здесь должна быть логика проверки, кому и когда отправлять.
    # В этом примере для простоты мы просто отправляем всем, у кого frequency != 'off'.
    # Для реального проекта нужно добавить проверку даты последней отправки.
    for user_id, settings in user_settings.items():
        if settings.get('frequency') != 'off':
            try:
                await bot.send_message(user_id, f"📜 {get_random_quote()}")
                print(f"Цитата отправлена пользователю {user_id}")
                # Здесь нужно обновить user_settings[user_id]['last_sent'] = сегодня
            except Exception as e:
                print(f"Не удалось отправить сообщение {user_id}: {e}")

# --- ЗАПУСК БОТА ---
async def main():
    # Запускаем бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
