import asyncio
import gc
import io
import json
import logging
import os
import platform
import re
import sys
import time
import uuid
from collections import deque
from logging.handlers import RotatingFileHandler

import httpx
import psutil

# Принудительная установка UTF-8 для Windows консоли
if platform.system() == "Windows":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai as genai_client
from google.genai import types as genai_types
from PIL import Image
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InlineQueryResultsButton,
    InputMediaPhoto,
    InputTextMessageContent,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatType
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)
from youtube_transcript_api import YouTubeTranscriptApi

# Базовая папка скрипта
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "tmp_media")
os.makedirs(TEMP_DIR, exist_ok=True)

# Загрузка переменных окружения
env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=env_path)

# --- КОНФИГУРАЦИЯ ---

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("❌ Не найдены переменные окружения! Проверьте файл .env")

# Проверка ADMIN_ID для админ-функций
if not ADMIN_ID:
    print("ВНИМАНИЕ: ADMIN_ID не задан в .env! Админ-функции будут недоступны.")

# Базовые настройки
MEMORY_TIMEOUT = 15 * 60  # 15 минут неактивности для обычного разговора
MEMORY_DEBUG = os.getenv("MEMORY_DEBUG", "0") == "1"
MEMORY_MONITOR_INTERVAL = int(os.getenv("MEMORY_MONITOR_INTERVAL", "10"))
MAX_RETRIES = 2

# Cleanup TTL (в секундах)
CLEANUP_INTERVAL = 60
PHOTO_TASK_TTL = 15 * 60
PENDING_ALBUM_TTL = 10 * 60
PENDING_TWEET_TTL = 10 * 60
ACTIVE_IMAGE_TTL = 15 * 60
LAST_GENERATED_PHOTO_TTL = 15 * 60
LAST_EDIT_DATA_TTL = 15 * 60
CHAT_SESSION_IDLE_TTL = 15 * 60
TEMP_FILE_TTL = 30 * 60
HTTP_CLIENT_TIMEOUT = 10.0

# Gemini chat session limits
MAX_ACTIVE_CHAT_SESSIONS = 50
MAX_CHAT_MESSAGES_PER_SESSION = 10

# Таймауты (в секундах)
TIMEOUT_SHORT = 60  # Перевод, YouTube саммари — обычно 5-15 сек
TIMEOUT_MEDIUM = 300  # Gemini чат с google_search — может искать долго
TIMEOUT_LONG = 180  # Генерация/редактирование изображений — самые долгие
PHOTO_BUTTON_TIMEOUT = 180  # Время жизни кнопок под фото (3 мин)
IMAGE_CONTEXT_TIMEOUT = 300  # Время жизни изображения в контексте (5 мин)

# Telegram лимиты
MAX_MESSAGE_LENGTH = 4000  # Максимальная длина сообщения
ALBUM_WAIT_TIME = 2.5  # Секунды ожидания остальных фото альбома
MAX_ALBUM_PHOTOS = 5  # Максимум фото в альбоме для обработки на сервере 1 ГБ RAM

# Лимиты подготовки изображений перед Gemini
MAX_IMAGE_SIDE = 1280
IMAGE_JPEG_QUALITY = 82
MAX_IMAGE_BYTES = 6_000_000
MAX_VOICE_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024

# Системная инструкция для Flash — краткость и скорость
SYSTEM_INSTRUCTION_FLASH = """Ты — быстрый помощник. МАКСИМУМ СМЫСЛА В МИНИМУМЕ СЛОВ.

• Отвечай предельно кратко и по делу
• Избегай "воды", клише, вступлений
• Для простых вопросов — 1-2 предложения
• Используй интернет для поиска информации
"""

# Системная инструкция для Pro — глубина и анализ
SYSTEM_INSTRUCTION_PRO = """Ты — интеллектуальный помощник с фокусом на глубину мысли.
• Используй интернет для поиска информации
"""

# Файл с доступами
USERS_FILE = os.path.join(BASE_DIR, "allowed_users.json")

# Файл с пользовательскими настройками
USER_SETTINGS_FILE = os.path.join(BASE_DIR, "user_settings.json")

# Клиент нового SDK (для чатов с google_search и генерации изображений)
gemini_client = genai_client.Client(api_key=GEMINI_API_KEY)

# Инструменты для интернет-поиска и анализа URL
SEARCH_TOOLS = [{"google_search": {}}, {"url_context": {}}]

# Модели для генераций изображений (Nano Banana) - Free Tier
IMAGE_MODELS = {
    "pro": "gemini-3.1-flash-image-preview",  # Pro заблокирована, используем Flash
    "flash": "gemini-3.1-flash-image-preview",
}


# Ссылки для инлайн-заглушек
avatar_url = (
    "https://raw.githubusercontent.com/Eniggman/GeminiTelegramBot/main/docs/image.png"
)
# Гарантированно рабочий черный квадрат (Placehold.co)
BLACK_SQUARE_URL = "https://placehold.co/600x400/000000/000000.png"

# Паттерн для детекции ссылок Twitter/X
TWITTER_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/(\d+)", re.IGNORECASE
)


# Настройка логирования с ротацией

# Константы для логирования
LOG_FILE = os.path.join(os.path.dirname(BASE_DIR), "logs", "bot.log")
LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 МБ максимум на файл
LOG_BACKUP_COUNT = 1  # Хранить 1 бэкап (итого макс ~100 МБ)
ACTIVITY_LOG_MAX_ENTRIES = 200  # Максимум последних событий activity_log в RAM
LOG_TO_FILE = os.getenv("LOG_TO_FILE", "1") == "1"
SAVE_ACTIVITY_LOG = os.getenv("SAVE_ACTIVITY_LOG", "1") == "1"
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))
LOG_MAX_TOTAL_BYTES = int(
    os.getenv("LOG_MAX_TOTAL_BYTES", str(LOG_MAX_BYTES * (LOG_BACKUP_COUNT + 1)))
)

# Настройка форматтера
log_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# Консольный хендлер (для отладки)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.ERROR)

# Файловый хендлер с ротацией (опционально)
handlers = [console_handler]
if LOG_TO_FILE:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)
    handlers.append(file_handler)

# Настройка корневого логгера
logging.basicConfig(level=logging.INFO, handlers=handlers)

# Отключаем лишний шум от библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# --- ОЧИСТКА ЛОГОВ ---
def cleanup_log_files() -> None:
    """Удаляет старые или избыточные логи, чтобы не засорять диск."""
    if not LOG_TO_FILE:
        return

    try:
        now = time.time()
        retention_sec = max(LOG_RETENTION_DAYS, 0) * 86400
        log_files = []

        for name in os.listdir(BASE_DIR):
            if name == os.path.basename(LOG_FILE) or name.startswith(
                f"{os.path.basename(LOG_FILE)}."
            ):
                try:
                    path = os.path.join(BASE_DIR, name)
                    stat = os.stat(path)
                    if retention_sec and (now - stat.st_mtime) > retention_sec:
                        os.remove(path)
                        continue
                    log_files.append((path, stat.st_mtime, stat.st_size))
                except Exception:
                    continue

        if LOG_MAX_TOTAL_BYTES > 0:
            total = sum(size for _, _, size in log_files)
            if total > LOG_MAX_TOTAL_BYTES:
                # Удаляем самые старые, пока не впишемся в лимит
                for path, _, size in sorted(log_files, key=lambda x: x[1]):
                    try:
                        os.remove(path)
                        total -= size
                        if total <= LOG_MAX_TOTAL_BYTES:
                            break
                    except Exception:
                        continue
    except Exception:
        pass


# --- ФИКСИРОВАННЫЕ МОДЕЛИ С ПРОВЕРКОЙ ДОСТУПНОСТИ ---


def get_latest_models() -> dict[str, str]:
    """
    Возвращает проверенные модели для текущего Free Tier API-ключа.

    По тестам:
    - gemini-3-flash-preview работает без tools, но с tools получает 429;
    - gemini-2.5-flash работает без tools и с Google Search + URL context;
    - gemini-2.5-pro сейчас недоступна в Free Tier (limit: 0).
    """
    pro_model = "gemini-3-flash-preview"  # Pro-режим: 3 Flash без tools
    flash_model = "gemini-2.5-flash"  # Flash-режим: 2.5 Flash с tools
    lite_model = "gemini-2.5-flash-lite"
    image_model = "gemini-3.1-flash-image-preview"

    return {
        "pro": pro_model,
        "flash": flash_model,
        "lite": lite_model,
        "img_pro": image_model,
        "img_flash": image_model,
    }


# Будет инициализировано в main()
MODELS = {}


def initialize_models() -> None:
    """Инициализирует глобальные переменные MODELS и IMAGE_MODELS"""
    try:
        latest = get_latest_models()
        MODELS.update(
            {"pro": latest["pro"], "flash": latest["flash"], "lite": latest["lite"]}
        )
        # Обновляем IMAGE_MODELS из проверенных данных
        IMAGE_MODELS.update({"pro": latest["img_pro"], "flash": latest["img_flash"]})
        logger.debug(
            f"✅ Модели: Pro={MODELS['pro']}, Flash={MODELS['flash']}, Lite={MODELS['lite']}"
        )
        logger.debug(
            f"🎨 Фото Модели: Pro={IMAGE_MODELS['pro']}, Flash={IMAGE_MODELS['flash']}"
        )
    except Exception as e:
        logger.error(f"Critical Error: {e}")
        # Фоллбек для запуска без сети / при ошибке инициализации
        MODELS.update(
            {
                "pro": "gemini-3-flash-preview",
                "flash": "gemini-2.5-flash",
                "lite": "gemini-2.5-flash-lite",
            }
        )
        IMAGE_MODELS.update(
            {
                "pro": "gemini-3.1-flash-image-preview",
                "flash": "gemini-3.1-flash-image-preview",
            }
        )
        print(f"Работаем с дефолтными моделями (Offline mode): {MODELS}")


# --- ПАМЯТЬ БОТА ---
# Хранит сессии Gemini, текущие модели пользователей и режимы работы.


allowed_users = set()

# Глобальные настройки пользователей (например, выбор image_model)
user_settings = {}

# Хранилище для сбора альбомов (media_group)
pending_albums = {}

# URL изображения бота
BOT_AVATAR_URL = (
    "https://raw.githubusercontent.com/Eniggman/GeminiTelegramBot/main/docs/image.png"
)

# --- СТАТИСТИКА И ЛОГИ ---
bot_stats = {
    "start_time": time.time(),
    "messages_count": 0,
    "voice_count": 0,
    "errors_count": 0,
    "last_errors": [],
    "cleanup": {
        "last_run": 0,
        "runs": 0,
        "photo_task": 0,
        "pending_albums": 0,
        "pending_tweet": 0,
        "active_image": 0,
        "last_generated_photo": 0,
        "last_edit_data": 0,
        "chat_session": 0,
        "temp_files": 0,
    },
}

PROCESS = psutil.Process(os.getpid())


def get_process_rss_mb() -> float:
    """Возвращает RSS текущего процесса в МБ."""
    try:
        return PROCESS.memory_info().rss / (1024 * 1024)
    except Exception as e:
        logger.debug(f"Memory RSS read error: {e}")
        return 0.0


def log_memory(label: str, user_id: int | None = None) -> None:
    """Логирует RSS процесса, если включён MEMORY_DEBUG=1."""
    if not MEMORY_DEBUG:
        return
    try:
        rss_mb = get_process_rss_mb()
        ram = psutil.virtual_memory()
        user_part = f" user={user_id}" if user_id is not None else ""
        logger.info(
            f"[MEMORY]{user_part} {label}: RSS={rss_mb:.1f} MB, "
            f"system={ram.percent}% ({ram.used / (1024**2):.0f}/{ram.total / (1024**2):.0f} MB)"
        )
    except Exception as e:
        logger.debug(f"Memory debug log error: {e}")


async def memory_monitor_loop() -> None:
    """Периодически пишет RSS процесса в лог при MEMORY_DEBUG=1."""
    if not MEMORY_DEBUG or MEMORY_MONITOR_INTERVAL <= 0:
        return

    logger.info(f"Memory monitor started: interval={MEMORY_MONITOR_INTERVAL}s")
    while True:
        try:
            log_memory("live")
            await asyncio.sleep(MEMORY_MONITOR_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Memory monitor stopped")
            raise
        except Exception as e:
            logger.debug(f"Memory monitor error: {e}")
            await asyncio.sleep(MEMORY_MONITOR_INTERVAL)


def sanitize_error(error_msg: object, limit: int = 200) -> str:
    """Убирает переводы строк и длинные хвосты из текста ошибки для логов/статуса."""
    text = str(error_msg or "unknown")
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    return text[:limit]


def log_error(error_type: str, error_msg: str, user_id: int = None):
    """Сохраняет ошибку в лог"""
    sanitized = sanitize_error(error_msg, 200)
    error_entry = {
        "time": time.strftime("%d.%m %H:%M"),
        "type": error_type,
        "msg": sanitized[:100],
        "user": user_id,
    }
    bot_stats["errors_count"] += 1
    bot_stats["last_errors"].append(error_entry)
    if len(bot_stats["last_errors"]) > 10:
        bot_stats["last_errors"].pop(0)
    logger.error(f"{error_type}: {sanitized}")


async def delete_safe(message: object):
    """Безопасно удаляет сообщение Telegram."""
    try:
        if message:
            await message.delete()
    except Exception:
        pass


def gc_collect_after_media(
    label: str | None = None, user_id: int | None = None
) -> None:
    """Лёгкий GC после тяжёлых медиа-сценариев."""
    try:
        gc.collect(0)
        if label:
            log_memory(label, user_id)
    except Exception as e:
        logger.debug(f"GC error: {e}")


def safe_delete_file(path: str | None) -> bool:
    """Удаляет файл без исключений. Возвращает True, если файл удалён."""
    if not path:
        return False
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception as e:
        logger.debug(f"Temp file delete error for {path}: {e}")
    return False


def get_temp_dir_stats() -> tuple[int, int]:
    """Возвращает количество файлов и общий размер TEMP_DIR в байтах."""
    count = 0
    total_size = 0
    try:
        for name in os.listdir(TEMP_DIR):
            path = os.path.join(TEMP_DIR, name)
            if os.path.isfile(path):
                count += 1
                total_size += os.path.getsize(path)
    except Exception as e:
        logger.debug(f"Temp dir stats error: {e}")
    return count, total_size


def cleanup_old_temp_files(current_time: float | None = None) -> int:
    """Удаляет старые temp-файлы с диска по TEMP_FILE_TTL."""
    current_time = current_time or time.time()
    removed = 0
    try:
        for name in os.listdir(TEMP_DIR):
            path = os.path.join(TEMP_DIR, name)
            if (
                os.path.isfile(path)
                and current_time - os.path.getmtime(path) > TEMP_FILE_TTL
            ):
                if safe_delete_file(path):
                    removed += 1
    except Exception as e:
        logger.debug(f"Temp cleanup error: {e}")
    return removed


def read_binary_file(path: str) -> bytes:
    """Читает файл в bytes и гарантированно закрывает file handle."""
    with open(path, "rb") as f:
        return f.read()


async def download_telegram_file_to_temp(telegram_file, suffix: str = "") -> str:
    """Скачивает Telegram File во временный файл на диске."""
    safe_suffix = re.sub(r"[^A-Za-z0-9._-]", "_", suffix or "")[:32]
    temp_path = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex}{safe_suffix}")
    await telegram_file.download_to_drive(custom_path=temp_path)
    return temp_path


async def get_http_client(app: Application) -> httpx.AsyncClient:
    """Возвращает общий AsyncClient для коротких HTTP-запросов."""
    client = app.bot_data.get("http_client")
    if not isinstance(client, httpx.AsyncClient) or client.is_closed:
        # Добавляем современный User-Agent, чтобы сервисы (Twitter, YouTube) не блокировали бота
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        }
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_CLIENT_TIMEOUT),
            follow_redirects=True,
            headers=headers
        )
        app.bot_data["http_client"] = client
    return client


async def close_http_client(app: Application) -> None:
    """Закрывает общий AsyncClient при остановке приложения."""
    client = app.bot_data.pop("http_client", None)
    if isinstance(client, httpx.AsyncClient) and not client.is_closed:
        await client.aclose()


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок"""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    user_id = None
    if isinstance(update, Update) and update.effective_user:
        user_id = update.effective_user.id

    # Логируем в статистику
    log_error("GLOBAL_HANDLER", str(context.error), user_id)

    # Если это сетевая ошибка - просто логируем warning
    if isinstance(context.error, NetworkError):
        logger.warning(f"NetworkError: {context.error}")
        return

    # Уведомляем пользователя если это возможно
    if isinstance(update, Update) and update.effective_message:
        try:
            error_text = (
                "🛑 Произошла ошибка системы: "
                f"<code>{escape_html(str(context.error)[:100])}</code>"
            )
            await update.effective_message.reply_text(error_text, parse_mode="HTML")
        except Exception as notify_err:
            logger.debug(f"Не удалось уведомить пользователя об ошибке: {notify_err}")


# Часовой пояс Киева
KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Файл для логов активности
ACTIVITY_LOG_FILE = os.path.join(os.path.dirname(BASE_DIR), "logs", "activity_log.jsonl")
LEGACY_ACTIVITY_LOG_FILE = os.path.join(BASE_DIR, "activity_log.json")

# Структура логов
user_activity = deque(maxlen=ACTIVITY_LOG_MAX_ENTRIES)
daily_counters = {
    "date": "",
    "actions": {},
    "users": {},
}


def get_day_start() -> float:
    """Возвращает timestamp начала текущего дня по Киеву (00:00)"""
    now_kyiv = datetime.now(KYIV_TZ)
    day_start = now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.timestamp()


def get_today_key() -> str:
    """Возвращает текущую дату по Киеву для daily counters."""
    return datetime.now(KYIV_TZ).strftime("%Y-%m-%d")


def ensure_daily_counters() -> None:
    """Сбрасывает daily counters при смене дня."""
    today = get_today_key()
    if daily_counters.get("date") != today:
        daily_counters["date"] = today
        daily_counters["actions"] = {}
        daily_counters["users"] = {}


def update_daily_counters(entry: dict) -> None:
    """Обновляет счётчики активности за текущий день."""
    ensure_daily_counters()
    action = entry.get("action", "unknown")
    daily_counters["actions"][action] = daily_counters["actions"].get(action, 0) + 1

    uid = str(entry.get("user_id"))
    user_stats = daily_counters["users"].setdefault(
        uid,
        {
            "username": entry.get("username", "Unknown"),
            "text": 0,
            "voice": 0,
            "img_gen": 0,
            "img_analyze": 0,
            "img_edit": 0,
        },
    )
    user_stats["username"] = entry.get("username", user_stats["username"])

    if action == "text":
        user_stats["text"] += 1
    elif action == "voice":
        user_stats["voice"] += 1
    elif action in ("img_gen", "image_gen", "img_regen"):
        user_stats["img_gen"] += 1
    elif action in ("img_analyze", "img_analyze_btn", "img_analyze_prompt"):
        user_stats["img_analyze"] += 1
    elif action in (
        "img_edit",
        "img_edit_btn_done",
        "img_edit_album",
        "img_edit_regen",
    ):
        user_stats["img_edit"] += 1


def log_activity(user_id: int, username: str, action: str, details: str = "") -> None:
    """Логирует активность пользователя"""
    entry = {
        "timestamp": time.time(),
        "user_id": user_id,
        "username": username or "Unknown",
        "action": action,
        "details": details,
    }
    user_activity.append(entry)
    update_daily_counters(entry)
    save_activity_log(entry)


def save_activity_log(entry: dict) -> None:
    """Append-only запись события активности в JSONL."""
    if not SAVE_ACTIVITY_LOG:
        return
    try:
        with open(ACTIVITY_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as e:
        logger.warning(f"Activity log save error: {e}")


def iter_activity_entries_from_file(path: str):
    """Читает activity log из JSONL или legacy JSON."""
    if path.endswith(".jsonl"):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)
    else:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            for entry in data:
                yield entry


def load_activity_log() -> None:
    """Загружает последние события и восстанавливает счётчики за текущий день."""
    if not SAVE_ACTIVITY_LOG:
        return
    global user_activity
    user_activity = deque(maxlen=ACTIVITY_LOG_MAX_ENTRIES)
    ensure_daily_counters()
    day_start = get_day_start()

    for path in (LEGACY_ACTIVITY_LOG_FILE, ACTIVITY_LOG_FILE):
        if not os.path.exists(path):
            continue
        try:
            for entry in iter_activity_entries_from_file(path):
                if entry.get("timestamp", 0) >= day_start:
                    user_activity.append(entry)
                    update_daily_counters(entry)
        except Exception as e:
            logger.warning(f"Activity log load error ({path}): {e}")


# --- ФУНКЦИИ УПРАВЛЕНИЯ ПОЛЬЗОВАТЕЛЯМИ ---
def load_users() -> None:
    global allowed_users
    env_users = os.getenv("ALLOWED_USERS", "")
    if env_users:
        try:
            allowed_users.update(
                int(u.strip()) for u in env_users.split(",") if u.strip()
            )
        except Exception as e:
            logger.warning(f"Ошибка загрузки ALLOWED_USERS: {e}")

    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                allowed_users.update(set(json.load(f)))
        except Exception as e:
            logger.warning(f"Ошибка загрузки {USERS_FILE}: {e}")


def save_users() -> None:
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(allowed_users), f)
    except Exception as e:
        logger.warning(f"Ошибка сохранения пользователей: {e}")


def load_user_settings() -> None:
    global user_settings
    if os.path.exists(USER_SETTINGS_FILE):
        try:
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                user_settings = json.load(f)
        except Exception as e:
            logger.warning(f"Ошибка загрузки {USER_SETTINGS_FILE}: {e}")
            user_settings = {}


def save_user_settings() -> None:
    try:
        with open(USER_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_settings, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        logger.warning(f"Ошибка сохранения настроек пользователей: {e}")


def check_access(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in allowed_users


def get_bot_avatar_url() -> str:
    """URL аватарки бота для inline-результатов"""
    return BOT_AVATAR_URL


def get_user_image_model(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Получает и кэширует в context предпочтительную модель изображений пользователя (pro/flash)"""
    val = context.user_data.get("image_model")
    if not val:
        uid_str = str(user_id)
        if uid_str in user_settings and "image_model" in user_settings[uid_str]:
            val = user_settings[uid_str]["image_model"]
        else:
            val = "pro"  # По умолчанию pro
        context.user_data["image_model"] = val
    return val


def get_model_key(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Возвращает ключ модели пользователя (pro/flash)"""
    return context.user_data.get("model", "flash")


def increment_chat_message_count(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Увеличивает счётчик сообщений текущей Gemini chat_session."""
    context.user_data["chat_message_count"] = (
        context.user_data.get("chat_message_count", 0) + 1
    )


def make_telegram_media_ref(file_id: str) -> dict:
    """Создаёт лёгкую ссылку на Telegram-файл вместо хранения bytes в RAM."""
    return {
        "kind": "telegram_file",
        "file_id": file_id,
        "timestamp": time.time(),
    }


def is_media_ref(value: object) -> bool:
    """Проверяет, что значение похоже на MediaRef."""
    return (
        isinstance(value, dict)
        and value.get("kind") == "telegram_file"
        and bool(value.get("file_id"))
    )


async def download_media_ref(bot, media_ref: dict) -> bytes:
    """Скачивает Telegram-файл по MediaRef только перед обработкой."""
    file = await bot.get_file(media_ref["file_id"])
    return bytes(await file.download_as_bytearray())


def prepare_image_for_gemini(image_bytes: bytes) -> bytes:
    """Уменьшает и конвертирует изображение в JPEG перед отправкой в Gemini."""
    image = None
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)

        if image.mode != "RGB":
            converted = image.convert("RGB")
            image.close()
            image = converted

        for quality in (IMAGE_JPEG_QUALITY, 78, 74, 70):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            result = output.getvalue()
            output.close()
            if len(result) <= MAX_IMAGE_BYTES or quality == 70:
                return result

        return result
    finally:
        if image:
            try:
                image.close()
            except Exception:
                pass


async def resolve_media_items_to_bytes(bot, media_items: list) -> list[bytes]:
    """Преобразует список MediaRef/bytes в подготовленные bytes для вызова Gemini."""
    result = []
    for item in media_items:
        raw_bytes = None
        prepared_bytes = None
        try:
            if is_media_ref(item):
                raw_bytes = await download_media_ref(bot, item)
                prepared_bytes = prepare_image_for_gemini(raw_bytes)
            elif isinstance(item, bytearray):
                raw_bytes = bytes(item)
                prepared_bytes = prepare_image_for_gemini(raw_bytes)
            elif isinstance(item, bytes):
                prepared_bytes = prepare_image_for_gemini(item)
            else:
                raise ValueError("Неподдерживаемый формат media item")
            result.append(prepared_bytes)
        finally:
            raw_bytes = None
            prepared_bytes = None
    return result


def get_sent_photo_file_id(message) -> str | None:
    """Достаёт file_id самой большой версии фото из отправленного Telegram-сообщения."""
    if message and getattr(message, "photo", None):
        return message.photo[-1].file_id
    return None


def cleanup_expired_user_data(user_data: dict, current_time: float) -> dict[str, int]:
    """Удаляет устаревшие временные данные пользователя и возвращает счётчики."""
    cleaned = {
        "photo_task": 0,
        "pending_tweet": 0,
        "active_image": 0,
        "last_generated_photo": 0,
        "last_edit_data": 0,
        "chat_session": 0,
    }

    ttl_map = {
        "photo_task": PHOTO_TASK_TTL,
        "pending_tweet": PENDING_TWEET_TTL,
        "active_image": ACTIVE_IMAGE_TTL,
        "last_generated_photo": LAST_GENERATED_PHOTO_TTL,
        "last_edit_data": LAST_EDIT_DATA_TTL,
    }

    for key, ttl in ttl_map.items():
        item = user_data.get(key)
        if isinstance(item, dict):
            timestamp = item.get("timestamp", current_time)
            if current_time - timestamp > ttl:
                user_data.pop(key, None)
                cleaned[key] += 1

    last_activity = user_data.get("last_activity", 0)
    if (
        "chat_session" in user_data
        and last_activity
        and current_time - last_activity > CHAT_SESSION_IDLE_TTL
    ):
        destroy_session(user_data)
        cleaned["chat_session"] += 1

    return cleaned


async def cleanup_loop(app: Application) -> None:
    """Фоновая очистка старых временных данных."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        current_time = time.time()
        cleaned = {
            "photo_task": 0,
            "pending_albums": 0,
            "pending_tweet": 0,
            "active_image": 0,
            "last_generated_photo": 0,
            "last_edit_data": 0,
            "chat_session": 0,
            "temp_files": 0,
        }

        for media_group_id, album_data in list(pending_albums.items()):
            timestamp = album_data.get("timestamp", current_time)
            if current_time - timestamp > PENDING_ALBUM_TTL:
                pending_albums.pop(media_group_id, None)
                cleaned["pending_albums"] += 1

        for user_data in list(app.user_data.values()):
            user_cleaned = cleanup_expired_user_data(user_data, current_time)
            for key, count in user_cleaned.items():
                cleaned[key] += count

        cleaned["temp_files"] += cleanup_old_temp_files(current_time)

        active_sessions = [
            (user_id, user_data.get("last_activity", 0), user_data)
            for user_id, user_data in app.user_data.items()
            if "chat_session" in user_data
        ]
        if len(active_sessions) > MAX_ACTIVE_CHAT_SESSIONS:
            active_sessions.sort(key=lambda item: item[1])
            overflow = len(active_sessions) - MAX_ACTIVE_CHAT_SESSIONS
            for _, _, user_data in active_sessions[:overflow]:
                destroy_session(user_data)
                cleaned["chat_session"] += 1

        cleanup_stats = bot_stats["cleanup"]
        cleanup_stats["last_run"] = current_time
        cleanup_stats["runs"] += 1
        for key, count in cleaned.items():
            cleanup_stats[key] += count

        total_cleaned = sum(cleaned.values())
        if total_cleaned:
            # После удаления сессий принудительно вызываем сборку мусора
            gc.collect()
            logger.info(f"Cleanup removed {total_cleaned} items. RSS now: {get_process_rss_mb():.1f} MB")
            log_memory("cleanup:done")


# --- ФУНКЦИЯ СБРОСА КОНТЕКСТА ---


def destroy_session(user_data: dict) -> None:
    """
    Явно уничтожает сессию Gemini, очищая историю для высвобождения RAM.
    Принимает словарь user_data напрямую для универсальности (используется и в хендлерах, и в cleanup_loop).
    """
    chat = user_data.pop("chat_session", None)
    if chat:
        try:
            # Очищаем историю — самый тяжелый кусок данных в ChatSession
            chat.history = []
            logger.debug("History cleared for a session before destruction.")
        except Exception:
            pass
    
    # Обнуляем счетчики и контекстные медиа
    user_data["chat_message_count"] = 0
    user_data.pop("active_image", None)
    user_data.pop("active_youtube", None)


def reset_session(context: ContextTypes.DEFAULT_TYPE) -> object:
    """
    Создаёт новую Gemini chat session.
    Перед созданием уничтожает старую сессию для экономии памяти.
    """
    # Агрессивная очистка старой сессии
    destroy_session(context.user_data)

    model_key = get_model_key(context)
    instruction = (
        SYSTEM_INSTRUCTION_PRO if model_key == "pro" else SYSTEM_INSTRUCTION_FLASH
    )

    if model_key == "pro":
        config = genai_types.GenerateContentConfig(system_instruction=instruction)
    else:
        config = genai_types.GenerateContentConfig(
            system_instruction=instruction, tools=SEARCH_TOOLS
        )

    chat = gemini_client.chats.create(
        model=MODELS[model_key],
        config=config,
    )

    context.user_data["chat_session"] = chat
    context.user_data["last_activity"] = time.time()
    
    # Режим сбрасываем только если это не специальный режим (например, YouTube)
    if context.user_data.get("mode") not in ("youtube_mode", "translate", "image_gen"):
        context.user_data.pop("mode", None)

    return chat


def get_or_create_session(context: ContextTypes.DEFAULT_TYPE) -> object:
    """Получает сессию или создаёт новую если нужно"""
    current_time = time.time()
    last_time = context.user_data.get("last_activity", 0)

    # Проверяем таймаут и лимит длины истории
    message_count = context.user_data.get("chat_message_count", 0)
    if (
        "chat_session" not in context.user_data
        or (current_time - last_time) > MEMORY_TIMEOUT
        or message_count >= MAX_CHAT_MESSAGES_PER_SESSION
    ):
        reset_session(context)
    else:
        context.user_data["last_activity"] = current_time

    return context.user_data["chat_session"]


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---


def format_gemini_error(error: Exception, context_info: str = "") -> str:
    """
    Универсальная функция для форматирования ошибок Gemini API.
    Классифицирует ошибки и возвращает понятное сообщение для пользователя.
    Источник: https://ai.google.dev/gemini-api/docs/troubleshooting

    Args:
        error: Исключение от Gemini API
        context_info: Дополнительный контекст (например, "генерация изображения")

    Returns:
        Форматированное сообщение об ошибке
    """
    error_str = str(error).lower()
    error_full = str(error)
    error_safe = escape_html(error_full)
    prefix = f"[{context_info}] " if context_info else ""

    # Ошибка сервиса (503 / 500) - высокая нагрузка или сбой
    if (
        "503" in error_str
        or "500" in error_str
        or "unavailable" in error_str
        or "high demand" in error_str
    ):
        return (
            f"⏳ {prefix}**Google сейчас перегружен** (высокая нагрузка).\n"
            f"Пожалуйста, подожди 30-60 секунд и попробуй снова.\n"
            f"`[Error 503/500: Server Issue]`"
        )

    # Квота / Rate Limit
    if "quota" in error_str or "rate limit" in error_str or "429" in error_str:
        if "limit: 0" in error_str:
            return (
                f"🚦 {prefix}**Ошибка квоты (Limit: 0)**\n\n"
                f"Похоже, эта функция (Image Gen) недоступна для чистого Free Tier аккаунта.\n\n"
                f"**Варианты и их значения:**\n"
                f"• **Billing Required** (Google требует привязать карту в Cloud Console, чтобы активировать Image-модели, даже если ты не будешь выходить за бесплатные лимиты).\n"
                f"• **Region Restriction** (В некоторых странах генерация медиа запрещена законом или политикой Google для бесплатных ключей).\n"
                f"• **Project Type** (Ваш проект в Google Cloud не имеет 'активного' статуса для медиа-запросов).\n\n"
                f"💡 _Попробуйте использовать текстовые запросы или привяжите Billing в консоли Google._"
            )
        return f"🚦 {prefix}[QUOTA] Превышен лимит запросов. Попробуй позже.\n`{error_safe[:120]}`"

    # Фильтр безопасности
    if (
        "blocked" in error_str
        or "safety" in error_str
        or "harmful" in error_str
        or "finish_reason" in error_str
    ):
        return f"🛡️ {prefix}[SAFETY] Контент заблокирован фильтром безопасности.\n`{error_safe[:120]}`"

    # Проблемы с авторизацией
    if (
        "api key" in error_str
        or "invalid" in error_str
        or "401" in error_str
        or "403" in error_str
    ):
        return f"🔑 {prefix}[AUTH] Проблема с API ключом.\n`{error_safe[:150]}`"

    # Модель недоступна (условие после 503, так как 503 часто содержит слово unavailable)
    if "model" in error_str and (
        "not found" in error_str or "does not exist" in error_str
    ):
        return f"🤖 {prefix}[MODEL] Модель не найдена или не поддерживается.\n`{error_safe[:120]}`"

    # Слишком длинный запрос
    if "token" in error_str and (
        "limit" in error_str or "exceed" in error_str or "too long" in error_str
    ):
        return f"📏 {prefix}[TOKEN LIMIT] Запрос слишком длинный.\n`{error_safe[:100]}`"

    # Проблемы с сетью
    if "connection" in error_str or "timeout" in error_str or "network" in error_str:
        return f"🌐 {prefix}[NETWORK] Ошибка сети.\n`{error_safe[:100]}`"

    # Неизвестная ошибка сервера Google
    if "internal" in error_str or "server" in error_str:
        return f"💥 {prefix}[SERVER] Внутренняя ошибка сервера Google.\n`{error_safe[:100]}`"

    # Неподдерживаемый формат
    if (
        "unsupported" in error_str
        or "invalid format" in error_str
        or "mime" in error_str
    ):
        return f"📄 {prefix}[FORMAT] Неподдерживаемый формат.\n<code>{error_safe[:120]}</code>"

    # Неизвестная ошибка — показываем полностью для отладки
    return f"{prefix}[ERROR]\n<code>{error_safe[:250]}</code>"


def escape_html(text: str) -> str:
    """
    Экранирует HTML-спецсимволы для безопасной отправки в Telegram.
    Источник: https://core.telegram.org/bots/api#html-style
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean_latex(text: str) -> str:
    """
    Конвертирует LaTeX-формулы в читаемый Unicode-текст для Telegram.
    Gemini иногда отвечает с $...$, \text{}, \frac{}{} и т.д.,
    которые Telegram не умеет рендерить.
    """
    if "$" not in text and "\\" not in text:
        return text

    # Убираем блочные формулы $$...$$, потом инлайн $...$
    text = re.sub(r"\$\$(.*?)\$\$", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\$([^$]+?)\$", r"\1", text)

    # \text{...} → содержимое
    text = re.sub(r"\\text\{([^}]*)\}", r"\1", text)
    # \textbf{...} → содержимое
    text = re.sub(r"\\textbf\{([^}]*)\}", r"\1", text)
    # \mathrm{...} → содержимое
    text = re.sub(r"\\mathrm\{([^}]*)\}", r"\1", text)
    # \mathbf{...} → содержимое
    text = re.sub(r"\\mathbf\{([^}]*)\}", r"\1", text)

    # \frac{a}{b} → a/b
    text = re.sub(r"\\frac\{([^}]*)\}\{([^}]*)\}", r"\1/\2", text)
    # \sqrt{x} → √(x)
    text = re.sub(r"\\sqrt\{([^}]*)\}", r"√(\1)", text)

    # Надстрочные цифры: ^{2} → ² или ^2 → ²
    superscripts = {
        "0": "⁰",
        "1": "¹",
        "2": "²",
        "3": "³",
        "4": "⁴",
        "5": "⁵",
        "6": "⁶",
        "7": "⁷",
        "8": "⁸",
        "9": "⁹",
        "+": "⁺",
        "-": "⁻",
        "n": "ⁿ",
    }

    def replace_superscript(match):
        content = match.group(1)
        return "".join(superscripts.get(c, c) for c in content)

    text = re.sub(r"\^\{([^}]*)\}", replace_superscript, text)
    text = re.sub(r"\^(\d)", lambda m: superscripts.get(m.group(1), m.group(1)), text)

    # Подстрочные цифры: _{2} → ₂ или _2 → ₂
    subscripts = {
        "0": "₀",
        "1": "₁",
        "2": "₂",
        "3": "₃",
        "4": "₄",
        "5": "₅",
        "6": "₆",
        "7": "₇",
        "8": "₈",
        "9": "₉",
        "+": "₊",
        "-": "₋",
        "a": "ₐ",
        "e": "ₑ",
        "i": "ᵢ",
        "o": "ₒ",
        "n": "ₙ",
        "x": "ₓ",
    }

    def replace_subscript(match):
        content = match.group(1)
        return "".join(subscripts.get(c, c) for c in content)

    text = re.sub(r"_\{([^}]*)\}", replace_subscript, text)
    text = re.sub(r"_(\d)", lambda m: subscripts.get(m.group(1), m.group(1)), text)

    # Греческие буквы и математические символы
    latex_symbols = {
        # Греческие (строчные)
        "\\alpha": "α",
        "\\beta": "β",
        "\\gamma": "γ",
        "\\delta": "δ",
        "\\epsilon": "ε",
        "\\zeta": "ζ",
        "\\eta": "η",
        "\\theta": "θ",
        "\\iota": "ι",
        "\\kappa": "κ",
        "\\lambda": "λ",
        "\\mu": "μ",
        "\\nu": "ν",
        "\\xi": "ξ",
        "\\pi": "π",
        "\\rho": "ρ",
        "\\sigma": "σ",
        "\\tau": "τ",
        "\\phi": "φ",
        "\\chi": "χ",
        "\\psi": "ψ",
        "\\omega": "ω",
        # Греческие (заглавные)
        "\\Gamma": "Γ",
        "\\Delta": "Δ",
        "\\Theta": "Θ",
        "\\Lambda": "Λ",
        "\\Pi": "Π",
        "\\Sigma": "Σ",
        "\\Phi": "Φ",
        "\\Psi": "Ψ",
        "\\Omega": "Ω",
        # Математические операторы
        "\\cdot": "·",
        "\\times": "×",
        "\\div": "÷",
        "\\pm": "±",
        "\\approx": "≈",
        "\\neq": "≠",
        "\\leq": "≤",
        "\\geq": "≥",
        "\\ll": "≪",
        "\\gg": "≫",
        "\\equiv": "≡",
        "\\sim": "∼",
        "\\propto": "∝",
        "\\infty": "∞",
        # Стрелки
        "\\to": "→",
        "\\rightarrow": "→",
        "\\leftarrow": "←",
        "\\leftrightarrow": "↔",
        "\\Rightarrow": "⇒",
        # Прочее
        "\\sum": "Σ",
        "\\prod": "Π",
        "\\int": "∫",
        "\\partial": "∂",
        "\\nabla": "∇",
        "\\degree": "°",
        "\\circ": "°",
        "\\bullet": "•",
    }

    # Сортируем по длине (длинные сначала), чтобы \lambda не перекрыл \lam
    for latex_cmd, unicode_char in sorted(
        latex_symbols.items(), key=lambda x: -len(x[0])
    ):
        text = text.replace(latex_cmd, unicode_char)

    # Убираем LaTeX-пробелы: \, \; \! \quad \qquad
    text = re.sub(r"\\[,;!]", " ", text)
    text = re.sub(r"\\q?quad", " ", text)

    # Убираем \left и \right (скобки остаются)
    text = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)", "", text)

    # Убираем оставшиеся \commandname (неизвестные команды) — но оставляем \n, \t
    text = re.sub(r"\\([a-zA-Z]+)", r"\1", text)

    # Чистим двойные пробелы
    text = re.sub(r"  +", " ", text)

    return text


def format_for_telegram(text: str) -> str:
    """
    Конвертирует Markdown в HTML для Telegram.
    Источник: https://core.telegram.org/bots/api#html-style

    Поддерживаемые теги Telegram HTML:
    - <b>bold</b>, <i>italic</i>, <u>underline</u>, <s>strikethrough</s>
    - <code>inline code</code>, <pre>code block</pre>
    - <a href="url">link</a>, <tg-spoiler>spoiler</tg-spoiler>
    """
    if not text:
        return ""

    # 0) Очистка LaTeX-формул — Gemini иногда отвечает с $...$
    text = clean_latex(text)

    # 1) Таблицы Markdown: оборачиваем в <pre> для моноширинного вывода
    table_blocks = []

    def wrap_table(match):
        table = match.group(0).strip("\n")
        lines = [line for line in table.splitlines() if line.strip()]

        def normalize_cell(value: str) -> str:
            # Убираем markdown-выделения, чтобы не ломать выравнивание
            value = re.sub(r"(\*\*|__)(.*?)\1", r"\2", value)
            value = re.sub(r"(\*|_)(.*?)\1", r"\2", value)
            value = value.replace("`", "")
            value = re.sub(r"\s+", " ", value).strip()
            return value

        rows = []
        for line in lines:
            parts = [normalize_cell(p) for p in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", p) for p in parts):
                continue
            rows.append(parts)

        if not rows:
            placeholder = f"%%TABLEBLOCK{len(table_blocks)}%%"
            table_blocks.append("<pre></pre>")
            return placeholder

        cols_count = max(len(r) for r in rows)
        widths = [0] * cols_count
        for row in rows:
            row.extend([""] * (cols_count - len(row)))
            for idx, cell in enumerate(row):
                widths[idx] = max(widths[idx], len(cell))

        aligned_lines = []
        for row in rows:
            padded_cells = []
            for idx, cell in enumerate(row):
                padded_cell = cell.ljust(widths[idx])
                padded_cells.append(escape_html(padded_cell))
            aligned_lines.append(" | ".join(padded_cells))

        placeholder = f"%%TABLEBLOCK{len(table_blocks)}%%"
        joined_lines = "\n".join(aligned_lines)
        table_blocks.append(f"<pre>{joined_lines}</pre>")
        return placeholder

    # Паттерн для таблиц: строки с | в начале и конце
    table_pattern = r"(?:^\|.+\|$\n?)+"
    text = re.sub(table_pattern, wrap_table, text, flags=re.MULTILINE)

    # 2) Разбиваем на код-блоки, чтобы не трогать их содержимое
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)

    result_parts = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # Код-блок или inline code
            if part.startswith("```"):
                # Многострочный код-блок: снимаем ``` и язык
                code_match = re.match(r"```(\w*)\n?([\s\S]*?)```", part)
                if code_match:
                    lang = code_match.group(1)
                    code = code_match.group(2).rstrip()
                    code = escape_html(code)
                    if lang:
                        result_parts.append(
                            f'<pre><code class="language-{lang}">{code}</code></pre>'
                        )
                    else:
                        result_parts.append(f"<pre>{code}</pre>")
                else:
                    result_parts.append(f"<pre>{escape_html(part[3:-3])}</pre>")
            else:
                # Inline code
                code = part[1:-1]
                code = escape_html(code)
                result_parts.append(f"<code>{code}</code>")
        else:
            # Обычный текст: применяем форматирование
            fragment = part

            # Экранируем HTML-спецсимволы
            fragment = escape_html(fragment)

            # 3) Заголовки: ### Header -> <b>Header</b>
            fragment = re.sub(
                r"^\s*#{1,6}\s+(.*?)\s*$", r"<b>\1</b>\n", fragment, flags=re.MULTILINE
            )

            # 4) Жирный: **text** -> <b>text</b>
            # Используем [^*]+ вместо.*? для корректной работы с кавычками
            fragment = re.sub(
                r"\*\*([^*]+(?:\*(?!\*)[^*]*)*)\*\*", r"<b>\1</b>", fragment
            )

            # 5) Курсив: *text* или _text_ -> <i>text</i>
            fragment = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", fragment)
            fragment = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", fragment)

            # 6) Зачёркнутый: ~~text~~ -> <s>text</s>
            fragment = re.sub(r"~~(.*?)~~", r"<s>\1</s>", fragment)

            # 7) Списки: * item или - item -> • item
            fragment = re.sub(r"^\s*[\*\-]\s+", "• ", fragment, flags=re.MULTILINE)

            # 8) Ссылки: [text](url) -> <a href="url">text</a>
            def replace_link(match):
                link_text = match.group(1)
                url = match.group(2).replace('"', "&quot;")
                return f'<a href="{url}">{link_text}</a>'

            fragment = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace_link, fragment)

            result_parts.append(fragment)

    result_text = "".join(result_parts)
    for i, block in enumerate(table_blocks):
        result_text = result_text.replace(f"%%TABLEBLOCK{i}%%", block)
    return result_text


def split_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list:
    """Разбивает длинный текст на части"""
    if not text:
        return [""]
    if len(text) <= max_length:
        return [text]

    parts = []
    current_part = ""
    paragraphs = text.split("\n\n")

    for paragraph in paragraphs:
        if len(paragraph) > max_length:
            if current_part:
                parts.append(current_part.strip())
                current_part = ""
            lines = paragraph.split("\n")
            for line in lines:
                if len(line) > max_length:
                    for i in range(0, len(line), max_length):
                        parts.append(line[i : i + max_length])
                elif len(current_part) + len(line) + 1 > max_length:
                    parts.append(current_part.strip())
                    current_part = line + "\n"
                else:
                    current_part += line + "\n"
        elif len(current_part) + len(paragraph) + 2 > max_length:
            parts.append(current_part.strip())
            current_part = paragraph + "\n\n"
        else:
            current_part += paragraph + "\n\n"

    if current_part.strip():
        parts.append(current_part.strip())

    return parts if parts else [text[:max_length]]


async def send_safe_message(update: Update, text: str):
    """Отправляет сообщение с HTML форматированием, разбивает длинные"""
    if not text:
        text = "Пустой ответ от API"

    parts = split_message(text, MAX_MESSAGE_LENGTH)

    for i, part in enumerate(parts):
        part = format_for_telegram(part)
        if len(parts) > 1:
            part = f"📄 [{i + 1}/{len(parts)}]\n\n{part}"

        try:
            await update.message.reply_text(
                part,
                parse_mode="HTML",
                reply_to_message_id=update.message.message_id if i == 0 else None,
            )
        except Exception:
            try:
                await update.message.reply_text(
                    part,
                    reply_to_message_id=update.message.message_id if i == 0 else None,
                )
            except Exception as e2:
                log_error("SEND", str(e2))


async def send_with_retry(chat, text: str, retries: int = MAX_RETRIES):
    """Отправляет в Gemini с повторными попытками (новый SDK)"""
    last_error = None
    for attempt in range(retries + 1):
        try:
            # Используем chat.send_message: нужны Thought Signatures для Gemini 3
            # Источник: https://ai.google.dev/gemini-api/docs/thought-signatures
            response = await asyncio.wait_for(
                asyncio.to_thread(chat.send_message, text),
                timeout=TIMEOUT_MEDIUM,  # Увеличен таймаут для поиска
            )
            if response and response.text and response.text.strip():
                return response
            last_error = RuntimeError("Пустой ответ от API")
            if attempt < retries:
                await asyncio.sleep(2)
                continue
            raise last_error
        except asyncio.TimeoutError:
            last_error = TimeoutError("Превышено время ожидания ответа от Gemini")
            if attempt < retries:
                await asyncio.sleep(2)
                continue
            raise last_error
        except Exception as e:
            last_error = e
            error_str = str(e)
            if any(code in error_str for code in ["429", "503", "500"]):
                if attempt < retries:
                    wait_time = (attempt + 1) * 3
                    logger.warning(f"Retry {attempt + 1}/{retries} через {wait_time}с")
                    await asyncio.sleep(wait_time)
                    continue
            raise e
    if last_error is None:
        raise RuntimeError("Пустой ответ от API без ошибки")
    raise last_error


# --- ФУНКЦИИ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ ---


async def generate_image(prompt: str, context, user_id: int) -> tuple[bytes, str]:
    """
    Генерирует изображение по промту через Gemini.
    Источник: https://ai.google.dev/gemini-api/docs/image-generation
    """
    model_key = get_user_image_model(user_id, context)
    model_name = IMAGE_MODELS[model_key]

    try:
        # Генерация изображения - по документации просто contents
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=model_name, contents=prompt
                )
            ),
            timeout=TIMEOUT_LONG,
        )

        for part in response.parts:
            if part.inline_data is not None:
                return part.inline_data.data, model_key

        raise ValueError("API не вернул изображение")

    except asyncio.TimeoutError:
        raise TimeoutError(f"Превышено время генерации ({TIMEOUT_LONG} сек)")


async def edit_image(
    images_bytes: list[bytes], prompt: str, user_id: int, model_key: str = "pro"
) -> tuple[bytes, str]:
    """
    Редактирует изображение(я) через Gemini Image API.
    Принимает список байтов (одно или несколько фото) и текстовый промпт.
    """
    model_name = IMAGE_MODELS.get(model_key, IMAGE_MODELS["pro"])
    pil_images = []

    try:
        for img_bytes in images_bytes:
            pil_images.append(Image.open(io.BytesIO(img_bytes)))

        contents = pil_images + [prompt]

        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=model_name, contents=contents
                )
            ),
            timeout=TIMEOUT_LONG,
        )

        if not response or not response.parts:
            raise ValueError("API вернул пустой ответ")

        for part in response.parts:
            if part.inline_data is not None:
                return part.inline_data.data, model_key

        raise ValueError(
            "API не вернул данные изображения (проверьте безопасность промпта)"
        )

    except asyncio.TimeoutError:
        raise TimeoutError(f"Превышено время редактирования ({TIMEOUT_LONG} сек)")
    finally:
        for img in pil_images:
            try:
                img.close()
            except Exception:
                pass


async def handle_image_generation(update: Update, context, prompt: str, user_id: int):
    """Общая функция генерации изображения (устраняет дублирование)"""
    log_memory("image_gen:start", user_id)
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="upload_photo"
    )
    model_key = get_user_image_model(user_id, context)
    model_icon = "💎" if model_key == "pro" else "⚡"
    thinking_msg = await update.message.reply_text(
        f"🎨 {model_icon} Генерирую изображение...",
        reply_to_message_id=update.message.message_id,
    )

    result_data = None

    try:
        result_data, used_model = await generate_image(prompt, context, user_id)
        await thinking_msg.delete()

        # Сначала текст с названием модели
        model_text = f"Модель: {used_model.capitalize()}{model_icon}"
        await update.message.reply_text(
            model_text, reply_to_message_id=update.message.message_id
        )

        # Сохраняем промпт для возможной перегенерации
        context.user_data["last_image_prompt"] = prompt

        # Кнопки под картинкой
        image_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔄 Ещё", callback_data="img_regen"),
                    InlineKeyboardButton(
                        "✏️ Изменить запрос", callback_data="img_change_prompt"
                    ),
                ]
            ]
        )

        # Потом сама картинка с кнопками
        sent_photo = await update.message.reply_photo(
            photo=result_data,
            reply_markup=image_keyboard,
            reply_to_message_id=update.message.message_id,
        )
        sent_file_id = get_sent_photo_file_id(sent_photo)
        if sent_file_id:
            context.user_data["last_generated_photo"] = make_telegram_media_ref(
                sent_file_id
            )

        # Логируем активность
        log_activity(user_id, update.effective_user.username, "img_gen", prompt[:30])
        log_memory("image_gen:done", user_id)

    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("IMAGE_GEN", str(e), user_id)
        error_msg = format_gemini_error(e, "IMAGE_GEN")
        await update.message.reply_text(
            error_msg, parse_mode="HTML", reply_to_message_id=update.message.message_id
        )
    finally:
        result_data = None
        gc_collect_after_media("image_gen:gc", user_id)


# --- YOUTUBE SUMMARIZER ---


def extract_video_id(url: str) -> str | None:
    """Извлекает video_id из YouTube ссылки"""
    patterns = [
        r"(?:youtube\.com\/watch\?v=)([\w-]+)",
        r"(?:youtu\.be\/)([\w-]+)",
        r"(?:youtube\.com\/embed\/)([\w-]+)",
        r"(?:youtube\.com\/shorts\/)([\w-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


async def get_youtube_preview(url: str, app: Application) -> dict:
    """
    Получает превью и название YouTube видео через oEmbed API.
    Использует общий httpx.AsyncClient вместо requests из async-кода.
    """
    video_id = extract_video_id(url)
    if not video_id:
        return {"success": False, "error": "🔗 Не удалось распознать ссылку YouTube"}

    try:
        client = await get_http_client(app)
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        response = await client.get(oembed_url)
        response.raise_for_status()
        data = response.json()

        thumbnail_url = (
            data.get("thumbnail_url")
            or f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
        )

        return {
            "success": True,
            "title": data.get("title", "Без названия"),
            "author": data.get("author_name", "YouTube"),
            "thumbnail_url": thumbnail_url,
            "original_url": url,
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"success": False, "error": "🔒 Видео не найдено или недоступно"}
        if e.response.status_code == 401:
            return {"success": False, "error": "🔞 Видео с ограниченным доступом"}
        return {
            "success": False,
            "error": f"❌ Ошибка YouTube API: {e.response.status_code}",
        }
    except httpx.TimeoutException:
        return {
            "success": False,
            "error": "⏱️ Превышено время ожидания ответа от YouTube",
        }
    except httpx.TransportError:
        return {"success": False, "error": "🌐 Ошибка подключения к YouTube"}
    except Exception as e:
        logger.error(f"YouTube Preview: {sanitize_error(e)}")
        return {"success": False, "error": f"❌ Ошибка: {sanitize_error(e, 100)}"}


def get_transcript(video_id: str) -> dict:
    """
    Получает субтитры видео
    Источник: https://pypi.org/project/youtube-transcript-api/
    Возвращает: dict с ключами 'success', 'text' или 'error', 'error_type'
    """
    try:
        ytt_api = YouTubeTranscriptApi()

        # Пробуем получить субтитры на русском или английском
        try:
            fetched_transcript = ytt_api.fetch(video_id, languages=["ru", "en"])
            full_text = " ".join(
                [snippet["text"] for snippet in fetched_transcript.to_raw_data()]
            )
            logger.info(
                f"YouTube: Субтитры ({fetched_transcript.language_code}), {len(full_text)} символов"
            )
            return {
                "success": True,
                "text": full_text,
                "language": fetched_transcript.language_code,
            }
        except Exception as e:
            # Если не нашли ru/en, получаем дефолтные
            logger.debug(f"Языки ru/en недоступны, пробуем дефолтные: {e}")
            fetched_transcript = ytt_api.fetch(video_id)
            full_text = " ".join(
                [snippet["text"] for snippet in fetched_transcript.to_raw_data()]
            )
            logger.info(
                f"YouTube: Субтитры ({fetched_transcript.language_code}), {len(full_text)} символов"
            )
            return {
                "success": True,
                "text": full_text,
                "language": fetched_transcript.language_code,
            }

    except Exception as e:
        error_str = str(e).lower()
        logger.error(f"YouTube: Ошибка получения субтитров: {e}")

        # Классификация ошибок для понятного отображения пользователю
        # Источник: https://github.com/jdepoix/youtube-transcript-api#exceptions
        if "subtitles are disabled" in error_str or "disabled" in error_str:
            return {
                "success": False,
                "error": "🚫 Субтитры отключены автором видео",
                "error_type": "disabled",
            }
        elif "no transcript" in error_str or "could not retrieve" in error_str:
            return {
                "success": False,
                "error": "📭 Субтитры недоступны для этого видео",
                "error_type": "not_available",
            }
        elif "video unavailable" in error_str or "video is unavailable" in error_str:
            return {
                "success": False,
                "error": "🔒 Видео недоступно (удалено или приватное)",
                "error_type": "video_unavailable",
            }
        elif "age restricted" in error_str or "age-restricted" in error_str:
            return {
                "success": False,
                "error": "🔞 Видео с возрастным ограничением",
                "error_type": "age_restricted",
            }
        elif (
            "connection" in error_str
            or "timeout" in error_str
            or "network" in error_str
        ):
            return {
                "success": False,
                "error": f"🌐 Ошибка сети: {str(e)[:80]}",
                "error_type": "network",
            }
        else:
            # Техническая ошибка скрипта — показываем полную информацию
            return {
                "success": False,
                "error": f"Техническая ошибка: {str(e)[:150]}",
                "error_type": "script_error",
            }


async def create_summary(text: str) -> str:
    """
    Создаёт саммари через Gemini Flash модель
    Всегда использует Flash для скорости обработки
    """
    # Обрезаем если слишком длинный
    if len(text) > 30000:
        text = text[:30000] + "..."
        logger.warning("YouTube: Текст обрезан до 30000 символов")

    prompt = f"""Создай структурированное саммари видео на русском языке:

📌 **Основная тема**: (1-2 предложения)

📋 **Ключевые моменты**:
• пункт 1
• пункт 2
• пункт 3
...

💡 **Главные выводы**: (2-3 предложения)

Текст субтитров:
{text}"""

    try:
        # Используем Flash модель для саммаризации
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS["flash"],
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION_FLASH,
                        tools=SEARCH_TOOLS
                    ),
                )
            ),
            timeout=TIMEOUT_SHORT,
        )

        return response.text

    except asyncio.TimeoutError:
        return f"⏱️ [GEMINI TIMEOUT] Превышено время генерации ({TIMEOUT_SHORT} сек)"
    except Exception as e:
        error_str = str(e).lower()
        error_full = str(e)
        logger.error(f"YouTube: Ошибка генерации саммари: {e}")

        # Классификация ошибок Gemini API для понятного отображения
        # Источник: https://ai.google.dev/gemini-api/docs/troubleshooting
        if "quota" in error_str or "rate limit" in error_str or "429" in error_str:
            return f"🚦 [GEMINI QUOTA] Превышен лимит запросов. Попробуй позже.\n`{error_full[:100]}`"
        elif "blocked" in error_str or "safety" in error_str or "harmful" in error_str:
            return f"🛡️ [GEMINI SAFETY] Контент заблокирован фильтром безопасности.\n`{error_full[:100]}`"
        elif (
            "api key" in error_str
            or "invalid" in error_str
            or "401" in error_str
            or "403" in error_str
        ):
            return f"🔑 [GEMINI AUTH] Проблема с API ключом.\n`{error_full[:150]}`"
        elif "model" in error_str and (
            "not found" in error_str or "unavailable" in error_str
        ):
            return f"🤖 [GEMINI MODEL] Модель недоступна.\n`{error_full[:100]}`"
        elif (
            "connection" in error_str
            or "timeout" in error_str
            or "network" in error_str
        ):
            return f"🌐 [GEMINI NETWORK] Ошибка сети.\n`{error_full[:100]}`"
        elif "500" in error_str or "503" in error_str or "internal" in error_str:
            return f"💥 [GEMINI SERVER] Ошибка сервера Google.\n`{error_full[:100]}`"
        else:
            # Неизвестная ошибка — показываем полностью для отладки
            return f"[GEMINI ERROR] {error_full[:200]}"


async def summarize_youtube(url: str) -> dict:
    """
    Главная функция - возвращает результат саммаризации
    """
    video_id = extract_video_id(url)

    if not video_id:
        return {"success": False, "error": "🔗 Не удалось распознать ссылку YouTube"}

    transcript_result = get_transcript(video_id)

    # Проверяем результат получения субтитров
    if not transcript_result["success"]:
        return {
            "success": False,
            "error": transcript_result["error"],
            "error_type": transcript_result.get("error_type"),
        }

    summary = await create_summary(transcript_result["text"])

    return {
        "success": True,
        "summary": summary,
        "language": transcript_result.get("language"),
    }


# --- ОБРАБОТЧИКИ КОМАНД ---


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id):
        # Уведомление для неавторизованных пользователей
        message = (
            "Привет! Вы можете сделать такого же бота бесплатно, "
            'по моему <a href="https://t.me/ChoronoNotes/107">гайду</a>. '
            "Или написать мне в канал, я помогу."
        )
        return await update.message.reply_text(
            message, parse_mode="HTML", disable_web_page_preview=True
        )
    reset_session(context)
    model_key = get_model_key(context)
    model_id = MODELS.get(model_key, "unknown")
    model_icon = "💎" if model_key == "pro" else "⚡"
    
    await update.message.reply_text(
        f"🔄 Контекст сброшен!\n{model_icon} Модель: <b>{model_key.upper()}</b>\nID: <code>{model_id}</code>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статус бота (только для админа)"""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return  # Только админ видит статус

    model_key = get_model_key(context)
    model_name = MODELS[model_key]
    has_session = "chat_session" in context.user_data
    last_time = context.user_data.get("last_activity", 0)

    uptime_sec = int(time.time() - bot_stats["start_time"])
    uptime_hours = uptime_sec // 3600
    uptime_min = (uptime_sec % 3600) // 60

    # Системная статистика
    cpu_usage = psutil.cpu_percent()
    ram = psutil.virtual_memory()
    # Кросс-платформенный путь диска
    disk_path = "C:" if platform.system() == "Windows" else "/"
    disk = psutil.disk_usage(disk_path)

    # Конвертация байт в ГБ (оставляем 3 знака для максимальной точности)
    ram_total_gb = f"{ram.total / (1024**3):.3f}"
    ram_used_gb = f"{ram.used / (1024**3):.3f}"
    process_rss_mb = get_process_rss_mb()

    disk_total_gb = f"{disk.total / (1024**3):.2f}"
    disk_used_gb = f"{disk.used / (1024**3):.2f}"

    if last_time:
        minutes_ago = int((time.time() - last_time) / 60)
        activity_text = f"{minutes_ago} мин. назад" if minutes_ago > 0 else "только что"
    else:
        activity_text = "нет данных"

    status_text = f"""📊 **Статус**

🤖 Модель: **{model_key.upper()}**
{model_name}
"""

    if user_id == ADMIN_ID:
        ensure_daily_counters()
        actions_today = daily_counters["actions"]

        # Считаем именно запросы к AI (текст, голос, фото, инлайн)
        ai_actions = [
            "text",
            "voice",
            "img_gen",
            "img_regen",
            "img_analyze",
            "img_analyze_btn",
            "img_analyze_prompt",
            "img_edit",
            "img_edit_btn_done",
            "img_edit_album",
            "img_edit_regen",
            "inline",
            "inline_translate",
            "inline_youtube",
            "youtube_summary",
            "translate",
        ]
        today_requests_count = sum(
            actions_today.get(action, 0) for action in ai_actions
        )
        cleanup_stats = bot_stats["cleanup"]
        cleanup_last_run = cleanup_stats.get("last_run", 0)
        if cleanup_last_run:
            cleanup_last_run_text = (
                f"{int((time.time() - cleanup_last_run) / 60)} мин. назад"
            )
        else:
            cleanup_last_run_text = "ещё не запускался"
        cleanup_removed_total = sum(
            cleanup_stats.get(key, 0)
            for key in [
                "photo_task",
                "pending_albums",
                "pending_tweet",
                "active_image",
                "last_generated_photo",
                "last_edit_data",
                "chat_session",
                "temp_files",
            ]
        )
        active_chat_sessions = sum(
            1
            for user_data in context.application.user_data.values()
            if "chat_session" in user_data
        )
        temp_files_count, temp_files_size = get_temp_dir_stats()
        temp_files_size_mb = temp_files_size / (1024 * 1024)

        status_text += f"""
━━━━━━━━━━━━━━━━━━━━
💻 **Сервер** ({platform.system()})

🖥 CPU: {cpu_usage}%
💾 RAM: {ram_used_gb} / {ram_total_gb} GB ({ram.percent}%)
🧠 RSS бота: {process_rss_mb:.1f} MB
💿 Disk: {disk_used_gb} / {disk_total_gb} GB ({disk.percent}%)

━━━━━━━━━━━━━━━━━━━━
📈 Запросы сегодня: **{today_requests_count} / 1500**
❌ Ошибок: {bot_stats["errors_count"]}
🧠 Активных сессий: {active_chat_sessions}
"""
        if temp_files_count > 0:
            status_text += f"📁 Temp: {temp_files_count} файлов / {temp_files_size_mb:.1f} MB\n"

        if bot_stats["last_errors"]:
            status_text += "\n📋 **Последние ошибки:**\n"
            for err in bot_stats["last_errors"][-5:]:
                err_msg = err["msg"][:40] if err["msg"] else "unknown"
                status_text += f"`{err['time']}` {err['type']}: {err_msg}\n"

        # Статистика за сегодняшний день
        user_stats = daily_counters["users"]

        # Текущее время и дата по Киеву
        now_kyiv = datetime.now(KYIV_TZ)
        now_date = now_kyiv.strftime("%d.%m.%Y")
        now_time = now_kyiv.strftime("%H:%M")

        status_text += "\n━━━━━━━━━━━━━━━━━━━━\n"
        status_text += f"📅 **{now_date}** (Киев {now_time})\n\n"

        if user_stats:
            for uid, stats in user_stats.items():
                username = (
                    f"@{stats['username']}"
                    if stats["username"] != "Unknown"
                    else f"ID:{uid}"
                )
                total = sum(
                    [
                        stats["text"],
                        stats["voice"],
                        stats["img_gen"],
                        stats["img_analyze"],
                        stats["img_edit"],
                    ]
                )

                if total > 0:
                    status_text += f"👤 {username}: **{total}** действий\n"
                    if stats["text"] > 0:
                        status_text += f"   💬 Текст: {stats['text']}\n"
                    if stats["voice"] > 0:
                        status_text += f"   🎤 Голос: {stats['voice']}\n"
                    if stats["img_gen"] > 0:
                        status_text += f"   🖼️ Генерация: {stats['img_gen']}\n"
                    if stats["img_analyze"] > 0:
                        status_text += f"   🔍 Анализ: {stats['img_analyze']}\n"
                    if stats["img_edit"] > 0:
                        status_text += f"   ✏️ Редактирование: {stats['img_edit']}\n"
        else:
            status_text += "Нет активности за сегодня\n"

    await update.message.reply_text(format_for_telegram(status_text), parse_mode="HTML")


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        return await update.message.reply_text("Пример: /add 123456")
    try:
        new_id = int(context.args[0])
        allowed_users.add(new_id)
        save_users()
        await update.message.reply_text(f"✅ ID {new_id} добавлен.")
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")


async def del_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        return await update.message.reply_text("Пример: /del 123456")
    try:
        target_id = int(context.args[0])
        if target_id in allowed_users:
            allowed_users.remove(target_id)
            save_users()
            # context.user_data автоматически управляется telegram-bot
            await update.message.reply_text(f"🚫 ID {target_id} удален.")
        else:
            await update.message.reply_text("Нет в списке.")
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш ID: <code>{update.effective_user.id}</code>", parse_mode="HTML"
    )


async def set_pro_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")

    context.user_data["model"] = "pro"
    reset_session(context)
    model_id = MODELS.get("pro", "unknown")

    await update.message.reply_text(
        f"✅ Модель переключена на <b>Pro</b>\n"
        f"Настоящая модель: <code>{model_id}</code>\n\n"
        "⚠️ Установлена мощная модель для глубокого анализа.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


async def set_flash_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    context.user_data["model"] = "flash"
    reset_session(context)
    model_id = MODELS.get("flash", "unknown")
    await update.message.reply_text(
        f"✅ Модель переключена на <b>Flash</b>\n"
        f"Настоящая модель: <code>{model_id}</code>\n"
        f"🌐 Подключенные инструменты: Google Search, URL Context", 
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


async def youtube_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает режим YouTube саммари"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")

    context.user_data["mode"] = "youtube_mode"
    await update.message.reply_text(
        "📺 Отправьте ссылку на YouTube видео:",
        reply_to_message_id=update.message.message_id,
    )
    log_activity(
        user_id, update.effective_user.username, "youtube_cmd", "Режим активирован"
    )


# --- КОМАНДЫ ДЛЯ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ ---


async def set_image_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает на Pro модель и активирует режим генерации изображений"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")

    uid_str = str(user_id)
    if uid_str not in user_settings:
        user_settings[uid_str] = {}
    user_settings[uid_str]["image_model"] = "pro"
    save_user_settings()

    context.user_data["image_model"] = "pro"
    context.user_data.pop("mode", None)

    await update.message.reply_text(
        "🎨 <b>Image Pro</b>\n\n"
        "💎 Установлена модель Pro высокого качества.\n"
        "⚠️ _Примечание: на бесплатном тарифе (Free Tier) может выдавать ошибку квоты._",
        parse_mode="HTML",
    )
    log_activity(
        user_id, update.effective_user.username, "image_pro_mode", "установлена вручную"
    )


async def set_image_flash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает на Flash модель и активирует режим генерации изображений"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    uid_str = str(user_id)
    if uid_str not in user_settings:
        user_settings[uid_str] = {}
    user_settings[uid_str]["image_model"] = "flash"
    save_user_settings()

    context.user_data["image_model"] = "flash"
    context.user_data.pop("mode", None)
    await update.message.reply_text(
        f"🎨 Глобальная модель для изображения:\n⚡ <b>Flash</b> {IMAGE_MODELS['flash']}",
        parse_mode="HTML",
    )
    log_activity(
        user_id,
        update.effective_user.username,
        "image_flash_mode",
        "установлена глобально",
    )


# --- ПОМОЩЬ ---


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """🤖 **Справка**

**📋 Команды:**
/start - Сбросить контекст
/status - Статус бота
/1model - 💎 Gemini Pro
/2model - ⚡ Gemini Flash

**⚡ Быстрые команды:**
• **П** — Gemini Pro | **Ф** — Gemini Flash
• **Пр** + текст — 🌐 Перевод на русский
• **Ю** + ссылка — 📺 YouTube саммари
• **Превью** + ссылка — 🖼️ YouTube превью
• **К** + описание — 🎨 Генерация картинки
• **Р** — ✏️ Режим редактирования фото

**🔍 Инлайн-режим** (в любом чате):
• @bot **пр** hello — перевод
• @bot **ю** ссылка — саммари
• @bot **превью** ссылка — превью
• @bot вопрос — ответ Gemini

**🖼️ Изображения:**
/imagepro - 💎 Pro | /imageflash - ⚡ Flash
• Отправьте фото → кнопки Анализировать | ✏️ Редактировать
• 📷 Альбом (2-10 фото) → поддержка нескольких изображений
• Фото + подпись → мгновенный ответ

**🐦 Соцсети и Видео:**
• **X/Twitter:** Отправь ссылку на твит → бот загрузит текст и медиа. Можно нажать «Обсудить» и задать вопрос по твиту.
• **YouTube:** Отправь ссылку с приставкой **Ю** → бот сделает саммари. После этого можно задавать вопросы по видео.

**📄 Документы:** PDF, TXT, CSV, JSON → суммаризация

**⏱ Сброс:**
**.** — полный сброс | **выход** — выход из режима
🎙️ Голос → текст (Flash)

**👤 Админ:** /add ID /del ID"""
    await update.message.reply_text(format_for_telegram(help_text), parse_mode="HTML")


# --- ОБРАБОТЧИК ГОЛОСА ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")

    voice = update.message.voice
    if voice.file_size and voice.file_size > MAX_VOICE_BYTES:
        return await update.message.reply_text(
            f"Голосовое слишком большое ({voice.file_size / (1024 * 1024):.1f} МБ). Лимит: {MAX_VOICE_BYTES // (1024 * 1024)} МБ."
        )

    bot_stats["voice_count"] += 1
    log_memory("voice:start", user_id)
    thinking_msg = await update.message.reply_text("🎤 Слушаю...")
    voice_data = None
    voice_temp_path = None

    try:
        voice_file = await voice.get_file()
        voice_temp_path = await download_telegram_file_to_temp(voice_file, ".ogg")
        voice_data = await asyncio.to_thread(read_binary_file, voice_temp_path)

        # Используем Flash для распознавания речи (быстрее)

        # Шаг 1: Распознаём речь в текст
        recognition_response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS["flash"],
                    contents=[
                        "Распознай речь в текст. Выведи ТОЛЬКО распознанный текст, без комментариев:",
                        genai_types.Part.from_bytes(
                            data=voice_data, mime_type="audio/ogg"
                        ),
                    ],
                )
            ),
            timeout=60.0,
        )

        recognized_text = (
            recognition_response.text
            if recognition_response and recognition_response.text
            else None
        )

        if not recognized_text or recognized_text.strip() == "":
            await thinking_msg.delete()
            await update.message.reply_text("Не удалось распознать речь")
            log_activity(user_id, update.effective_user.username, "voice_failed", "")
            return

        # Шаг 2: Отправляем распознанный текст в сессию чата пользователя
        # Это сохранит контекст для последующих текстовых вопросов
        chat = get_or_create_session(context)

        # Отправляем в чат с повторными попытками
        response = await send_with_retry(chat, recognized_text)
        increment_chat_message_count(context)

        await thinking_msg.delete()

        # Проверка на пустой ответ
        response_text = (
            response.text if response and response.text else "Пустой ответ от API"
        )

        # Формируем финальный ответ с показом распознанного текста
        final_text = f"🎤 *Распознано:* {recognized_text}\n\n{response_text}"
        await send_safe_message(update, final_text)

        # Логируем активность
        log_activity(
            user_id, update.effective_user.username, "voice", recognized_text[:30]
        )
        log_memory("voice:done", user_id)

    except asyncio.TimeoutError:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("VOICE_TIMEOUT", "Таймаут", user_id)
        await update.message.reply_text(
            "Превышено время ожидания.", reply_to_message_id=update.message.message_id
        )

    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("VOICE", str(e), user_id)
        error_msg = format_gemini_error(e, "VOICE")
        await update.message.reply_text(
            error_msg, parse_mode="HTML", reply_to_message_id=update.message.message_id
        )

        if user_id != ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🚨 Voice Error\nUser: {user_id}\n<code>{str(e)[:200]}</code>",
                    parse_mode="HTML",
                )
            except Exception as notify_err:
                logger.debug(f"Не удалось уведомить админа: {notify_err}")
    finally:
        voice_data = None
        safe_delete_file(voice_temp_path)
        gc_collect_after_media("voice:gc", user_id)


# --- ОБРАБОТЧИК ФОТО (РЕДАКТИРОВАНИЕ) ---


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Диспетчер фото и альбомов.
    Определяет режим (анализ/перевод/редактирование) и делегирует обработку.
    """
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")

    caption = update.message.caption or ""
    caption_lower = caption.strip().lower()
    media_group_id = update.message.media_group_id
    log_memory("photo:start", user_id)

    # --- Обработка пересланных сообщений ---
    if getattr(update.message, "forward_origin", None):
        if media_group_id and context.user_data.get("last_forwarded_album") == media_group_id:
            return

        if media_group_id:
            context.user_data["last_forwarded_album"] = media_group_id

        context.user_data["forwarded_context"] = {
            "type": "photo",
            "photo_id": update.message.photo[-1].file_id,
            "text": caption
        }
        await update.message.reply_text("✅ Принято. Жду ваш вопрос.")
        return

    # --- Обработка альбомов (media_group) ---
    # Если это часть альбома — собираем все фото
    if media_group_id:
        # Для альбомов храним только Telegram file_id, не bytes.
        photo = update.message.photo[-1]
        photo_ref = make_telegram_media_ref(photo.file_id)
        log_memory("album_photo:stored_ref", user_id)

        # Проверяем, есть ли уже данные об этом альбоме
        if media_group_id not in pending_albums:
            # Первое фото альбома — создаём запись
            pending_albums[media_group_id] = {
                "photos": [photo_ref],
                "caption": caption,
                "user_id": user_id,
                "chat_id": update.effective_chat.id,
                "message_id": update.message.message_id,
                "timestamp": time.time(),
            }

            # Запускаем отложенную обработку альбома
            asyncio.create_task(process_album_delayed(media_group_id, update, context))
            return
        else:
            # Дополнительное фото — добавляем к альбому
            if len(pending_albums[media_group_id]["photos"]) < MAX_ALBUM_PHOTOS:
                pending_albums[media_group_id]["photos"].append(photo_ref)
            # Обновляем caption если первое было пустым
            if not pending_albums[media_group_id]["caption"] and caption:
                pending_albums[media_group_id]["caption"] = caption
            return

    # --- Одиночное фото (без media_group_id) ---

    # Проверяем режим перевода -> перевод текста на изображении
    if context.user_data.get("mode") == "translate" or caption_lower in [
        "перевод",
        "пр",
        "translate",
    ]:
        thinking_msg = await update.message.reply_text(
            "Перевожу текст на изображении...",
            reply_to_message_id=update.message.message_id,
        )

        photo_bytes = None
        result_data = None

        try:
            # Получаем фото
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            photo_bytes = prepare_image_for_gemini(
                bytes(await photo_file.download_as_bytearray())
            )
            log_memory("photo_translate:after_download", user_id)

            # Промпт для перевода прямо на изображении
            prompt = (
                "Translate all text in the image to Russian. "
                "Replace the original text in-place while preserving layout, "
                "font style, size, and colors as closely as possible. "
                "Keep the rest of the image unchanged. "
                "Return only the edited image."
            )

            # Для перевода на фото используем flash-image модель
            # Используем IMAGE_MODELS['flash'] (gemini-3.1-flash-image-preview)
            result_data, used_model = await edit_image(
                [photo_bytes], prompt, user_id, "flash"
            )

            await delete_safe(thinking_msg)

            # Сохраняем промпт для кнопок. Сам результат сохраним как file_id после отправки.
            context.user_data["last_image_prompt"] = prompt

            # Кнопки под переведенной картинкой
            translate_keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔄 Еще", callback_data="img_regen"),
                    ]
                ]
            )

            sent_photo = await update.message.reply_photo(
                photo=result_data,
                reply_markup=translate_keyboard,
                reply_to_message_id=update.message.message_id,
            )
            sent_file_id = get_sent_photo_file_id(sent_photo)
            if sent_file_id:
                context.user_data["last_generated_photo"] = make_telegram_media_ref(
                    sent_file_id
                )

            context.user_data.pop("mode", None)
            log_activity(
                user_id,
                update.effective_user.username,
                "img_translate_image",
                used_model,
            )
            return

        except Exception as e:
            log_error("IMAGE_TRANSLATE_EDIT", str(e), user_id)

            try:
                # Фоллбек: OCR + текстовый перевод (используем flash-модель для анализа)
                ocr_prompt = (
                    "Find all text in the image and translate it to Russian. "
                    "Output only the translation, no comments."
                )
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: gemini_client.models.generate_content(
                            model=MODELS["flash"],
                            contents=[
                                genai_types.Part.from_bytes(
                                    data=photo_bytes, mime_type="image/jpeg"
                                ),
                                ocr_prompt,
                            ],
                            config=genai_types.GenerateContentConfig(
                                system_instruction="Ты — переводчик. Твоя задача — точно перевести текст с изображения на русский язык.",
                            )
                        )
                    ),
                    timeout=60.0,
                )

                try:
                    await delete_safe(thinking_msg)
                except Exception:
                    pass

                response_text = (
                    response.text
                    if response and response.text
                    else "Не удалось распознать текст"
                )

                await send_safe_message(update, response_text)

                context.user_data.pop("mode", None)
                log_activity(
                    user_id,
                    update.effective_user.username,
                    "img_translate",
                    "OCR+translate",
                )
                return

            except Exception as fallback_error:
                try:
                    await delete_safe(thinking_msg)
                except Exception:
                    pass
                log_error("IMAGE_TRANSLATE_FALLBACK", str(fallback_error), user_id)
                error_msg = format_gemini_error(
                    fallback_error, "IMAGE_TRANSLATE_FALLBACK"
                )
                await send_safe_message(update, error_msg)
                context.user_data.pop("mode", None)
                return

        except asyncio.TimeoutError:
            try:
                await thinking_msg.delete()
            except Exception as del_err:
                logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
            log_error("IMAGE_TRANSLATE_TIMEOUT", "Таймаут", user_id)
            await update.message.reply_text(
                "Превышено время обработки.",
                reply_to_message_id=update.message.message_id,
            )
            context.user_data.pop("mode", None)
            return

        except Exception as e:
            try:
                await thinking_msg.delete()
            except Exception as del_err:
                logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
            log_error("IMAGE_TRANSLATE", str(e), user_id)
            await update.message.reply_text(
                f"Ошибка: <code>{escape_html(str(e)[:150])}</code>",
                parse_mode="HTML",
                reply_to_message_id=update.message.message_id,
            )
            context.user_data.pop("mode", None)
            return
        finally:
            photo_bytes = None
            result_data = None
            gc_collect_after_media("photo_translate:gc", user_id)

    if context.user_data.get("mode") == "awaiting_edit_photo":
        try:
            photo = update.message.photo[-1]
            photo_ref = make_telegram_media_ref(photo.file_id)
            log_memory("photo_edit_wait:stored_ref", user_id)

            # Сохраняем ссылку на фото и переходим в режим ожидания промпта
            context.user_data["photo_task"] = {
                "photos": [photo_ref],
                "message_id": update.message.message_id,
                "timestamp": time.time(),
            }
            context.user_data["mode"] = "awaiting_edit_prompt"

            model_key = get_user_image_model(user_id, context)
            model_icon = "💎" if model_key == "pro" else "⚡"

            await update.message.reply_text(
                f"📷 Фото получено! {model_icon}\n\n✏️ Опишите что нужно сделать с изображением:",
                reply_to_message_id=update.message.message_id,
            )
            log_activity(
                user_id,
                update.effective_user.username,
                "edit_photo_received",
                "awaiting prompt",
            )
            return
        except Exception as e:
            log_error("EDIT_PHOTO_RECEIVE", str(e), user_id)
            context.user_data.pop("mode", None)
            await update.message.reply_text(f"Ошибка: {str(e)[:100]}")
            return

    # Проверяем команду редактирования (Р/Редактировать)
    is_edit_short = caption_lower.startswith("р ") or caption_lower == "р"
    is_edit_long = (
        caption_lower.startswith("редактировать ") or caption_lower == "редактировать"
    )

    if is_edit_short or is_edit_long:
        # Логика редактирования остаётся ниже
        pass

    # Если фото без подписи и не в режиме перевода — предлагаем выбор (кнопки)
    if not (is_edit_short or is_edit_long):
        try:
            photo = update.message.photo[-1]
            photo_ref = make_telegram_media_ref(photo.file_id)
            log_memory("photo_menu:stored_ref", user_id)

            # Сохраняем ссылку на фото как СПИСОК (для совместимости с альбомами)
            context.user_data["photo_task"] = {
                "photos": [photo_ref],  # Список ссылок на изображения
                "caption": caption,  # Подпись к фото (используется при анализе)
                "message_id": update.message.message_id,
                "timestamp": time.time(),
            }

            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔍 Анализировать", callback_data="photo_analyze"
                    ),
                    InlineKeyboardButton("✏️ Редактировать", callback_data="photo_edit"),
                ],
                [
                    InlineKeyboardButton(
                        "📝 Добавить вопрос", callback_data="photo_add_caption"
                    )
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                "Что сделать с этим фото?",
                reply_markup=reply_markup,
                reply_to_message_id=update.message.message_id,
            )
            return
        except Exception as e:
            log_error("PHOTO_MENU", str(e), user_id)
            await update.message.reply_text("Ошибка при подготовке меню действий.")
            return

    # Извлекаем промт
    if is_edit_long:
        prompt = caption.strip()[13:].strip()  # "редактировать" = 13 символов
    else:
        prompt = caption.strip()[1:].strip()
    if not prompt:
        # Нет промта — сохраняем фото и переходим в режим ожидания промта
        try:
            photo = update.message.photo[-1]
            photo_ref = make_telegram_media_ref(photo.file_id)
            log_memory("photo_edit_prompt_wait:stored_ref", user_id)

            context.user_data["photo_task"] = {
                "photos": [photo_ref],
                "message_id": update.message.message_id,
                "timestamp": time.time(),
            }
            context.user_data["mode"] = "awaiting_edit_prompt"

            model_key = get_user_image_model(user_id, context)
            model_icon = "💎" if model_key == "pro" else "⚡"

            return await update.message.reply_text(
                f"📷 Фото получено! {model_icon}\n\n✏️ Опишите что нужно сделать с изображением:",
                reply_to_message_id=update.message.message_id,
            )
        except Exception as e:
            log_error("EDIT_PHOTO_SAVE", str(e), user_id)
            return await update.message.reply_text("Ошибка при сохранении фото")

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="upload_photo"
    )
    thinking_msg = await update.message.reply_text(
        "🎨 Редактирую изображение...", reply_to_message_id=update.message.message_id
    )

    photo_bytes = None
    result_data = None

    try:
        # Получаем фото (берём самое большое разрешение) только перед вызовом Gemini
        photo = update.message.photo[-1]
        photo_ref = make_telegram_media_ref(photo.file_id)
        photo_file = await photo.get_file()
        photo_bytes = prepare_image_for_gemini(
            bytes(await photo_file.download_as_bytearray())
        )
        log_memory("photo_edit_immediate:after_download", user_id)

        # Получаем модель для изображений
        model_key = get_user_image_model(user_id, context)
        model_icon = "💎" if model_key == "pro" else "⚡"

        # Редактируем
        result_data, used_model = await edit_image(
            [photo_bytes], prompt, user_id, model_key
        )
        await thinking_msg.delete()

        # Сохраняем данные для перегенерации
        context.user_data["last_edit_data"] = {
            "photos": [photo_ref],
            "prompt": prompt,
            "model_key": model_key,
            "timestamp": time.time(),
        }

        # Сначала текстовое сообщение с информацией
        await update.message.reply_text(
            f"{model_icon} Отредактировано через <b>{IMAGE_MODELS[used_model]}</b>\n\n✏️ Запрос: {prompt}",
            parse_mode="HTML",
            reply_to_message_id=update.message.message_id,
        )

        # Кнопки под отредактированной картинкой
        edit_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔄 Еще", callback_data="img_edit_regen"),
                    InlineKeyboardButton(
                        "✏️ Сменить промт", callback_data="img_edit_change_prompt"
                    ),
                ]
            ]
        )

        # Потом фото с кнопками
        await update.message.reply_photo(photo=result_data, reply_markup=edit_keyboard)

        # Логируем активность
        log_activity(
            user_id,
            update.effective_user.username,
            "img_edit",
            f"{used_model}: {prompt[:20]}",
        )

    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("IMAGE_EDIT", str(e), user_id)
        error_msg = format_gemini_error(e, "IMAGE_EDIT")
        await update.message.reply_text(
            error_msg, parse_mode="HTML", reply_to_message_id=update.message.message_id
        )
    finally:
        photo_bytes = None
        result_data = None
        gc_collect_after_media("photo_edit_immediate:gc", user_id)


async def process_album_delayed(
    media_group_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Отложенная обработка альбома после сбора всех фото."""
    # Ждём пока все фото альбома придут
    await asyncio.sleep(ALBUM_WAIT_TIME)

    # Получаем данные альбома
    if media_group_id not in pending_albums:
        return  # Альбом уже обработан или удалён

    album_data = pending_albums.pop(media_group_id)
    photos_refs = album_data["photos"]
    caption = album_data["caption"]
    user_id = album_data["user_id"]
    chat_id = album_data["chat_id"]
    message_id = album_data["message_id"]

    caption_lower = caption.strip().lower()
    photos_count = len(photos_refs)

    # Режим ожидания фото для редактирования (команда "р") для альбомов
    if context.user_data.get("mode") == "awaiting_edit_photo":
        context.user_data["photo_task"] = {
            "photos": photos_refs,
            "message_id": message_id,
            "timestamp": time.time(),
        }
        context.user_data["mode"] = "awaiting_edit_prompt"

        model_key = get_user_image_model(user_id, context)
        model_icon = "💎" if model_key == "pro" else "⚡"

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📷 Получено {photos_count} фото (альбом)! {model_icon}\n\n✏️ Опишите что нужно сделать с изображениями:",
            reply_to_message_id=message_id,
        )
        return

    # Проверяем команду редактирования (Р/Редактировать)
    is_edit_short = caption_lower.startswith("р ") or caption_lower == "р"
    is_edit_long = (
        caption_lower.startswith("редактировать ") or caption_lower == "редактировать"
    )

    if is_edit_short or is_edit_long:
        # Извлекаем промт
        if is_edit_long:
            prompt = caption.strip()[13:].strip()
        else:
            prompt = caption.strip()[1:].strip()

        if not prompt:
            # Нет промта — сохраняем альбом и переходим в режим ожидания промта
            context.user_data["photo_task"] = {
                "photos": photos_refs,
                "message_id": message_id,
                "timestamp": time.time(),
            }
            context.user_data["mode"] = "awaiting_edit_prompt"

            model_key = get_user_image_model(user_id, context)
            model_icon = "💎" if model_key == "pro" else "⚡"

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📷 Получено {photos_count} фото (альбом)! {model_icon}\n\n✏️ Опишите что нужно сделать с изображениями:",
                reply_to_message_id=message_id,
            )
            return

        # Редактируем альбом
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        thinking_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🎨 Редактирую изображение",
            reply_to_message_id=message_id,
        )

        photos_bytes = None
        result_data = None

        try:
            # Получаем модель для изображений
            model_key = get_user_image_model(user_id, context)
            model_icon = "💎" if model_key == "pro" else "⚡"

            photos_bytes = await resolve_media_items_to_bytes(context.bot, photos_refs)
            log_memory("album_edit:after_download", user_id)
            result_data, used_model = await edit_image(
                photos_bytes, prompt, user_id, model_key
            )
            await delete_safe(thinking_msg)

            # Сначала текстовое сообщение с информацией
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{model_icon} Отредактировано {photos_count} фото через <b>{IMAGE_MODELS[used_model]}</b>\n\n✏️ Запрос: {prompt}",
                parse_mode="HTML",
                reply_to_message_id=message_id,
            )

            # Потом фото отдельно
            await context.bot.send_photo(chat_id=chat_id, photo=result_data)

            log_activity(
                user_id,
                update.effective_user.username,
                "img_edit_album",
                f"{used_model}, {photos_count} photos: {prompt[:15]}",
            )

        except Exception as e:
            await delete_safe(thinking_msg)
            log_error("IMAGE_EDIT_ALBUM", str(e), user_id)
            error_msg = format_gemini_error(e, "IMAGE_EDIT_ALBUM")
            await context.bot.send_message(
                chat_id=chat_id,
                text=error_msg,
                parse_mode="HTML",
                reply_to_message_id=message_id,
            )
        finally:
            photos_bytes = None
            result_data = None
            gc_collect_after_media("album_edit:gc", user_id)
    else:
        # Альбом без команды редактирования — показываем кнопки
        # Сохраняем все фото альбома в photo_task
        context.user_data["photo_task"] = {
            "photos": photos_refs,  # Список ссылок на изображения альбома
            "message_id": message_id,
            "timestamp": time.time(),
        }

        keyboard = [
            [
                InlineKeyboardButton("Анализировать", callback_data="photo_analyze"),
                InlineKeyboardButton("✏️ Редактировать", callback_data="photo_edit"),
            ],
            [
                InlineKeyboardButton(
                    "📝 Добавить вопрос", callback_data="photo_add_caption"
                )
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📷 Получено {photos_count} фото. Что сделать с альбомом?",
            reply_markup=reply_markup,
            reply_to_message_id=message_id,
        )


# --- ОБРАБОТЧИК ДОКУМЕНТОВ (PDF, TXT, CSV и др.) ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает документы:
    - Файл без подписи → суммаризация
    - Файл + вопрос → ответ по содержимому
    """
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")

    document = update.message.document
    if not document:
        return

    if document.file_size and document.file_size > MAX_DOCUMENT_BYTES:
        return await update.message.reply_text(
            f"Документ слишком большой ({document.file_size / (1024 * 1024):.1f} МБ). Лимит: {MAX_DOCUMENT_BYTES // (1024 * 1024)} МБ."
        )

    # Получаем модель пользователя
    model_key = get_model_key(context)
    model_icon = "💎" if model_key == "pro" else "⚡"

    # Проверяем MIME тип
    mime_type = document.mime_type or "application/octet-stream"
    supported_mimes = [
        "application/pdf",
        "text/plain",
        "text/csv",
        "text/html",
        "text/markdown",
        "application/json",
    ]

    # Проверяем поддержку формата
    is_supported = mime_type in supported_mimes or mime_type.startswith("text/")
    if not is_supported:
        return await update.message.reply_text(
            f"Формат `{mime_type}` не поддерживается.\nПоддерживаемые: PDF, TXT, CSV, JSON, HTML, Markdown",
            parse_mode="HTML",
        )

    # Подпись или дефолтный промт
    caption = update.message.caption or ""
    prompt = (
        caption
        if caption
        else "Суммаризируй содержимое этого документа. Выдели ключевые моменты."
    )

    thinking_msg = await update.message.reply_text(
        f"{model_icon} Анализирую документ...",
        reply_to_message_id=update.message.message_id,
    )

    file_bytes = None
    document_temp_path = None

    try:
        # Скачиваем файл во временный файл, затем читаем в bytes только перед Gemini.
        log_memory("docuuad", user_id)
        suffix = os.path.splitext(document.file_name or "")[1]
        file = await document.get_file()
        document_temp_path = await download_telegram_file_to_temp(file, suffix)
        file_bytes = await asyncio.to_thread(read_binary_file, document_temp_path)
        log_memory("document:after_download", user_id)

        # Отправляем в Gemini через новый SDK
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS[model_key],
                    contents=[
                        genai_types.Part.from_bytes(
                            data=file_bytes, mime_type=mime_type
                        ),
                        prompt,
                    ],
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION_FLASH,
                        tools=SEARCH_TOOLS
                    ) if model_key == "flash" else None
                )
            ),
            timeout=120.0,  # Больше времени для документов
        )

        await thinking_msg.delete()
        response_text = (
            response.text
            if response and response.text
            else "Не удалось проанализировать документ"
        )
        await send_safe_message(update, response_text)

        # Логируем
        log_activity(
            user_id,
            update.effective_user.username,
            "doc_analyze",
            f"{document.file_name[:20]}",
        )
        log_memory("document:done", user_id)

    except asyncio.TimeoutError:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("DOC_ANALYZE_TIMEOUT", "Таймаут", user_id)
        await update.message.reply_text(
            "Превышено время анализа документа.",
            reply_to_message_id=update.message.message_id,
        )

    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("DOC_ANALYZE", str(e), user_id)
        error_msg = format_gemini_error(e, "DOC_ANALYZE")
        await update.message.reply_text(
            error_msg, parse_mode="HTML", reply_to_message_id=update.message.message_id
        )
    finally:
        file_bytes = None
        safe_delete_file(document_temp_path)
        gc_collect_after_media("document:gc", user_id)


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ handle_message ---
# Рефакторинг: вынесены для снижения цикломатической сложности (Radon F → B)


async def _process_photo_edit_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    """
    Обрабатывает ввод промта для редактирования фото (mode='awaiting_edit_prompt').
    Возвращает True если обработано.
    """
    if context.user_data.get("mode") != "awaiting_edit_prompt":
        return False

    if "photo_task" not in context.user_data:
        context.user_data.pop("mode", None)
        await update.message.reply_text("Данные фото потеряны. Отправьте фото заново.")
        return True

    text = update.message.text
    prompt = text
    photo_task = context.user_data["photo_task"]
    photo_items = photo_task["photos"]
    photos_count = len(photo_items)
    orig_msg_id = photo_task["message_id"]

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="upload_photo"
    )
    thinking_msg = await update.message.reply_text(
        f"🎨 Редактирую {photos_count} изображения..."
        if photos_count > 1
        else "🎨 Редактирую изображение...",
        reply_to_message_id=update.message.message_id,
    )

    photos_bytes = None
    result_data = None

    try:
        model_key = context.user_data.get("image_model", "pro")
        model_icon = "💎" if model_key == "pro" else "⚡"

        photos_bytes = await resolve_media_items_to_bytes(context.bot, photo_items)
        log_memory("photo_edit_prompt:after_download", user_id)
        result_data, used_model = await edit_image(
            photos_bytes, prompt, user_id, model_key
        )
        await thinking_msg.delete()

        # Сохраняем данные для перегенерации без bytes
        context.user_data["last_edit_data"] = {
            "photos": photo_items,
            "prompt": prompt,
            "model_key": model_key,
            "timestamp": time.time(),
        }

        # Формируем caption
        if photos_count > 1:
            caption = f"{model_icon} Отредактировано {photos_count} фото через <b>{IMAGE_MODELS[used_model]}</b>\n\n✏️ Запрос: {prompt}"
        else:
            caption = f"{model_icon} Отредактировано через <b>{IMAGE_MODELS[used_model]}</b>\n\n✏️ Запрос: {prompt}"

        await update.message.reply_text(
            caption, parse_mode="HTML", reply_to_message_id=orig_msg_id
        )

        # Кнопка перегенерации редактирования
        regen_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Ещё", callback_data="img_edit_regen")]]
        )

        await update.message.reply_photo(photo=result_data, reply_markup=regen_keyboard)

        log_activity(
            user_id,
            update.effective_user.username,
            "img_edit_btn_done",
            f"{used_model}, {photos_count} photos: {prompt[:15]}",
        )
        context.user_data.pop("mode", None)
        context.user_data.pop("photo_task", None)

    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("IMAGE_EDIT_BTN", str(e), user_id)
        error_msg = format_gemini_error(e, "IMAGE_EDIT_BTN")
        await update.message.reply_text(error_msg, parse_mode="HTML")
        context.user_data.pop("mode", None)
    finally:
        photos_bytes = None
        result_data = None
        gc_collect_after_media("photo_edit_prompt:gc", user_id)

    return True


async def _process_photo_analyze_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    """
    Обрабатывает текст пользователя после кнопки "Добавить описание" для анализа фото.
    """
    if context.user_data.get("mode") != "awaiting_photo_analyze_prompt":
        return False

    if "photo_task" not in context.user_data:
        context.user_data.pop("mode", None)
        await update.message.reply_text("Данные фото потеряны. Отправьте фото заново.")
        return True

    prompt = (update.message.text or "").strip()
    if not prompt:
        await update.message.reply_text(
            "Введите описание или вопрос к фото.",
            reply_to_message_id=update.message.message_id,
        )
        return True

    photo_task = context.user_data["photo_task"]
    photo_items = photo_task["photos"]
    photos_count = len(photo_items)

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    thinking_msg = await update.message.reply_text(
        f"⚡ Анализирую {photos_count} фото и описание..."
        if photos_count > 1
        else "⚡ Анализирую фото и описание...",
        reply_to_message_id=update.message.message_id,
    )

    photos_bytes = None
    contents = None

    try:
        photos_bytes = await resolve_media_items_to_bytes(context.bot, photo_items)
        log_memory("photo_analyze_prompt:after_download", user_id)

        # Объединяем оригинальную подпись поста и новый вопрос пользователя
        original_caption = photo_task.get("caption", "").strip()
        context_text = ""
        if original_caption:
            context_text += f"Описание поста: {original_caption}\n\n"
        context_text += f"Вопрос пользователя: {prompt}"

        # Инструкция для фактчекинга и поиска
        analysis_instruction = (
            "Ты — аналитик новостей и фактчекер. Проанализируй предоставленные изображения и описание поста. "
            "Используй Google Search, чтобы проверить достоверность этой информации, найти первоисточник или дополнительные подробности. "
            "Твоя задача — не просто сравнить фото и текст, а дать пользователю глубокий и проверенный ответ на его вопрос.\n\n"
            "Если информация в посте кажется ложной или устаревшей — обязательно укажи на это."
        )

        contents = [
            genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            for img_bytes in photos_bytes
        ] + [
            f"{analysis_instruction}\n\n{context_text}"
        ]

        # Создаем конфиг с инструментами поиска
        config = genai_types.GenerateContentConfig(
            system_instruction=None,  # Отключаем короткий флеш-промт, чтобы работал только промт аналитика
            tools=SEARCH_TOOLS
        )

        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS["flash"], 
                    contents=contents,
                    config=config
                )
            ),
            timeout=TIMEOUT_MEDIUM,
        )

        await thinking_msg.delete()
        response_text = (
            response.text
            if response and response.text
            else "Не удалось проанализировать изображение"
        )
        await send_safe_message(update, response_text)

        context.user_data["active_image"] = {
            "photo": photo_items[0],
            "timestamp": time.time(),
        }

        context.user_data.pop("mode", None)
        context.user_data.pop("photo_task", None)
        log_activity(
            user_id, update.effective_user.username, "img_analyze_prompt", prompt[:40]
        )

    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("IMG_ANALYZE_PROMPT", str(e), user_id)
        error_msg = format_gemini_error(e, "IMG_ANALYZE_PROMPT")
        await update.message.reply_text(
            error_msg, parse_mode="HTML", reply_to_message_id=update.message.message_id
        )
        context.user_data.pop("mode", None)
    finally:
        photos_bytes = None
        contents = None
        gc_collect_after_media("photo_analyze_prompt:gc", user_id)

    return True


async def _process_exit_commands(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lower_text: str
) -> bool:
    """
    Сбрасывает активный режим по команде выхода (выход/exit/quit/stop).
    Возвращает True если обработано.
    """
    if lower_text not in ["выход", "exit", "quit", "stop"]:
        return False

    current_mode = context.user_data.get("mode")
    if not current_mode:
        return False

    context.user_data.pop("mode", None)

    messages = {
        "translate": "✅ Режим переводчика выключен.",
        "image_gen": "✅ Режим генерации изображений выключен.",
        "youtube_mode": "✅ Режим YouTube саммари выключен.",
        "youtube_preview_mode": "✅ Режим YouTube превью выключен.",
    }

    msg = messages.get(current_mode, "✅ Режим выключен.")
    await update.message.reply_text(msg, reply_to_message_id=update.message.message_id)
    return True


async def _process_fast_commands(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    stripped: str,
    lower_text: str,
    user_id: int,
) -> bool:
    """
    Обрабатывает быстрые команды: п, ф, к, ю, пр, .
    Возвращает True если команда обработана.
    """
    # Включение режима переводчика (без текста)
    if lower_text in ["пр", "перевод", "translate"]:
        context.user_data["mode"] = "translate"
        await update.message.reply_text(
            "🗣 Отправьте текст для перевода на русский:",
            reply_to_message_id=update.message.message_id,
        )
        return True

    # Мгновенный перевод с текстом (пр <текст>)
    if (
        lower_text.startswith("пр ")
        or lower_text.startswith("перевод ")
        or lower_text.startswith("translate ")
    ):
        if lower_text.startswith("translate "):
            text_to_translate = stripped[10:].strip()
        elif lower_text.startswith("перевод "):
            text_to_translate = stripped[8:].strip()
        else:
            text_to_translate = stripped[3:].strip()

        if text_to_translate:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="typing"
            )
            prompt_text = f"Переведи этот текст на русский язык максимально точно и литературно, сохраняя стиль оригинала. Не добавляй никаких комментариев, только перевод:\n\n{text_to_translate}"

            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: gemini_client.models.generate_content(
                            model=MODELS.get("lite", MODELS["flash"]),
                            contents=prompt_text,
                            config=genai_types.GenerateContentConfig(
                                system_instruction="Ты — профессиональный переводчик.",
                            )
                        )
                    ),
                    timeout=TIMEOUT_SHORT,
                )
                response_text = (
                    response.text
                    if response and response.text
                    else "Не удалось перевести"
                )
                await send_safe_message(update, response_text)
                log_activity(
                    user_id,
                    update.effective_user.username,
                    "translate",
                    text_to_translate[:30],
                )
            except Exception as e:
                log_error("TRANSLATE", str(e), user_id)
                error_msg = format_gemini_error(e, "TRANSLATE")
                await update.message.reply_text(
                    error_msg,
                    parse_mode="HTML",
                    reply_to_message_id=update.message.message_id,
                )
            return True

    # Включение режима YouTube саммари (без ссылки)
    if lower_text in ["ю", "ютуб", "youtube", "самари"]:
        context.user_data["mode"] = "youtube_mode"
        await update.message.reply_text(
            "📺 Отправьте ссылку на YouTube видео:",
            reply_to_message_id=update.message.message_id,
        )
        log_activity(
            user_id,
            update.effective_user.username,
            "youtube_request",
            "Режим активирован",
        )
        return True

    # Мгновенное саммари YouTube со ссылкой (ю <ссылка>)
    if (
        lower_text.startswith("ю ")
        or lower_text.startswith("ютуб ")
        or lower_text.startswith("youtube ")
        or lower_text.startswith("самари ")
    ):
        if lower_text.startswith("youtube "):
            url = stripped[8:].strip()
        elif lower_text.startswith("самари "):
            url = stripped[7:].strip()
        elif lower_text.startswith("ютуб "):
            url = stripped[5:].strip()
        else:
            url = stripped[2:].strip()

        if url:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="typing"
            )
            thinking_msg = await update.message.reply_text(
                "⏳ Загружаю субтитры и создаю саммари...",
                reply_to_message_id=update.message.message_id,
            )

            try:
                result = await summarize_youtube(url)
                await delete_safe(thinking_msg)

                if result["success"]:
                    await send_safe_message(update, result["summary"])
                    log_activity(
                        user_id, update.effective_user.username, "youtube_summary", url
                    )
                else:
                    await update.message.reply_text(
                        f"❌ {result['error']}",
                        reply_to_message_id=update.message.message_id,
                    )
                    log_activity(
                        user_id,
                        update.effective_user.username,
                        "youtube_error",
                        result["error"],
                    )
            except Exception as e:
                await delete_safe(thinking_msg)
                log_error("YOUTUBE", str(e), user_id)
                error_msg = format_gemini_error(e, "YOUTUBE")
                await update.message.reply_text(
                    error_msg,
                    parse_mode="HTML",
                    reply_to_message_id=update.message.message_id,
                )
            return True

    # --- YOUTUBE ПРЕВЬЮ ---
    # Включение режима YouTube превью (без ссылки)
    if lower_text in ["превью", "пре"]:
        context.user_data["mode"] = "youtube_preview_mode"
        await update.message.reply_text(
            "🖼️ Отправьте ссылку на YouTube видео для превью:",
            reply_to_message_id=update.message.message_id,
        )
        log_activity(
            user_id,
            update.effective_user.username,
            "preview_request",
            "Режим активирован",
        )
        return True

    # Мгновенное превью со ссылкой (превью <ссылка>)
    if lower_text.startswith("превью ") or lower_text.startswith("пре "):
        if lower_text.startswith("пре "):
            url = stripped[4:].strip()
        else:
            url = stripped[7:].strip()

        if url:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="upload_photo"
            )

            result = await get_youtube_preview(url, context.application)

            if result["success"]:
                # Формируем подпись: название + ссылка
                caption = f"🎬 {result['title']}\n{result['original_url']}"

                try:
                    await update.message.reply_photo(
                        photo=result["thumbnail_url"],
                        caption=caption,
                        reply_to_message_id=update.message.message_id,
                    )
                    log_activity(
                        user_id, update.effective_user.username, "youtube_preview", url
                    )
                except Exception as e:
                    logger.error(f"YouTube Preview send error: {e}")
                    await update.message.reply_text(
                        f"❌ Ошибка отправки превью: {str(e)[:100]}",
                        reply_to_message_id=update.message.message_id,
                    )
            else:
                await update.message.reply_text(
                    result["error"], reply_to_message_id=update.message.message_id
                )
            return True

    # Переключение моделей (Про / Флэш)
    if lower_text in ["п", "про", "pro"] or (lower_text.startswith("✅") and "pro" in lower_text.lower()):
        context.user_data["model"] = "pro"
        reset_session(context)
        model_id = MODELS.get("pro", "unknown")
        await update.message.reply_text(
            f"✅ Модель переключена на <b>Pro</b>\n"
            f"Настоящая модель: <code>{model_id}</code>", 
            parse_mode="HTML", 
            reply_to_message_id=update.message.message_id,
            reply_markup=ReplyKeyboardRemove()
        )
        return True

    if lower_text in ["ф", "флеш", "flash"] or (lower_text.startswith("✅") and "flash" in lower_text.lower()) or "gemini-2.5-flash" in lower_text:
        context.user_data["model"] = "flash"
        reset_session(context)
        model_id = MODELS.get("flash", "unknown")
        await update.message.reply_text(
            f"✅ Модель переключена на <b>Flash</b>\n"
            f"Настоящая модель: <code>{model_id}</code>\n"
            f"🌐 Подключенные инструменты: Google Search, URL Context", 
            parse_mode="HTML", 
            reply_to_message_id=update.message.message_id,
            reply_markup=ReplyKeyboardRemove()
        )
        return True

    # Сброс контекста
    if stripped == ".":
        was_in_mode = context.user_data.get("mode")
        reset_session(context)
        if was_in_mode == "image_gen":
            await update.message.reply_text(
                "🔄 Режим генерации отменён.",
                reply_to_message_id=update.message.message_id,
            )
        elif was_in_mode == "translate":
            await update.message.reply_text(
                "🔄 Режим перевода отменён.",
                reply_to_message_id=update.message.message_id,
            )
        else:
            await update.message.reply_text(
                "🔄 Контекст сброшен.", reply_to_message_id=update.message.message_id
            )
        return True

    # КОМАНДА "К" или "КАРТИНКА" - генерация изображений
    if lower_text in ["к", "картинка"]:
        context.user_data["mode"] = "image_gen"
        model_key = get_user_image_model(user_id, context)
        model_icon = "💎" if model_key == "pro" else "⚡"
        await update.message.reply_text(
            f"🎨 {model_icon} Опишите что нарисовать:",
            reply_to_message_id=update.message.message_id,
        )
        return True

    # Переключение модели картинок через "к про" или "к флеш"
    if lower_text in ["к про", "к pro"]:
        uid_str = str(user_id)
        if uid_str not in user_settings:
            user_settings[uid_str] = {}
        user_settings[uid_str]["image_model"] = "pro"
        save_user_settings()

        context.user_data["image_model"] = "pro"
        context.user_data.pop("mode", None)
        await update.message.reply_text(
            f"🎨 Глобальная модель для изображения:\n💎 <b>Pro</b> {IMAGE_MODELS['pro']}",
            parse_mode="HTML",
            reply_to_message_id=update.message.message_id,
        )
        return True

    if lower_text in ["к флеш", "к flash"]:
        uid_str = str(user_id)
        if uid_str not in user_settings:
            user_settings[uid_str] = {}
        user_settings[uid_str]["image_model"] = "flash"
        save_user_settings()

        context.user_data["image_model"] = "flash"
        context.user_data.pop("mode", None)
        await update.message.reply_text(
            f"🎨 Глобальная модель для изображения:\n⚡ <b>Flash</b> {IMAGE_MODELS['flash']}",
            parse_mode="HTML",
            reply_to_message_id=update.message.message_id,
        )
        return True

    # С промтом сразу после команды
    if lower_text.startswith("к ") or lower_text.startswith("картинка "):
        if lower_text.startswith("картинка "):
            prompt = stripped[9:].strip()
        else:
            prompt = stripped[2:].strip()

        await handle_image_generation(update, context, prompt, user_id)
        return True

    # КОМАНДА "Р" или "РЕДАКТИРОВАТЬ" - режим ожидания фото для редактирования
    if lower_text in ["р", "редактировать", "edit"]:
        context.user_data["mode"] = "awaiting_edit_photo"
        model_key = get_user_image_model(user_id, context)
        model_icon = "💎" if model_key == "pro" else "⚡"
        await update.message.reply_text(
            f"✏️ {model_icon} Отправьте фото (или альбом) для редактирования:",
            reply_to_message_id=update.message.message_id,
        )
        return True

    return False


async def fetch_tweet_data(tweet_id: str, app: Application) -> tuple:
    """
    Получает данные твита параллельно через несколько API.
    Все запросы запускаются одновременно — побеждает первый успешный.
    Вынесена на уровень модуля, чтобы быть доступной и из button_callback,
    и из handle_message при обработке вопроса пользователя.
    """
    apis = [
        f"https://api.fxtwitter.com/status/{tweet_id}",
        f"https://api.vxtwitter.com/status/{tweet_id}",
        f"https://api.fixupx.com/status/{tweet_id}",
    ]

    client = await get_http_client(app)
    PER_REQUEST_TIMEOUT = 7.0

    async def try_api(url: str):
        try:
            logger.debug(f"Twitter fetch attempt: {url}")
            resp = await asyncio.wait_for(client.get(url), timeout=PER_REQUEST_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                if "tweet" in data:
                    return data["tweet"]
        except Exception as e:
            logger.warning(f"Twitter API failed ({url.split('/')[2]}): {type(e).__name__}: {str(e)[:80]}")
        return None

    tasks = [asyncio.create_task(try_api(url)) for url in apis]
    last_error = "Все API вернули ошибку или пустой ответ"
    result = None

    try:
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
            timeout=PER_REQUEST_TIMEOUT + 1.0,
        )
        for task in done:
            try:
                val = task.result()
                if val is not None:
                    result = val
                    break
            except Exception:
                pass

        if result is None and pending:
            done2, _ = await asyncio.wait(pending, timeout=2.0)
            for task in done2:
                try:
                    val = task.result()
                    if val is not None:
                        result = val
                        break
                except Exception:
                    pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

    if result is not None:
        return result, None
    return None, last_error


async def _process_twitter_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int
) -> bool:
    """
    Детектирует ссылку на Twitter/X в сообщении и предлагает действия.
    Запрос к FxTwitter API делается только по нажатию кнопки — не здесь.
    Возвращает True если ссылка найдена.
    """
    match = TWITTER_PATTERN.search(text)
    if not match:
        return False

    tweet_id = match.group(1)
    tweet_url = match.group(0)  # Полный URL из сообщения

    # Сохраняем только ID и URL — без лишних запросов к API
    context.user_data["pending_tweet"] = {
        "id": tweet_id,
        "url": tweet_url,
        "timestamp": time.time(),
    }

    # Кнопки действий
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💬 Обсудить", callback_data="twitter_discuss"),
                InlineKeyboardButton("📤 Отправить", callback_data="twitter_send"),
            ]
        ]
    )

    await update.message.reply_text(
        "Что вы хотите с этим сделать?",
        reply_markup=keyboard,
        reply_to_message_id=update.message.message_id,
    )

    log_activity(
        user_id, update.effective_user.username, "twitter_link", tweet_url[:60]
    )
    return True


async def _process_reply_to_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    """
    Анализирует фото при реплае на сообщение с фото.
    Возвращает True если обработано.
    """
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        return False

    text = update.message.text
    prompt = text.strip() or "Сделай анализ фото"

    model_key = get_model_key(context)
    model_icon = "💎" if model_key == "pro" else "⚡"

    thinking_msg = await update.message.reply_text(
        f"{model_icon} Анализирую...", reply_to_message_id=update.message.message_id
    )

    try:
        photo = update.message.reply_to_message.photo[-1]
        photo_file = await photo.get_file()
        photo_bytes = prepare_image_for_gemini(
            bytes(await photo_file.download_as_bytearray())
        )
        log_memory("reply_photo:after_download", user_id)

        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS[model_key],
                    contents=[
                        genai_types.Part.from_bytes(
                            data=photo_bytes, mime_type="image/jpeg"
                        ),
                        prompt,
                    ],
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION_FLASH,
                        tools=SEARCH_TOOLS
                    ) if model_key == "flash" else None
                )
            ),
            timeout=60.0,
        )

        await thinking_msg.delete()
        response_text = (
            response.text
            if response and response.text
            else "Не удалось проанализировать"
        )
        await send_safe_message(update, response_text)
        bot_stats["messages_count"] += 1
        log_activity(
            user_id,
            update.effective_user.username,
            "img_analyze",
            f"reply: {prompt[:20]}",
        )

    except asyncio.TimeoutError:
        await delete_safe(thinking_msg)
        log_error("IMAGE_ANALYZE_TIMEOUT", "Таймаут", user_id)
        await update.message.reply_text(
            "Превышено время анализа.", reply_to_message_id=update.message.message_id
        )

    except Exception as e:
        await delete_safe(thinking_msg)
        log_error("IMAGE_ANALYZE", str(e), user_id)
        await update.message.reply_text(
            f"Ошибка: <code>{escape_html(str(e)[:150])}</code>",
            parse_mode="HTML",
            reply_to_message_id=update.message.message_id,
        )

    return True


async def _process_translation_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int
) -> None:
    """Переводит текст на русский (mode='translate')"""
    prompt_text = f"Переведи этот текст на русский язык максимально точно и литературно, сохраняя стиль оригинала. Не добавляй никаких комментариев, только перевод:\n\n{text}"

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS.get("lite", MODELS["flash"]), 
                    contents=prompt_text,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION_FLASH,
                        tools=SEARCH_TOOLS
                    )
                )
            ),
            timeout=TIMEOUT_SHORT,
        )
        response_text = (
            response.text if response and response.text else "Не удалось перевести"
        )
        await send_safe_message(update, response_text)
        context.user_data.pop("mode", None)
    except Exception as e:
        log_error("TRANSLATE", str(e), user_id)
        await update.message.reply_text(f"Ошибка перевода: {str(e)[:100]}")


async def _process_youtube_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int
) -> None:
    """Создаёт саммари YouTube видео (mode='youtube_mode')"""
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    thinking_msg = await update.message.reply_text(
        "⏳ Загружаю субтитры и создаю саммари...",
        reply_to_message_id=update.message.message_id,
    )

    try:
        result = await summarize_youtube(text)
        await thinking_msg.delete()

        if result["success"]:
            await send_safe_message(update, result["summary"])
            # Сохраняем саммари в контекст для последующего обсуждения
            context.user_data["active_youtube"] = {
                "summary": result["summary"],
                "timestamp": time.time(),
                "injected": False
            }
            log_activity(
                user_id, update.effective_user.username, "youtube_summary", text
            )
        else:
            await update.message.reply_text(
                f"❌ {result['error']}", reply_to_message_id=update.message.message_id
            )
            log_activity(
                user_id,
                update.effective_user.username,
                "youtube_error",
                result["error"],
            )
    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("YOUTUBE", str(e), user_id)
        await update.message.reply_text(
            f"❌ Ошибка обработки YouTube: {str(e)[:100]}",
            reply_to_message_id=update.message.message_id,
        )


async def _process_image_gen_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int
) -> None:
    """Генерирует изображение по промту (mode='image_gen')"""
    prompt = text.strip()
    await handle_image_generation(update, context, prompt, user_id)


# --- ОБРАБОТЧИК СООБЩЕНИЙ ---


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Диспетчер сообщений — делегирует обработку вспомогательным функциям.
    Рефакторинг: снижена сложность с F до B/C по Radon.
    """
    # 1. Валидация входных данных
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    text = update.message.text

    # 2. Проверка доступа
    if not check_access(user_id):
        if chat_type == ChatType.PRIVATE:
            await update.message.reply_text("⛔️ Нет доступа.")
        return

    # --- Обработка пересланных сообщений ---
    if getattr(update.message, "forward_origin", None):
        context.user_data["forwarded_context"] = {
            "type": "text",
            "text": text
        }
        await update.message.reply_text("✅ Текст принят. Жду ваш вопрос.")
        return

    bot_username = context.bot.username

    # 3. Проверка групповых чатов (reply/mention)
    is_reply_to_bot = False
    if update.message.reply_to_message:
        reply_user = update.message.reply_to_message.from_user
        if reply_user:
            is_reply_to_bot = reply_user.id == context.bot.id

    is_mentioned = bot_username and bot_username in text

    if chat_type != ChatType.PRIVATE and not (is_reply_to_bot or is_mentioned):
        return

    # --- Склейка пересланного контекста ---
    forwarded_data = context.user_data.get("forwarded_context")
    if forwarded_data:
        saved_text = forwarded_data.get("text", "")
        saved_photo = forwarded_data.get("photo_id")
        
        final_prompt = f"Контекст:\n{saved_text}\n\nВопрос: {text}"
        context.user_data.pop("forwarded_context", None)
        
        if saved_photo:
            context.user_data["photo_task"] = {
                "photos": [make_telegram_media_ref(saved_photo)],
                "caption": saved_text,
                "message_id": update.message.message_id,
                "timestamp": time.time(),
            }
            context.user_data["mode"] = "awaiting_photo_analyze_prompt"
            # Для анализа фото _process_photo_analyze_prompt читает update.message.text (он содержит сам вопрос)
            return await _process_photo_analyze_prompt(update, context, user_id)
        else:
            text = final_prompt

    # 4. Подготовка текста
    stripped = text.strip()
    lower_text = stripped.lower()

    # 5. ДИСПЕТЧЕР — делегирование helper-функциям

    # Редактирование фото по кнопке (mode='awaiting_edit_prompt')
    if await _process_photo_edit_prompt(update, context, user_id):
        return

    # Анализ фото по кнопке "Добавить описание"
    if await _process_photo_analyze_prompt(update, context, user_id):
        return

    # Команды выхода (выход/exit/quit/stop)
    if await _process_exit_commands(update, context, lower_text):
        return

    # Быстрые команды (п, ф, к, ю, пр, .)
    if await _process_fast_commands(update, context, stripped, lower_text, user_id):
        return

    # Реплай на фото — анализ изображения
    if await _process_reply_to_photo(update, context, user_id):
        return

    # Twitter/X ссылка — предлагаем действия (без запроса к API)
    if await _process_twitter_link(update, context, text, user_id):
        return

    # Подсчет сообщений
    bot_stats["messages_count"] += 1

    # Режим изменения промпта для генерации (новое)
    if context.user_data.get("mode") == "awaiting_new_image_prompt":
        context.user_data.pop("mode", None)
        return await _process_image_gen_mode(update, context, text, user_id)

    # Режим изменения промпта для редактирования (новое)
    if context.user_data.get("mode") == "awaiting_new_edit_prompt":
        context.user_data.pop("mode", None)
        # Подставляем старые фото, но новый промпт
        last_edit = context.user_data.get("last_edit_data")
        if not last_edit:
            await update.message.reply_text(
                "Данные фото потеряны. Отправьте фото заново."
            )
            return

        # Обновляем промпт в сохраненных данных
        last_edit["prompt"] = text
        context.user_data["photo_task"] = {
            "photos": last_edit["photos"],
            "message_id": update.message.message_id,
            "timestamp": time.time(),
        }
        # Используем существующий обработчик промпта для редактирования
        context.user_data["mode"] = "awaiting_edit_prompt"
        return await _process_photo_edit_prompt(update, context, user_id)

    # Режим YouTube саммари
    if context.user_data.get("mode") == "youtube_mode":
        context.user_data.pop("mode", None)
        return await _process_youtube_mode(update, context, text, user_id)

    # Режим вопроса о твите (Шаг 2: пользователь написал вопрос — идём к Gemini)
    if context.user_data.get("mode") == "twitter_question_mode":
        context.user_data.pop("mode", None)
        tweet_data = context.user_data.get("pending_tweet")
        if not tweet_data:
            await update.message.reply_text(
                "⚠️ Данные твита устарели. Отправьте ссылку заново.",
                reply_to_message_id=update.message.message_id,
            )
            return

        tweet_url = tweet_data["url"]
        tweet_id = tweet_data["id"]
        user_question = text.strip()

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        thinking_msg = await update.message.reply_text(
            "⚡ Загружаю твит и думаю...", reply_to_message_id=update.message.message_id
        )

        # Получаем данные твита через модульную функцию (параллельные запросы)
        tw, error = await fetch_tweet_data(tweet_id, context.application)

        tweet_text = ""
        author = ""
        if tw:
            tweet_text = tw.get("text", "")
            author = tw.get("author", {}).get("screen_name", "")

        # Формируем промпт: контекст твита + вопрос пользователя
        if tweet_text:
            prompt = (
                f'Твит от @{author}:\n\n"{tweet_text}"\n'
                f"Ссылка: {tweet_url}\n\n"
                f"Вопрос: {user_question}"
            )
        else:
            # Данные не получены — отправляем только ссылку + вопрос
            prompt = f"Твит: {tweet_url}\n\nВопрос: {user_question}"

        try:
            chat = get_or_create_session(context)
            response_gemini = await asyncio.wait_for(
                asyncio.to_thread(chat.send_message, prompt), timeout=TIMEOUT_MEDIUM
            )
            increment_chat_message_count(context)
            await delete_safe(thinking_msg)

            response_text = (
                response_gemini.text
                if response_gemini and response_gemini.text
                else "Не удалось получить ответ"
            )
            await send_safe_message(update, response_text)
            log_activity(user_id, update.effective_user.username, "twitter_discuss", tweet_url[:50])
            context.user_data.pop("pending_tweet", None)

        except Exception as e:
            await delete_safe(thinking_msg)
            log_error("TWITTER_DISCUSS", str(e), user_id)
            await send_safe_message(update, format_gemini_error(e, "TWITTER_DISCUSS"))
        return

    # Режим YouTube превью
    if context.user_data.get("mode") == "youtube_preview_mode":
        context.user_data.pop("mode", None)
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="upload_photo"
        )

        result = await get_youtube_preview(text, context.application)

        if result["success"]:
            # Формируем подпись: название + ссылка
            caption = f"🎬 {result['title']}\n{result['original_url']}"

            try:
                await update.message.reply_photo(
                    photo=result["thumbnail_url"],
                    caption=caption,
                    reply_to_message_id=update.message.message_id,
                )
                log_activity(
                    user_id, update.effective_user.username, "youtube_preview", text
                )
            except Exception as e:
                logger.error(f"YouTube Preview send error: {e}")
                await update.message.reply_text(
                    f"❌ Ошибка отправки превью: {str(e)[:100]}",
                    reply_to_message_id=update.message.message_id,
                )
        else:
            await update.message.reply_text(
                result["error"], reply_to_message_id=update.message.message_id
            )
        return

    # Режим переводчика
    if context.user_data.get("mode") == "translate":
        return await _process_translation_mode(update, context, text, user_id)

    # 6. ОБЫЧНЫЙ ТЕКСТОВЫЙ ЧАТ

    # Проверяем активное изображение в контексте
    active_image = context.user_data.get("active_image")
    if active_image:
        elapsed = time.time() - active_image["timestamp"]
        if elapsed > IMAGE_CONTEXT_TIMEOUT:
            context.user_data.pop("active_image", None)
            active_image = None

    # Проверяем активный контекст YouTube
    active_youtube = context.user_data.get("active_youtube")
    if active_youtube:
        elapsed = time.time() - active_youtube["timestamp"]
        if elapsed > IMAGE_CONTEXT_TIMEOUT:
            context.user_data.pop("active_youtube", None)
            active_youtube = None

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    thinking_msg = await update.message.reply_text(
        "❇️ Думаю...", reply_to_message_id=update.message.message_id
    )

    try:
        clean_text = (
            text.replace(f"@{bot_username}", "").strip() if bot_username else text
        )

        # Мультимодальный запрос с активным изображением
        if active_image:
            model_key = get_model_key(context)
            active_photo_items = [
                active_image.get("photo", active_image.get("photo_bytes"))
            ]
            active_photo_bytes = (
                await resolve_media_items_to_bytes(context.bot, active_photo_items)
            )[0]
            log_memory("active_image:after_download", user_id)
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: gemini_client.models.generate_content(
                        model=MODELS[model_key],
                        contents=[
                            genai_types.Part.from_bytes(
                                data=active_photo_bytes, mime_type="image/jpeg"
                            ),
                            clean_text,
                        ],
                        config=genai_types.GenerateContentConfig(
                            system_instruction=SYSTEM_INSTRUCTION_FLASH,
                            tools=SEARCH_TOOLS
                        ) if model_key == "flash" else None
                    )
                ),
                timeout=TIMEOUT_SHORT,
            )
        else:
            # Обычный текстовый чат с поиском
            chat = get_or_create_session(context)
            
            # Если есть активный YouTube контекст и он еще не был отправлен в сессию
            if active_youtube and not active_youtube.get("injected"):
                clean_text = f"[Контекст из недавнего YouTube видео:\n{active_youtube['summary']}]\n\nВопрос пользователя: {clean_text}"
                active_youtube["injected"] = True  # Чтобы не дублировать огромный текст в каждый запрос

            log_memory("text:before_gemini", user_id)
            response = await send_with_retry(chat, clean_text)
            increment_chat_message_count(context)
            log_memory("text:after_gemini", user_id)

        await delete_safe(thinking_msg)

        response_text = (
            response.text if response and response.text else "Пустой ответ от API"
        )
        await send_safe_message(update, response_text)

        model_key = get_model_key(context)
        log_activity(
            user_id, update.effective_user.username, "text", f"Model: {model_key}"
        )

    except Exception as e:
        await delete_safe(thinking_msg)
        log_error("API", str(e), user_id)
        error_text = format_gemini_error(e, "CHAT")
        await send_safe_message(update, error_text)

        if chat_type == ChatType.PRIVATE and user_id != ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🚨 API Error\nUser: {user_id}\n<code>{error_text[:200]}</code>",
                    parse_mode="HTML",
                )
            except Exception as notify_err:
                logger.debug(f"Не удалось уведомить админа: {notify_err}")


def _parse_inline_command(text: str) -> tuple[str, str]:
    """
    Парсит inline-запрос и определяет команду по первому слову.
    Поддерживает разделители: пробел, Enter (новая строка).
    Возвращает (command_type, argument).
    """
    parts = text.split(None, 1)
    if not parts:
        return ("gemini", text)

    cmd_word = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    # Перевод: пр / перевод
    if cmd_word in ("пр", "перевод"):
        return ("translate", arg)

    # YouTube саммари: ю / ютуб
    if cmd_word in ("ю", "ютуб"):
        return ("youtube", arg)

    # YouTube превью: пре / превью
    if cmd_word in ("пре", "превью"):
        return ("preview", arg)

    # Twitter: тв / твиттер
    if cmd_word in ("тв", "твиттер"):
        return ("twitter", arg)

    # Всё остальное — Gemini
    return ("gemini", text)


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает inline-запросы (@bot_name текст).
    Поддерживает команды: пр (перевод), ю (YouTube), превью, и обычный вопрос Gemini.
    Требуется: /setinlinefeedback 100% в @BotFather
    """
    query = update.inline_query
    user = query.from_user
    text = (query.query or "").strip()

    avatar_url = get_bot_avatar_url()

    # Проверка доступа — неавторизованные получат кнопку перехода в бота
    if not check_access(user.id):
        results = [
            InlineQueryResultArticle(
                id="no_access",
                title="👋 Привет! Это приватный бот Энигмена",
                description="Хочешь такого же? Сделай бесплатно сам по гайду, жми на кнопку ⬆️",
                input_message_content=InputTextMessageContent(
                    message_text="Ты нажал не ту кнопку"
                ),
                thumbnail_url=avatar_url,
            )
        ]
        await query.answer(
            results,
            cache_time=1,
            button=InlineQueryResultsButton(
                text="➡️➡️➡️【Жми на меня】⬅️⬅️⬅️", start_parameter="guide"
            ),
        )
        return

    # Пустой запрос — подсказка с доступными командами
    if not text:
        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="💡 Введите вопрос или команду",
                description="пр <текст> · ю <ссылка> · превью <ссылка> · тв <ссылка> · или вопрос",
                input_message_content=InputTextMessageContent(
                    message_text="💡 Команды: пр, ю, превью, тв — или просто вопрос",
                    parse_mode="HTML",
                ),
                thumbnail_url=avatar_url,
            )
        ]
        await query.answer(results, cache_time=60)
        return

    # Определяем команду по префиксу
    cmd_type, cmd_arg = _parse_inline_command(text)

    # ВАЖНО: reply_markup обязательна! Без InlineKeyboardMarkup Telegram
    # не передаёт inline_message_id в ChosenInlineResult, и edit_message_text невозможен.
    loading_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏳", callback_data="inline_loading")]]
    )

    # --- ПЕРЕВОД ---
    if cmd_type == "translate":
        if not cmd_arg:
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="🌐 Перевод",
                    description="Введите: пр <текст для перевода>",
                    input_message_content=InputTextMessageContent(
                        message_text="🌐 Используйте: @bot пр <текст>"
                    ),
                    thumbnail_url=avatar_url,
                )
            ]
            await query.answer(results, cache_time=30)
            return

        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="🌐 Перевести",
                description=cmd_arg[:100],
                input_message_content=InputTextMessageContent(
                    message_text="🌐 Перевожу...", parse_mode="HTML"
                ),
                reply_markup=loading_keyboard,
                thumbnail_url=avatar_url,
            )
        ]
        await query.answer(results, cache_time=0)
        return

    # --- YOUTUBE САММАРИ ---
    if cmd_type == "youtube":
        if not cmd_arg:
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="📺 YouTube Саммари",
                    description="Введите: ю <ссылка на видео>",
                    input_message_content=InputTextMessageContent(
                        message_text="📺 Используйте: @bot ю <ссылка>"
                    ),
                    thumbnail_url=avatar_url,
                )
            ]
            await query.answer(results, cache_time=30)
            return

        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="📺 YouTube Саммари",
                description=cmd_arg[:100],
                input_message_content=InputTextMessageContent(
                    message_text="📺 Загружаю саммари...", parse_mode="HTML"
                ),
                reply_markup=loading_keyboard,
                thumbnail_url=avatar_url,
            )
        ]
        await query.answer(results, cache_time=0)
        return

    # --- YOUTUBE ПРЕВЬЮ ---
    if cmd_type == "preview":
        if not cmd_arg:
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="🖼️ YouTube Превью",
                    description="Введите: превью <ссылка на видео>",
                    input_message_content=InputTextMessageContent(
                        message_text="🖼️ Используйте: @bot превью <ссылка>"
                    ),
                    thumbnail_url=avatar_url,
                )
            ]
            await query.answer(results, cache_time=30)
            return

        # Извлекаем video_id для YouTube-миниатюры в списке результатов
        video_id = extract_video_id(cmd_arg)
        logger.info(f"Inline Preview: arg='{cmd_arg}', video_id='{video_id}'")

        if not video_id:
            logger.warning(f"Inline Preview: Invalid video link '{cmd_arg}'")
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="❌ Некорректная ссылка",
                    description="Не удалось распознать YouTube ссылку",
                    input_message_content=InputTextMessageContent(
                        message_text="❌ Не удалось распознать YouTube ссылку"
                    ),
                    thumbnail_url=avatar_url,
                )
            ]
            await query.answer(results, cache_time=30)
            return

        # Гибридная заглушка:
        # thumbnail_url (в списке) = чёрный квадрат (как просил юзер)
        # photo_url (в чате) = чёрный квадрат (placeholder, заменится на реальное превью)
        results = [
            InlineQueryResultPhoto(
                id=str(uuid.uuid4()),
                photo_url=BLACK_SQUARE_URL,
                thumbnail_url=BLACK_SQUARE_URL,
                title=f"✅ YouTube: {cmd_arg[:40]}...",
                caption=f"⏳ Формирую превью...\n{cmd_arg}",
                reply_markup=loading_keyboard,
            )
        ]
        await query.answer(results, cache_time=0)
        return

    # --- TWITTER ---
    if cmd_type == "twitter":
        if not cmd_arg:
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="🐦 Twitter/X",
                    description="Введите: тв <ссылка на твит>",
                    input_message_content=InputTextMessageContent(
                        message_text="🐦 Используйте: @bot тв <ссылка>"
                    ),
                    thumbnail_url=avatar_url,
                )
            ]
            await query.answer(results, cache_time=30)
            return

        match = TWITTER_PATTERN.search(cmd_arg)
        if not match:
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="❌ Некорректная ссылка",
                    description="Не удалось распознать ссылку на Twitter/X",
                    input_message_content=InputTextMessageContent(
                        message_text="❌ Не удалось распознать ссылку на Twitter/X"
                    ),
                    thumbnail_url=avatar_url,
                )
            ]
            await query.answer(results, cache_time=30)
            return

        results = [
            InlineQueryResultPhoto(
                id=str(uuid.uuid4()),
                photo_url=BLACK_SQUARE_URL,
                thumbnail_url=BLACK_SQUARE_URL,
                title=f"🐦 Twitter: {cmd_arg[:40]}...",
                caption=f"⏳ Загружаю твит...\n{cmd_arg}",
                reply_markup=loading_keyboard,
            )
        ]
        await query.answer(results, cache_time=0)
        return

    # --- GEMINI (по умолчанию) ---
    results = [
        InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="🔮 Спросить Gemini",
            description=text[:100],
            input_message_content=InputTextMessageContent(
                message_text="Ищу ответ (╭ರ_•́)╭", parse_mode="HTML"
            ),
            reply_markup=loading_keyboard,
            thumbnail_url=avatar_url,
        )
    ]
    await query.answer(results, cache_time=0)


async def handle_chosen_inline_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """
    Вызывается ПОСЛЕ того, как юзер кликнул на inline-результат.
    Роутинг по команде: перевод, YouTube саммари или Gemini вопрос.
    Превью обрабатывается в handle_inline_query (не нужен ChosenInlineResult).
    Требуется: /setinlinefeedback 100% в @BotFather
    """
    result = update.chosen_inline_result
    inline_message_id = result.inline_message_id
    user = result.from_user
    text = (result.query or "").strip()

    # Без текста или без inline_message_id — редактировать нечего
    if not text or not inline_message_id:
        return

    # Определяем тип команды
    cmd_type, cmd_arg = _parse_inline_command(text)

    try:
        # --- ПЕРЕВОД ---
        if cmd_type == "translate" and cmd_arg:
            prompt_text = f"Переведи этот текст на русский язык максимально точно и литературно, сохраняя стиль оригинала. Не добавляй никаких комментариев, только перевод:\n\n{cmd_arg}"
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: gemini_client.models.generate_content(
                        model=MODELS["flash"], 
                        contents=prompt_text,
                        config=genai_types.GenerateContentConfig(
                            system_instruction="Ты — профессиональный переводчик.",
                        )
                    )
                ),
                timeout=TIMEOUT_SHORT,
            )
            response_text = (
                response.text if response and response.text else "Не удалось перевести"
            )
            formatted_text = format_for_telegram(response_text)
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=f"<b>🌐 Перевод:</b>\n{formatted_text}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([]),
            )
            log_activity(user.id, user.username, "inline_translate", cmd_arg[:30])
            return

        # --- YOUTUBE САММАРИ ---
        if cmd_type == "youtube" and cmd_arg:
            result_yt = await summarize_youtube(cmd_arg)
            if result_yt["success"]:
                formatted_text = format_for_telegram(result_yt["summary"])
                await context.bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=f"<b>📺 YouTube Саммари:</b>\n{formatted_text}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([]),
                )
            else:
                await context.bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=f"❌ {result_yt['error']}",
                    reply_markup=InlineKeyboardMarkup([]),
                )
            log_activity(user.id, user.username, "inline_youtube", cmd_arg[:30])
            return

        # --- YOUTUBE ПРЕВЬЮ ---
        if cmd_type == "preview" and cmd_arg:
            preview = await get_youtube_preview(cmd_arg, context.application)
            if preview["success"]:
                thumb_url = preview["thumbnail_url"]
                title = preview["title"]

                # Заменяем фото-заглушку на реальное превью видео
                await context.bot.edit_message_media(
                    inline_message_id=inline_message_id,
                    media=InputMediaPhoto(
                        media=thumb_url,
                        caption=f"🎬 <b>{escape_html(title)}</b>\n{preview['original_url']}",
                        parse_mode="HTML",
                    ),
                    reply_markup=InlineKeyboardMarkup([]),
                )
            else:
                # Если ошибка, меняем подпись у заглушки
                await context.bot.edit_message_caption(
                    inline_message_id=inline_message_id,
                    caption=f"❌ {preview['error']}",
                    reply_markup=InlineKeyboardMarkup([]),
                )
            log_activity(user.id, user.username, "inline_preview", cmd_arg[:30])
            return

        # --- TWITTER ---
        if cmd_type == "twitter" and cmd_arg:
            match = TWITTER_PATTERN.search(cmd_arg)
            if not match:
                await context.bot.edit_message_caption(
                    inline_message_id=inline_message_id,
                    caption="❌ Неверная ссылка на Twitter/X",
                    reply_markup=InlineKeyboardMarkup([]),
                )
                return

            tweet_id = match.group(1)
            tweet_url = match.group(0)

            tw, error = await fetch_tweet_data(tweet_id, context.application)
            if not tw:
                await context.bot.edit_message_caption(
                    inline_message_id=inline_message_id,
                    caption=f"❌ Не удалось получить данные твита.\nОшибка: <code>{error}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([]),
                )
                return

            tweet_text = tw.get("text", "")
            photos = []

            media = tw.get("media", {})
            if media:
                photos = [p["url"] for p in media.get("photos", [])]

            author_name = tw.get("author", {}).get("name", "")
            author_handle = tw.get("author", {}).get("screen_name", "")

            caption_parts = []
            
            # Строим заголовок в формате: 👤 Имя / @username (где в @username вшита ссылка)
            header_elements = []
            if author_name:
                header_elements.append(f"<b>{escape_html(author_name)}</b>")
            if author_handle:
                header_elements.append(f'<a href="{tweet_url}">@{escape_html(author_handle)}</a>')
            else:
                header_elements.append(f'<a href="{tweet_url}">Пост</a>')
            
            header = f"👤 {' / '.join(header_elements)}:"
            caption_parts.append(header)

            if tweet_text:
                caption_parts.append(escape_html(tweet_text))

            caption = "\n\n".join(caption_parts)[:1024]

            if not photos:
                await context.bot.edit_message_caption(
                    inline_message_id=inline_message_id,
                    caption=caption or "Медиа не найдено.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([]),
                )
            else:
                await context.bot.edit_message_media(
                    inline_message_id=inline_message_id,
                    media=InputMediaPhoto(
                        media=photos[0],
                        caption=caption,
                        parse_mode="HTML",
                    ),
                    reply_markup=InlineKeyboardMarkup([]),
                )
            log_activity(user.id, user.username, "inline_twitter", cmd_arg[:30])
            return

        # --- GEMINI (по умолчанию) ---
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS["flash"],
                    contents=text,
                    config=genai_types.GenerateContentConfig(
                        system_instruction="Отвечай кратко, но если тема обширная — выдели главное, опусти второстепенное. Используй интернет для поиска актуальной информации.",
                        tools=SEARCH_TOOLS,
                    ),
                )
            ),
            timeout=TIMEOUT_MEDIUM,
        )

        response_text = (
            response.text if response and response.text else "Не удалось получить ответ"
        )
        formatted_text = format_for_telegram(response_text)

        # Длинные ответы сворачиваем в expandable blockquote
        if len(formatted_text) > 500:
            body = f"<blockquote expandable>{formatted_text}</blockquote>"
        else:
            body = formatted_text

        await context.bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=f"<b>✦ Gemini:</b> {body}\nฅ≽^◕⩊◕^≼⊃━✧゜",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([]),
        )
        log_activity(user.id, user.username, "inline", text[:30])

    except asyncio.TimeoutError:
        logger.warning(f"Inline chosen timeout for user {user.id}")
        await context.bot.edit_message_text(
            inline_message_id=inline_message_id,
            text="⏱️ Превышено время ожидания. Попробуйте снова.",
            reply_markup=InlineKeyboardMarkup([]),
        )

    except Exception as e:
        logger.warning(f"Inline chosen error: {e}")
        try:
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=f"❌ Ошибка: {str(e)[:200]}",
                reply_markup=InlineKeyboardMarkup([]),
            )
        except Exception:
            pass


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки под фото/альбом и перегенерацию картинок"""
    query = update.callback_query
    user_id = query.from_user.id
    action = query.data

    # --- TWITTER ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

    # Делегируем модульной функции — доступна и из handle_message
    async def _fetch_tweet_data(tweet_id: str):
        return await fetch_tweet_data(tweet_id, context.application)

    # Кнопка-заглушка из инлайн-режима — просто игнорируем
    if action == "inline_loading":
        await query.answer()
        return

    if not check_access(user_id):
        await query.answer("⛔️ Нет доступа.", show_alert=False)
        return

    # Кнопки перегенерации и Twitter — им не нужен photo_task, обрабатываем отдельно
    if action in ("img_regen", "img_edit_regen", "twitter_discuss", "twitter_send"):
        # Ответ на callback будет внутри обработчиков ниже
        pass
    else:
        # Все остальные кнопки (photo_analyze, photo_edit и т.д.) требуют photo_task
        await query.answer()

        if "photo_task" not in context.user_data:
            return await query.edit_message_text(
                "Данные фото устарели или отсутствуют. Отправьте фото заново."
            )

        # Проверяем таймаут (3 минуты)
        photo_data = context.user_data["photo_task"]
        elapsed_time = time.time() - photo_data.get("timestamp", 0)

        if elapsed_time > PHOTO_BUTTON_TIMEOUT:
            # Данные устарели — удаляем и сообщаем
            context.user_data.pop("photo_task", None)
            return await query.edit_message_text(
                f"⏱ Время ожидания истекло ({PHOTO_BUTTON_TIMEOUT // 60} мин). Отправьте фото заново."
            )

    # Подготавливаем данные для фото-кнопок (если есть)
    photo_data = context.user_data.get("photo_task", {})
    photo_items = photo_data.get("photos", [])
    photos_count = len(photo_items)

    if action == "photo_analyze":
        await query.edit_message_text(
            f"Анализирую {photos_count} фото..."
            if photos_count > 1
            else "Анализирую..."
        )

        # Используем модель пользователя
        model_key = get_model_key(context)
        model_icon = "💎" if model_key == "pro" else "⚡"

        # Формируем prompt: если есть подпись от пользователя — используем её
        user_caption = photo_data.get("caption", "").strip()
        if user_caption:
            prompt = user_caption
        else:
            prompt = "Сделай анализ фото"

        try:
            photos_bytes = await resolve_media_items_to_bytes(context.bot, photo_items)
            log_memory("photo_analyze_btn:after_download", user_id)
            # Формируем contents: все изображения + prompt
            contents = [
                genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
                for img_bytes in photos_bytes
            ] + [prompt]

            response = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: gemini_client.models.generate_content(
                        model=MODELS[model_key], 
                        contents=contents,
                        config=genai_types.GenerateContentConfig(
                            system_instruction=(
                                "Ты — аналитик новостей и фактчекер. Проанализируй предоставленные изображения. "
                                "Используй Google Search, чтобы проверить достоверность этой информации, найти подробности. "
                                "Дай проверенный и глубокий ответ."
                            ) if model_key == "flash" else None,
                            tools=SEARCH_TOOLS if model_key == "flash" else None
                        )
                    )
                ),
                timeout=120.0,
            )

            response_text = (
                response.text
                if response and response.text
                else "Не удалось проанализировать изображение"
            )

            # Отправляем ответ новым сообщением
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{model_icon} <b>Результат анализа ({photos_count} фото):</b>\n\n{format_for_telegram(response_text)}"
                if photos_count > 1
                else f"{model_icon} <b>Результат анализа:</b>\n\n{format_for_telegram(response_text)}",
                parse_mode="HTML",
                reply_to_message_id=photo_data["message_id"],
            )

            # Сохраняем ссылку на первое изображение в контексте для последующих вопросов
            context.user_data["active_image"] = {
                "photo": photo_items[0],
                "timestamp": time.time(),
            }

            # Очищаем временные данные кнопок
            context.user_data.pop("photo_task", None)

            log_activity(
                user_id,
                query.from_user.username,
                "img_analyze_btn",
                f"{model_key}, {photos_count} photos",
            )

        except Exception as e:
            log_error("BTN_ANALYZE", str(e), user_id)
            error_msg = format_gemini_error(e, "BTN_ANALYZE")
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=error_msg, parse_mode="HTML"
            )

    elif action == "photo_add_caption":
        context.user_data["mode"] = "awaiting_photo_analyze_prompt"
        await query.edit_message_text("📝 Жду вопрос к фото")

    elif action == "img_regen":
        # Перегенерация картинки по сохранённому промпту
        last_prompt = context.user_data.get("last_image_prompt")
        if not last_prompt:
            return await query.answer(
                "Промпт не найден. Сгенерируйте картинку заново.", show_alert=True
            )

        await query.answer("🔄 Перегенерирую...")
        model_key = get_user_image_model(user_id, context)
        model_icon = "💎" if model_key == "pro" else "⚡"

        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="upload_photo"
            )
            result_data, used_model = await generate_image(
                last_prompt, context, user_id
            )

            # Результат сохраним как Telegram file_id после отправки.

            # Кнопки под картинкой
            image_keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔄 Ещё", callback_data="img_regen"),
                        InlineKeyboardButton(
                            "✏️ Изменить запрос", callback_data="img_change_prompt"
                        ),
                    ]
                ]
            )

            sent_photo = await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=result_data,
                reply_markup=image_keyboard,
            )
            sent_file_id = get_sent_photo_file_id(sent_photo)
            if sent_file_id:
                context.user_data["last_generated_photo"] = make_telegram_media_ref(
                    sent_file_id
                )
            log_activity(
                user_id, query.from_user.username, "img_regen", last_prompt[:30]
            )

        except Exception as e:
            log_error("IMG_REGEN", str(e), user_id)
            error_msg = format_gemini_error(e, "IMG_REGEN")
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=error_msg, parse_mode="HTML"
            )

    elif action == "img_edit_regen":
        # Перегенерация редактирования по сохранённым данным
        last_edit = context.user_data.get("last_edit_data")
        if not last_edit:
            return await query.answer(
                "Данные редактирования не найдены. Отправьте фото заново.",
                show_alert=True,
            )

        await query.answer("🔄 Перегенерирую...")

        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="upload_photo"
            )
            edit_photos_bytes = await resolve_media_items_to_bytes(
                context.bot, last_edit["photos"]
            )
            log_memory("img_edit_regen:after_download", user_id)
            result_data, used_model = await edit_image(
                edit_photos_bytes,
                last_edit["prompt"],
                user_id,
                last_edit.get("model_key", "pro"),
            )

            # Кнопки под отредактированной картинкой
            edit_keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 В ту же степь", callback_data="img_edit_regen"
                        ),
                        InlineKeyboardButton(
                            "✏️ Другие правки", callback_data="img_edit_change_prompt"
                        ),
                    ]
                ]
            )

            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=result_data,
                reply_markup=edit_keyboard,
            )
            log_activity(
                user_id,
                query.from_user.username,
                "img_edit_regen",
                last_edit["prompt"][:30],
            )

        except Exception as e:
            log_error("IMG_EDIT_REGEN", str(e), user_id)
            error_msg = format_gemini_error(e, "IMG_EDIT_REGEN")
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=error_msg, parse_mode="HTML"
            )

    elif action == "photo_edit":
        # Переводим в режим ожидания промта для редактирования
        # Редактирование использует текущую Pro-модель изображений (в Free Tier это Flash Image).
        context.user_data["mode"] = "awaiting_edit_prompt"
        image_model_name = IMAGE_MODELS.get("pro", "gemini-3.1-flash-image-preview")

        if photos_count > 1:
            msg = f"✏️ Введите описание того, что нужно сделать с {photos_count} фото:\n\n💎 Используется: <b>{image_model_name}</b>"
        else:
            msg = f"✏️ Введите описание того, что нужно изменить или добавить на этом фото:\n\n💎 Используется: <b>{image_model_name}</b>"

        await query.edit_message_text(msg, parse_mode="HTML")
        # Данные фото не удаляем, они понадобятся в handle_message

    elif action == "photo_analyze_last":
        # Анализ последнего сгенерированного/отредактированного изображения
        photo_ref = context.user_data.get("last_generated_photo")
        if not photo_ref:
            # Пытаемся достать из данных редактирования если там пусто
            last_edit = context.user_data.get("last_edit_data")
            if last_edit and "photos" in last_edit:
                # В данном контексте "последнее" это результат, но если его нет,
                # берём оригинал для анализа. На самом деле нужно сохранять результат.
                await query.answer("Сначала сгенерируйте фото", show_alert=True)
                return

        context.user_data["photo_task"] = {
            "photos": [photo_ref]
            if is_media_ref(photo_ref) or isinstance(photo_ref, bytes)
            else photo_ref,
            "message_id": query.message.message_id,
            "timestamp": time.time(),
        }
        context.user_data["mode"] = "awaiting_photo_analyze_prompt"
        await query.edit_message_text("🔍 О чем спросить у этого изображения?")

    elif action == "img_change_prompt":
        context.user_data["mode"] = "awaiting_new_image_prompt"
        await query.edit_message_text("✏️ Введите новый запрос для генерации:")

    elif action == "img_edit_change_prompt":
        context.user_data["mode"] = "awaiting_new_edit_prompt"
        await query.edit_message_text("✏️ Опишите другие правки для этого фото:")

    # --- TWITTER КНОПКИ ---

    elif action == "twitter_discuss":
        # Шаг 1: спрашиваем у пользователя его вопрос, а не сразу к Gemini.
        tweet_data = context.user_data.get("pending_tweet")
        if not tweet_data:
            await query.answer("Данные устарели. Отправьте ссылку заново.", show_alert=True)
            return

        await query.answer()
        # Переключаем режим — следующее сообщение пользователя будет вопросом о твите
        context.user_data["mode"] = "twitter_question_mode"
        await query.edit_message_text("✍️ Напишите ваш вопрос о твите:")

    elif action == "twitter_send":
        tweet_data = context.user_data.get("pending_tweet")
        if not tweet_data:
            await query.answer("Данные устарели. Отправьте ссылку заново.", show_alert=True)
            return

        await query.answer()
        tweet_url = tweet_data["url"]
        tweet_id = tweet_data["id"]

        await query.edit_message_text("📤 Загружаю медиа из твита...")

        # Получаем данные через нашу функцию с фоллбеками
        tw, error = await _fetch_tweet_data(tweet_id)

        if not tw:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ Не удалось получить данные твита.\nОшибка: <code>{error}</code>",
                parse_mode="HTML"
            )
            return

        try:
            tweet_text = tw.get("text", "")
            photos = []

            media = tw.get("media", {})
            if media:
                photos = [p["url"] for p in media.get("photos", [])]

            author_name = tw.get("author", {}).get("name", "")
            author_handle = tw.get("author", {}).get("screen_name", "")

            caption_parts = []
            
            # Строим заголовок в формате: 👤 Имя / @username (где в @username вшита ссылка)
            header_elements = []
            if author_name:
                header_elements.append(f"<b>{escape_html(author_name)}</b>")
            if author_handle:
                header_elements.append(f'<a href="{tweet_url}">@{escape_html(author_handle)}</a>')
            else:
                header_elements.append(f'<a href="{tweet_url}">Пост</a>')
            
            header = f"👤 {' / '.join(header_elements)}:"
            caption_parts.append(header)

            if tweet_text:
                caption_parts.append(escape_html(tweet_text))

            caption = "\n\n".join(caption_parts)[:1024]

            if not photos:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=caption or "Медиа не найдено.",
                    parse_mode="HTML",
                )
            elif len(photos) == 1:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=photos[0],
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                media_group = [
                    InputMediaPhoto(
                        media=url,
                        caption=caption if i == 0 else None,
                        parse_mode="HTML",
                    )
                    for i, url in enumerate(photos[:10])
                ]
                await context.bot.send_media_group(chat_id=update.effective_chat.id, media=media_group)

            log_activity(user_id, query.from_user.username, "twitter_send", f"{len(photos)} photos")
            context.user_data.pop("pending_tweet", None)

        except Exception as e:
            log_error("TWITTER_SEND", str(e), user_id)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ Ошибка отправки: <code>{escape_html(str(e)[:200])}</code>",
                parse_mode="HTML",
            )


# --- ЗАПУСК ---
if __name__ == "__main__":
    cleanup_log_files()
    load_activity_log()
    logger.info(f"Загружено {len(user_activity)} записей за сегодня")
    load_users()
    logger.info(f"Загружено {len(allowed_users)} пользователей")
    load_user_settings()
    logger.info(f"Загружены настройки пользователей: {len(user_settings)} шт.")


async def post_init(app: Application):
    """Настройка команд меню и уведомление админа после старта"""
    # Обновляем команды меню с актуальными ID моделей
    latest = get_latest_models()
    pro_id = latest.get("pro", "gemini-3-flash-preview")
    flash_id = latest.get("flash", "gemini-2.5-flash")

    await app.bot.set_my_commands(
        [
            ("start", "🔄 Сбросить контекст"),
            ("status", "📊 Статус бота"),
            ("youtube", "📺 YouTube Саммари"),
            ("imagepro", "🎨💎Image Pro"),
            ("imageflash", "🎨⚡Image Flash"),
            ("1model", f"💎 Pro [{pro_id}]"),
            ("2model", f"⚡ Flash [{flash_id}]"),
            ("help", "❓ Справка"),
        ]
    )
    logger.info("Меню команд установлено")
    if "cleanup_task" not in app.bot_data:
        app.bot_data["cleanup_task"] = asyncio.create_task(cleanup_loop(app))
        logger.info(f"Cleanup task started: interval={CLEANUP_INTERVAL}s")

    if MEMORY_DEBUG and "memory_monitor_task" not in app.bot_data:
        app.bot_data["memory_monitor_task"] = asyncio.create_task(memory_monitor_loop())

    await get_http_client(app)
    logger.info("HTTP client started")

    if ADMIN_ID:
        try:
            now = datetime.now(KYIV_TZ)
            start_time = now.strftime("%H:%M:%S")
            start_date = now.strftime("%d.%m.%Y")
            pro_model = MODELS.get("pro", "?")
            flash_model = MODELS.get("flash", "?")
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🟢 <b>Бот запущен!</b>\n"
                    f"📅 {start_date}\n"
                    f"⏰ {start_time}\n"
                    f"💎 Pro: <code>{pro_model}</code>\n"
                    f"⚡ Flash: <code>{flash_model}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление о старте: {e}")


async def post_shutdown(app: Application):
    """Корректно закрывает общие ресурсы при остановке приложения."""
    await close_http_client(app)
    cleanup_task = app.bot_data.get("cleanup_task")
    if cleanup_task:
        cleanup_task.cancel()

    memory_monitor_task = app.bot_data.get("memory_monitor_task")
    if memory_monitor_task:
        memory_monitor_task.cancel()


def main():
    """Основная функция запуска бота"""
    # Инициализация моделей (безопасная, не роняет бот при старте без сети)
    initialize_models()

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("youtube", youtube_command))
    application.add_handler(CommandHandler("add", add_user))
    application.add_handler(CommandHandler("del", del_user))
    application.add_handler(CommandHandler("1model", set_pro_model))
    application.add_handler(CommandHandler("2model", set_flash_model))
    application.add_handler(CommandHandler("id", my_id))
    application.add_handler(CommandHandler("imagepro", set_image_pro))
    application.add_handler(CommandHandler("imageflash", set_image_flash))

    # Обработчики сообщений
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    )
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(InlineQueryHandler(handle_inline_query))
    application.add_handler(ChosenInlineResultHandler(handle_chosen_inline_result))

    # Глобальный обработчик ошибок
    application.add_error_handler(global_error_handler)

    logger.info(
        f"🚀 BOT STARTED. Pro: {MODELS.get('pro')} | Flash: {MODELS.get('flash')}"
    )
    log_memory("startup:before_polling")

    application.run_polling(drop_pending_updates=True)


# --- ЗАПУСК ---
if __name__ == "__main__":
    cleanup_log_files()
    load_activity_log()
    logger.info(f"Загружено {len(user_activity)} записей за сегодня")
    load_users()
    logger.info(f"Загружено {len(allowed_users)} пользователей")
    load_user_settings()
    logger.info(f"Загружены настройки пользователей: {len(user_settings)} шт.")

    main()
