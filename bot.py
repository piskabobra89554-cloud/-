import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, List

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ContentType
from aiogram.enums import ParseMode
from rapidfuzz import fuzz

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "ВАШ_ТОКЕН_ОТ_BOTFATHER"
BOT_USERNAME = "YourBotUsername"          # без символа @
CHANNEL_ID = -1001234567890               # ID канала (начинается с -100)
ADMIN_ID = 123456789                      # ваш Telegram ID
ADMIN_CHAT_ID = 123456789                 # личный чат с админом
TIMEOUT_MINUTES = 5
SIMILARITY_THRESHOLD = 85
MAX_TEXT_LENGTH = 3500
DB_PATH = "bot_data.db"

# ================== ЗАПРЕЩЁННЫЕ ПАТТЕРНЫ ==================
# Основано на Terms of Service Telegram (публичные каналы)
BANNED_PATTERNS = [
    # CSAM / детская эксплуатация
    r"\b(детск|child|minor|underage|pedo|педо|малолет|школьниц|девочк.{0,10}(секс|порн|nude))",
    r"\b(csam|cp\b|child porn)",
    # Насилие / терроризм / экстремизм
    r"\b(террор|terrorism|джихад|джихад|взрывчат|бомб|убить|убивать|расстрел|казнит)",
    r"\b(призыв.{0,15}(к насилию|к убийству|к войне)|promote violence)",
    # Наркотики
    r"\b(наркот|героин|кокаин|амфетамин|мефедрон|спайс|соль|закладк|клад|героин|mdma|lsd)",
    r"\b(drugs?|cocaine|heroin|meth|weed\b|cannabis)",
    # Оружие / незаконные товары
    r"\b(продам.{0,20}(оружие|пистолет|автомат|гранат|взрывчат)|оружие на заказ)",
    r"\b(firearms?|weapons?|gun\b|ammo)",
    # Мошенничество / скамы
    r"\b(заработок без вложений|гарантия дохода|инвестируй|схема заработка|пирамид)",
    r"\b(скам|scam|phishing|фишинг|развод на деньги)",
    # Доксинг
    r"\b(паспортные данные|номер телефона|адрес проживания|снилс|инн\b)",
    r"\b(dox|doxx|personal info)",
    # Незаконная порнография / NCII
    r"\b(слив.{0,10}(фото|видео)|revenge porn|неконсенсуальн|скрытая камера)",
    r"\b(порн|porn|xxx|секс видео|интим фото).{0,20}(без согласия|украден)",
]

# ================== ТЕКСТЫ ==================
START_FIRST = (
    "Привет. Я Чатти — бот анонимных предложек для канала @Dpodslyshano.\n\n"
    "Я полностью анонимен. Исходный код открыт и опубликован в канале @Dpodslyshano.\n\n"
    "Чтобы отправить предложку, используйте команду:\n"
    "/pred ваш текст\n"
    "Можно прикрепить фото или видео."
)

START_AGAIN = "Вы уже знакомы со мной. Отправьте предложку командой /pred."

MSG_SENT_MODERATION = "Ваша предложка отправлена на модерацию. Пожалуйста, подождите."
MSG_AUTO_APPROVED = "Ваша предложка прошла модерацию и уже опубликована в канале."
MSG_AUTO_REJECTED = "Сожалеем, ваша предложка не прошла модерацию. Измените текст и попробуйте снова."
MSG_MEDIA_TO_ADMIN = (
    "Ваша предложка отправлена на модерацию. "
    "Поскольку она содержит фото или видео, проверка может занять больше времени."
)
MSG_ADMIN_APPROVED = "Администратор одобрил вашу предложку. В ближайшее время она появится в канале."
MSG_ADMIN_REJECTED = (
    "Сожалеем, администратор не одобрил предложку, "
    "поскольку она содержит контент, нарушающий правила Telegram."
)
MSG_TIMEOUT = "Подождите {minutes} мин. {seconds} сек. и вы снова сможете отправлять предложки."
MSG_SPAM = "Такая или очень похожая предложка уже была отправлена ранее."
MSG_TOO_LONG = f"Текст предложки не должен превышать {MAX_TEXT_LENGTH} символов."
MSG_EDITED = "Текст предложки обновлён. Теперь вы можете отправить /ok или /no."

# ================== ЛОГИРОВАНИЕ ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ================== БАЗА ДАННЫХ ==================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_start INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text_normalized TEXT,
                original_text TEXT,
                edited_text TEXT,
                has_media INTEGER,
                media_type TEXT,
                file_id TEXT,
                status TEXT,
                created_at TEXT,
                message_id_admin INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS timeouts (
                user_id INTEGER PRIMARY KEY,
                until_time TEXT
            )
        """)
        await db.commit()

def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text

async def is_banned(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    for pattern in BANNED_PATTERNS:
        if re.search(pattern, lower, re.IGNORECASE):
            return True
    return False

async def is_similar(text: str) -> bool:
    norm = normalize_text(text)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT text_normalized FROM suggestions WHERE status IN ('pending', 'approved')"
        ) as cursor:
            rows = await cursor.fetchall()
            for (existing,) in rows:
                if existing and fuzz.ratio(norm, existing) >= SIMILARITY_THRESHOLD:
                    return True
    return False

async def get_timeout(user_id: int) -> Optional[datetime]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT until_time FROM timeouts WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return datetime.fromisoformat(row[0])
    return None

async def set_timeout(user_id: int):
    until = datetime.utcnow() + timedelta(minutes=TIMEOUT_MINUTES)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO timeouts (user_id, until_time) VALUES (?, ?)",
            (user_id, until.isoformat())
        )
        await db.commit()

def make_footer() -> str:
    return f'\n\n<a href="https://t.me/{BOT_USERNAME}">анон бот</a>'

async def publish_to_channel(text: str, file_id: Optional[str] = None, media_type: Optional[str] = None):
    final_text = (text or "") + make_footer()
    if file_id and media_type == "photo":
        await bot.send_photo(CHANNEL_ID, photo=file_id, caption=final_text, parse_mode=ParseMode.HTML)
    elif file_id and media_type == "video":
        await bot.send_video(CHANNEL_ID, video=file_id, caption=final_text, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(CHANNEL_ID, final_text, parse_mode=ParseMode.HTML)

# ================== ХЕНДЛЕРЫ ==================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT first_start FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            await db.execute(
                "INSERT INTO users (user_id, first_start) VALUES (?, 0)", (user_id,)
            )
            await db.commit()
            await message.answer(START_FIRST)
        else:
            await message.answer(START_AGAIN)

@dp.message(Command("pred"))
async def cmd_pred(message: Message):
    user_id = message.from_user.id

    # Таймаут
    until = await get_timeout(user_id)
    if until and until > datetime.utcnow():
        remaining = until - datetime.utcnow()
        minutes = int(remaining.total_seconds() // 60)
        seconds = int(remaining.total_seconds() % 60)
        await message.answer(MSG_TIMEOUT.format(minutes=minutes, seconds=seconds))
        return

    # Определяем тип контента
    content_type = message.content_type
    has_media = False
    media_type = None
    file_id = None

    if content_type == ContentType.PHOTO:
        has_media = True
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif content_type == ContentType.VIDEO:
        has_media = True
        media_type = "video"
        file_id = message.video.file_id
    elif content_type not in {ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO}:
        # Стикеры, гс, кружки и т.д. — полностью игнорируем
        return

    text = message.text or message.caption or ""
    if text.startswith("/pred"):
        text = text[5:].strip()

    if not text and not has_media:
        await message.answer("Отправьте текст после команды /pred или прикрепите фото/видео.")
        return

    if len(text) > MAX_TEXT_LENGTH:
        await message.answer(MSG_TOO_LONG)
        return

    # Автомодерация текста
    if text and await is_banned(text):
        await message.answer(MSG_AUTO_REJECTED)
        return

    # Антиспам (похожесть)
    if text and await is_similar(text):
        await message.answer(MSG_SPAM)
        return

    norm = normalize_text(text) if text else ""

    # Сохраняем
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO suggestions 
               (user_id, text_normalized, original_text, edited_text, has_media, 
                media_type, file_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (user_id, norm, text, text, 1 if has_media else 0,
             media_type, file_id, datetime.utcnow().isoformat())
        )
        suggestion_id = cursor.lastrowid
        await db.commit()

    if has_media:
        await message.answer(MSG_MEDIA_TO_ADMIN)

        admin_text = (
            f"Новая предложка #{suggestion_id}\n"
            f"От пользователя: {user_id}\n"
            f"Текст: {text or '(без текста)'}\n\n"
            f"Команды (ответьте на это сообщение):\n"
            f"/ok — одобрить\n"
            f"/no — отклонить\n"
            f"/red новый текст — отредактировать"
        )
        admin_msg = await bot.send_message(ADMIN_CHAT_ID, admin_text)

        # Пересылаем медиа
        await message.copy_to(ADMIN_CHAT_ID)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE suggestions SET message_id_admin = ? WHERE id = ?",
                (admin_msg.message_id, suggestion_id)
            )
            await db.commit()
    else:
        # Чистый текст — публикуем сразу
        await publish_to_channel(text)
        await message.answer(MSG_AUTO_APPROVED)
        await set_timeout(user_id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE suggestions SET status = 'approved' WHERE id = ?",
                (suggestion_id,)
            )
            await db.commit()

@dp.message(Command("red"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_red(message: Message):
    if not message.reply_to_message:
        await message.answer("Ответьте командой /red на сообщение с предложкой.")
        return

    new_text = message.text[4:].strip() if message.text.startswith("/red") else ""
    if not new_text:
        await message.answer("Укажите новый текст после команды /red.")
        return

    if len(new_text) > MAX_TEXT_LENGTH:
        await message.answer(MSG_TOO_LONG)
        return

    replied_id = message.reply_to_message.message_id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM suggestions WHERE message_id_admin = ? AND status = 'pending'",
            (replied_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await message.answer("Предложка не найдена или уже обработана.")
            return

        sug_id = row[0]
        await db.execute(
            "UPDATE suggestions SET edited_text = ? WHERE id = ?",
            (new_text, sug_id)
        )
        await db.commit()

    await message.answer(MSG_EDITED)

@dp.message(Command("ok"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_ok(message: Message):
    if not message.reply_to_message:
        await message.answer("Ответьте командой /ok на сообщение с предложкой.")
        return

    replied_id = message.reply_to_message.message_id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, user_id, edited_text, has_media, media_type, file_id 
               FROM suggestions WHERE message_id_admin = ? AND status = 'pending'""",
            (replied_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await message.answer("Предложка не найдена или уже обработана.")
            return

        sug_id, user_id, text, has_media, media_type, file_id = row

        await publish_to_channel(text, file_id if has_media else None, media_type)

        await db.execute(
            "UPDATE suggestions SET status = 'approved' WHERE id = ?", (sug_id,)
        )
        await db.commit()

    await set_timeout(user_id)
    await bot.send_message(user_id, MSG_ADMIN_APPROVED)
    await message.answer(f"Предложка #{sug_id} одобрена и опубликована.")

@dp.message(Command("no"), F.chat.id == ADMIN_CHAT_ID)
async def cmd_no(message: Message):
    if not message.reply_to_message:
        await message.answer("Ответьте командой /no на сообщение с предложкой.")
        return

    replied_id = message.reply_to_message.message_id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id FROM suggestions WHERE message_id_admin = ? AND status = 'pending'",
            (replied_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await message.answer("Предложка не найдена или уже обработана.")
            return

        sug_id, user_id = row
        await db.execute(
            "UPDATE suggestions SET status = 'rejected' WHERE id = ?", (sug_id,)
        )
        await db.commit()

    await bot.send_message(user_id, MSG_ADMIN_REJECTED)
    await message.answer(f"Предложка #{sug_id} отклонена.")

# ================== ЗАПУСК ==================
async def main():
    await init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())