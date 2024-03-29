import logging
import asyncio
import os
from zipfile import ZipFile
import fitz  # PyMuPDF
from datetime import datetime
import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

bot_token = 'YOUR_BOT_TOKEN'  # Замените на ваш токен бота
bot = Bot(token=bot_token)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

processed_zips = 0


class Form(StatesGroup):
    waiting_for_print_size = State()


def size_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("A4", callback_data="A4"))
    keyboard.add(InlineKeyboardButton("Термопринтер", callback_data="Термопринтер"))
    return keyboard


# Настройка базы данных
async def init_db():
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS user_stats 
                            (user_id INTEGER PRIMARY KEY, username TEXT, processed_archives INTEGER DEFAULT 0)''')
        await db.commit()


# Асинхронная функция для обновления статистики пользователя
async def update_user_stats(user_id, username, archives_count):
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('INSERT OR IGNORE INTO user_stats (user_id, username) VALUES (?, ?)', (user_id, username))
        await db.execute('UPDATE user_stats SET processed_archives = processed_archives + ?, '
                         'username = ? WHERE user_id = ?', (archives_count, username, user_id))
        await db.commit()


# Функция для обработки команды /start
@dp.message_handler(Command("start"))
async def start(message: types.Message):
    await message.reply("Здравствуйте! Могли бы вы, пожалуйста, прислать мне zip-файл, который вы скачали с Kaspi?")


# Функция для обработки загруженных zip-файлов
@dp.message_handler(content_types=types.ContentType.DOCUMENT, state='*')
async def handle_zip_file(message: types.Message, state: FSMContext):
    global processed_zips
    file = message.document
    if not file.file_name.endswith('.zip'):
        await message.reply("❌ Ошибка! Пожалуйста, отправьте zip-файл.")
        return

    # Проверка размера файла
    if file.file_size > 20 * 1024 * 1024:  # 20 МБ
        await message.reply(
            "❌ Ошибка! Максимальный размер файла, который бот может скачать через Telegram API, составляет 20 МБ.")
        return

    try:
        file_info = await bot.get_file(file.file_id)
        file_path = file_info.file_path
        file_local_path = f"downloads/{file.file_name}"
        if not os.path.exists('downloads'):
            os.makedirs('downloads')

        progress_emojis = ["⬜"] * 4
        progress_percentage = [0, 25, 50, 75, 100]
        progress_message = await message.reply(f"Прогресс: {progress_emojis[0]} 0%")
        await state.update_data(progress_emojis=progress_emojis, progress_message=progress_message.message_id)

        # Скачивание файла
        await bot.download_file(file_path, file_local_path)

        # Проверка, что скачанный файл не пустой и его размер больше 1 килобайта
        if os.path.getsize(file_local_path) < 1024:  # Меньше 1 килобайта
            await message.reply("❌ Ошибка! Присланный zip-файл пуст или слишком мал.")
            progress_emojis[0] = "❌"
            await progress_message.edit_text(f"Прогресс: {progress_emojis[0]} {progress_percentage[0]}%")
            return

        progress_emojis[0] = "✅"
        await progress_message.edit_text(f"Прогресс: {progress_emojis[0]} {progress_percentage[1]}%")

        # Извлечение файлов
        pdf_files = extract_zip(file_local_path)
        progress_emojis[1] = "✅"
        await progress_message.edit_text(f"Прогресс: {' '.join(progress_emojis[:2])} {progress_percentage[2]}%")

        await state.update_data(pdf_files=pdf_files, file_path=file_local_path)
        await Form.waiting_for_print_size.set()
        await message.reply("Выберите размер: A4 или термопринтер 75x120", reply_markup=size_keyboard())

    except Exception as e:
        logging.error(f"Ошибка при обработке файла: {e}")
        await message.reply("❌ Произошла ошибка при обработке вашего файла.")


# Функция для обработки выбора размера печати
@dp.callback_query_handler(lambda c: c.data in ["A4", "Термопринтер"], state=Form.waiting_for_print_size)
async def process_size_selection(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    selected_size = callback_query.data
    await state.update_data(print_size=selected_size)
    user_data = await state.get_data()
    pdf_files = user_data['pdf_files']
    file_local_path = user_data['file_path']
    progress_emojis = user_data.get('progress_emojis', ["⬜", "⬜", "⬜", "⬜"])
    progress_message_id = user_data.get('progress_message')
    kg_count = 0  # Инициализация переменной

    # Скрыть кнопки после выбора
    await bot.edit_message_reply_markup(chat_id=callback_query.message.chat.id,
                                        message_id=callback_query.message.message_id)

    try:
        if selected_size == "A4":
            merged_pdf_file, bill_count, kg_count = merge_pdfs(pdf_files)
        else:  # Режим термопринтера
            output_pdf_writer = fitz.open()
            bill_count = 0  # Счетчик накладных
            kg_count = 0  # Счетчик килограммов

            for pdf_file in pdf_files:
                extract_and_scale_pdf_pages(pdf_file, output_pdf_writer, printer_size=(75, 120))
                with fitz.open(pdf_file) as input_pdf:
                    input_page = input_pdf[0]
                    if "" in input_page.get_text("text"):
                        bill_count += 1
                    kg_count += input_page.get_text("text").count('КГ')

            current_datetime = datetime.now().strftime("%d-%m-%Y")
            merged_pdf_file = f"thermal-zammler-{current_datetime}.pdf"
            output_pdf_writer.save(merged_pdf_file)
            output_pdf_writer.close()

        progress_emojis[2] = "✅"
        with open(merged_pdf_file, 'rb') as f:
            await bot.send_document(callback_query.message.chat.id, f)

        progress_emojis[3] = "✅"
        if progress_message_id:
            await bot.edit_message_text(chat_id=callback_query.message.chat.id, message_id=progress_message_id,
                                        text=f"Прогресс: {' '.join(progress_emojis)}\nОтправляем файл: 100%")

        await clean_up_files(file_local_path, pdf_files, merged_pdf_file, callback_query.from_user.id,
                             callback_query.from_user.full_name)
        await bot.send_message(callback_query.message.chat.id, f"\nОбработано накладных: {kg_count}")

        await send_info_message(callback_query.message)

    except Exception as e:
        logging.error(f"Ошибка при обработке размера печати: {e}")
        await bot.send_message(callback_query.message.chat.id, "❌ Произошла ошибка при обработке вашего запроса.")
