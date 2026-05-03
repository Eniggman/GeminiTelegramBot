from logging.handlers import RotatingFileHandler
import os
import json
import logging
import time
import asyncio
import io
import re
import uuid
import platform
import psutil
import requests
import sys
import io

# Принудительная установка UTF-8 для Windows консоли
if platform.system() == 'Windows':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


from google import genai as genai_client
from google.genai import types as genai_types
from PIL import Image
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    InlineQueryResultArticle, InlineQueryResultPhoto,
    InputTextMessageContent, InlineQueryResultsButton,
    InputMediaPhoto
)
from telegram.constants import ChatType
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, CallbackQueryHandler, InlineQueryHandler, 
    ChosenInlineResultHandler, filters
)
from telegram.error import NetworkError
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi

# Базовая папка скрипта
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Загрузка переменных окружения
env_path = os.path.join(BASE_DIR, '.env')
load_dotenv(dotenv_path=env_path)

# --- КОНФИГУРАЦИЯ ---

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("❌ Не найдены переменные окружения! Проверьте файл .env")

# Проверка ADMIN_ID для админ-функций
if not ADMIN_ID:
    print("ВНИМАНИЕ: ADMIN_ID не задан в .env! Админ-функции будут недоступны.")

# Базовые настройки
MEMORY_TIMEOUT = 5 * 60  # 5 минут
MAX_RETRIES = 2

# Таймауты (в секундах)
TIMEOUT_SHORT = 60        # Перевод, YouTube саммари — обычно 5-15 сек
TIMEOUT_MEDIUM = 300      # Gemini чат с google_search — может искать долго
TIMEOUT_LONG = 180        # Генерация/редактирование изображений — самые долгие
PHOTO_BUTTON_TIMEOUT = 180    # Время жизни кнопок под фото (3 мин)
IMAGE_CONTEXT_TIMEOUT = 300   # Время жизни изображения в контексте (5 мин)

# Telegram лимиты
MAX_MESSAGE_LENGTH = 4000     # Максимальная длина сообщения
ALBUM_WAIT_TIME = 2.5         # Секунды ожидания остальных фото альбома
MAX_ALBUM_PHOTOS = 10         # Максимум фото в альбоме для обработки

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
USERS_FILE = os.path.join(BASE_DIR, 'allowed_users.json')

# Файл с пользовательскими настройками
USER_SETTINGS_FILE = os.path.join(BASE_DIR, 'user_settings.json')

# Клиент нового SDK (для чатов с google_search и генерации изображений)
gemini_client = genai_client.Client(api_key=GEMINI_API_KEY)

# Инструменты для интернет-поиска и анализа URL
SEARCH_TOOLS = [
    {"google_search": {}},
    {"url_context": {}}
]

# Модели для генераций изображений (Nano Banana) - Free Tier
IMAGE_MODELS = {
    'pro': 'gemini-3.1-flash-image-preview',  # Pro заблокирована, используем Flash
    'flash': 'gemini-3.1-flash-image-preview'
}


# Ссылки для инлайн-заглушек
avatar_url = "https://raw.githubusercontent.com/Eniggman/GeminiTelegramBot/main/docs/image.png"
# Гарантированно рабочий черный квадрат (Placehold.co)
BLACK_SQUARE_URL = "https://placehold.co/600x400/000000/000000.png"

# Паттерн для детекции ссылок Twitter/X
TWITTER_PATTERN = re.compile(
    r'https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/(\d+)',
    re.IGNORECASE
)


# Настройка логирования с ротацией

# Константы для логирования
LOG_FILE = os.path.join(BASE_DIR, 'bot.log')
LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 МБ максимум на файл
LOG_BACKUP_COUNT = 1  # Хранить 1 бэкап (итого макс ~100 МБ)
ACTIVITY_LOG_MAX_ENTRIES = 500  # Максимум записей в activity_log
LOG_TO_FILE = os.getenv('LOG_TO_FILE', '1') == '1'
SAVE_ACTIVITY_LOG = os.getenv('SAVE_ACTIVITY_LOG', '1') == '1'
LOG_RETENTION_DAYS = int(os.getenv('LOG_RETENTION_DAYS', '7'))
LOG_MAX_TOTAL_BYTES = int(os.getenv('LOG_MAX_TOTAL_BYTES', str(LOG_MAX_BYTES * (LOG_BACKUP_COUNT + 1))))

# Настройка форматтера
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Консольный хендлер (для отладки)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.ERROR)

# Файловый хендлер с ротацией (опционально)
handlers = [console_handler]
if LOG_TO_FILE:
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding='utf-8'
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
            if name == os.path.basename(LOG_FILE) or name.startswith(f"{os.path.basename(LOG_FILE)}."):
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
    Возвращает актуальные версии бесплатных моделей Gemini (Серия 3 и стабильный Lite).
    """
    # Новейшие и стабильные бесплатные модели
    flash_model = 'gemini-3-flash-preview'
    lite_model = 'gemini-flash-lite-latest'  # Самый стабильный для перевода
    image_model = 'gemini-3.1-flash-image-preview'

    return {
        'pro': flash_model, # Заглушка для Free Tier
        'flash': flash_model, 
        'lite': lite_model,
        'img_pro': image_model,
        'img_flash': image_model
    }



# Будет инициализировано в main()
MODELS = {}


def initialize_models() -> None:
    """Инициализирует глобальные переменные MODELS и IMAGE_MODELS"""
    try:
        latest = get_latest_models()
        MODELS.update({
            'pro': latest['pro'],
            'flash': latest['flash'],
            'lite': latest['lite']
        })
        # Обновляем IMAGE_MODELS из проверенных данных
        IMAGE_MODELS.update({
            'pro': latest['img_pro'],
            'flash': latest['img_flash']
        })
        logger.debug(f"✅ Модели: Pro={MODELS['pro']}, Flash={MODELS['flash']}, Lite={MODELS['lite']}")
        logger.debug(f"🎨 Фото Модели: Pro={IMAGE_MODELS['pro']}, Flash={IMAGE_MODELS['flash']}")
    except Exception as e:
        logger.error(f"Critical Error: {e}")
        # Фоллбек для запуска без сети
        MODELS.update({
            'pro': 'gemini-3.1-pro-preview',
            'flash': 'gemini-3-flash-preview',
            'lite': 'gemini-3.1-flash-lite-preview'
        })
        IMAGE_MODELS.update({
            'pro': 'gemini-3-pro-image-preview',
            'flash': 'gemini-3.1-flash-image-preview'
        })
        print(f"Работаем с дефолтными моделями (Offline mode): {MODELS}")


# --- ПАМЯТЬ БОТА ---
# Хранит сессии Gemini, текущие модели пользователей и режимы работы.


allowed_users = set()

# Глобальные настройки пользователей (например, выбор image_model)
user_settings = {}

# Хранилище для сбора альбомов (media_group)
pending_albums = {}

# URL изображения бота
BOT_AVATAR_URL = "https://raw.githubusercontent.com/Eniggman/GeminiTelegramBot/main/docs/image.png"

# --- СТАТИСТИКА И ЛОГИ ---
bot_stats = {
    'start_time': time.time(),
    'messages_count': 0,
    'voice_count': 0,
    'errors_count': 0,
    'last_errors': [],
}


def log_error(error_type: str, error_msg: str, user_id: int = None):
    """Сохраняет ошибку в лог"""
    error_entry = {
        'time': time.strftime('%d.%m %H:%M'),
        'type': error_type,
        'msg': str(error_msg)[:100],
        'user': user_id
    }
    bot_stats['errors_count'] += 1
    bot_stats['last_errors'].append(error_entry)
    if len(bot_stats['last_errors']) > 10:
        bot_stats['last_errors'].pop(0)
    logger.error(f"{error_type}: {str(error_msg)[:200]}")


async def delete_safe(message: object):
    """Безопасно удаляет сообщение Telegram."""
    try:
        if message:
            await message.delete()
    except Exception:
        pass


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
            await update.effective_message.reply_text(error_text, parse_mode='HTML')
        except Exception as notify_err:
            logger.debug(f"Не удалось уведомить пользователя об ошибке: {notify_err}")

# Часовой пояс Киева
KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Файл для логов активности
ACTIVITY_LOG_FILE = os.path.join(BASE_DIR, 'activity_log.json')

# Структура логов
user_activity = []


def get_day_start() -> float:
    """Возвращает timestamp начала текущего дня по Киеву (00:00)"""
    now_kyiv = datetime.now(KYIV_TZ)
    day_start = now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.timestamp()


def log_activity(user_id: int, username: str, action: str, details: str = "") -> None:
    """Логирует активность пользователя"""
    entry = {
        'timestamp': time.time(),
        'user_id': user_id,
        'username': username or 'Unknown',
        'action': action,
        'details': details
    }
    user_activity.append(entry)

    # Удаляем записи старше начала текущего дня
    day_start = get_day_start()
    user_activity[:] = [a for a in user_activity if a['timestamp'] >= day_start]

    # Лимит записей для экономии памяти и диска (e2-micro оптимизация)
    if len(user_activity) > ACTIVITY_LOG_MAX_ENTRIES:
        user_activity[:] = user_activity[-ACTIVITY_LOG_MAX_ENTRIES:]

    # Периодически сохраняем (каждые 10 записей)
    if len(user_activity) % 10 == 0:
        save_activity_log()


def save_activity_log() -> None:
    """Сохраняет логи в файл"""
    if not SAVE_ACTIVITY_LOG:
        return
    try:
        with open(ACTIVITY_LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(user_activity, f)
    except Exception as e:
        logger.warning(f"Activity log save error: {e}")


def load_activity_log() -> None:
    """Загружает логи из файла"""
    if not SAVE_ACTIVITY_LOG:
        return
    global user_activity
    if os.path.exists(ACTIVITY_LOG_FILE):
        try:
            with open(ACTIVITY_LOG_FILE, 'r', encoding='utf-8') as f:
                user_activity = json.load(f)
            # Оставляем только записи с начала текущего дня
            day_start = get_day_start()
            user_activity = [a for a in user_activity if a['timestamp'] >= day_start]
        except Exception as e:
            logger.warning(f"Activity log load error: {e}")
            user_activity = []


# --- ФУНКЦИИ УПРАВЛЕНИЯ ПОЛЬЗОВАТЕЛЯМИ ---
def load_users() -> None:
    global allowed_users
    env_users = os.getenv('ALLOWED_USERS', '')
    if env_users:
        try:
            allowed_users.update(int(u.strip()) for u in env_users.split(',') if u.strip())
        except Exception as e:
            logger.warning(f"Ошибка загрузки ALLOWED_USERS: {e}")

    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                allowed_users.update(set(json.load(f)))
        except Exception as e:
            logger.warning(f"Ошибка загрузки {USERS_FILE}: {e}")


def save_users() -> None:
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(allowed_users), f)
    except Exception as e:
        logger.warning(f"Ошибка сохранения пользователей: {e}")


def load_user_settings() -> None:
    global user_settings
    if os.path.exists(USER_SETTINGS_FILE):
        try:
            with open(USER_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                user_settings = json.load(f)
        except Exception as e:
            logger.warning(f"Ошибка загрузки {USER_SETTINGS_FILE}: {e}")
            user_settings = {}


def save_user_settings() -> None:
    try:
        with open(USER_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(user_settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Ошибка сохранения настроек пользователей: {e}")


def check_access(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in allowed_users


def get_bot_avatar_url() -> str:
    """URL аватарки бота для inline-результатов"""
    return BOT_AVATAR_URL


def get_user_image_model(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Получает и кэширует в context предпочтительную модель изображений пользователя (pro/flash)"""
    val = context.user_data.get('image_model')
    if not val:
        uid_str = str(user_id)
        if uid_str in user_settings and 'image_model' in user_settings[uid_str]:
            val = user_settings[uid_str]['image_model']
        else:
            val = 'pro' # По умолчанию pro
        context.user_data['image_model'] = val
    return val


def get_model_key(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Возвращает ключ модели пользователя (pro/flash)"""
    return context.user_data.get('model', 'flash')


# --- ФУНКЦИЯ СБРОСА КОНТЕКСТА ---
def reset_session(context: ContextTypes.DEFAULT_TYPE) -> object:
    """Создаёт новую сессию чата с google_search и url_context"""
    model_key = get_model_key(context)
    instruction = SYSTEM_INSTRUCTION_PRO if model_key == 'pro' else SYSTEM_INSTRUCTION_FLASH

    # Создаём чат через новый SDK с инструментами поиска
    chat = gemini_client.chats.create(
        model=MODELS[model_key],
        config=genai_types.GenerateContentConfig(
            system_instruction=instruction,
            tools=SEARCH_TOOLS
        )
    )

    context.user_data['chat_session'] = chat
    context.user_data['last_activity'] = time.time()

    # Сбрасываем режим при сбросе сессии
    context.user_data.pop('mode', None)

    # Сбрасываем активное изображение
    context.user_data.pop('active_image', None)

    return chat


def get_or_create_session(context: ContextTypes.DEFAULT_TYPE) -> object:
    """Получает сессию или создаёт новую если нужно"""
    current_time = time.time()
    last_time = context.user_data.get('last_activity', 0)

    # Проверяем таймаут
    if 'chat_session' not in context.user_data or (current_time - last_time) > MEMORY_TIMEOUT:
        reset_session(context)
    else:
        context.user_data['last_activity'] = current_time

    return context.user_data['chat_session']

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
    if '503' in error_str or '500' in error_str or 'unavailable' in error_str or 'high demand' in error_str:
        return (
            f"⏳ {prefix}**Google сейчас перегружен** (высокая нагрузка).\n"
            f"Пожалуйста, подожди 30-60 секунд и попробуй снова.\n"
            f"`[Error 503/500: Server Issue]`"
        )

    # Квота / Rate Limit
    if 'quota' in error_str or 'rate limit' in error_str or '429' in error_str:
        if 'limit: 0' in error_str:
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
    if 'blocked' in error_str or 'safety' in error_str or 'harmful' in error_str or 'finish_reason' in error_str:
        return f"🛡️ {prefix}[SAFETY] Контент заблокирован фильтром безопасности.\n`{error_safe[:120]}`"

    # Проблемы с авторизацией
    if 'api key' in error_str or 'invalid' in error_str or '401' in error_str or '403' in error_str:
        return f"🔑 {prefix}[AUTH] Проблема с API ключом.\n`{error_safe[:150]}`"

    # Модель недоступна (условие после 503, так как 503 часто содержит слово unavailable)
    if 'model' in error_str and ('not found' in error_str or 'does not exist' in error_str):
        return f"🤖 {prefix}[MODEL] Модель не найдена или не поддерживается.\n`{error_safe[:120]}`"

    # Слишком длинный запрос
    if 'token' in error_str and ('limit' in error_str or 'exceed' in error_str or 'too long' in error_str):
        return f"📏 {prefix}[TOKEN LIMIT] Запрос слишком длинный.\n`{error_safe[:100]}`"

    # Проблемы с сетью
    if 'connection' in error_str or 'timeout' in error_str or 'network' in error_str:
        return f"🌐 {prefix}[NETWORK] Ошибка сети.\n`{error_safe[:100]}`"

    # Неизвестная ошибка сервера Google
    if 'internal' in error_str or 'server' in error_str:
        return f"💥 {prefix}[SERVER] Внутренняя ошибка сервера Google.\n`{error_safe[:100]}`"

    # Неподдерживаемый формат
    if 'unsupported' in error_str or 'invalid format' in error_str or 'mime' in error_str:
        return f"📄 {prefix}[FORMAT] Неподдерживаемый формат.\n<code>{error_safe[:120]}</code>"

    # Неизвестная ошибка — показываем полностью для отладки
    return f"{prefix}[ERROR]\n<code>{error_safe[:250]}</code>"


def escape_html(text: str) -> str:
    """
    Экранирует HTML-спецсимволы для безопасной отправки в Telegram.
    Источник: https://core.telegram.org/bots/api#html-style
    """
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def clean_latex(text: str) -> str:
    """
    Конвертирует LaTeX-формулы в читаемый Unicode-текст для Telegram.
    Gemini иногда отвечает с $...$, \text{}, \frac{}{} и т.д.,
    которые Telegram не умеет рендерить.
    """
    if '$' not in text and '\\' not in text:
        return text

    # Убираем блочные формулы $$...$$, потом инлайн $...$
    text = re.sub(r'\$\$(.*?)\$\$', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\$([^$]+?)\$', r'\1', text)

    # \text{...} → содержимое
    text = re.sub(r'\\text\{([^}]*)\}', r'\1', text)
    # \textbf{...} → содержимое
    text = re.sub(r'\\textbf\{([^}]*)\}', r'\1', text)
    # \mathrm{...} → содержимое
    text = re.sub(r'\\mathrm\{([^}]*)\}', r'\1', text)
    # \mathbf{...} → содержимое
    text = re.sub(r'\\mathbf\{([^}]*)\}', r'\1', text)

    # \frac{a}{b} → a/b
    text = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\1/\2', text)
    # \sqrt{x} → √(x)
    text = re.sub(r'\\sqrt\{([^}]*)\}', r'√(\1)', text)

    # Надстрочные цифры: ^{2} → ² или ^2 → ²
    superscripts = {'0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
                    '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
                    '+': '⁺', '-': '⁻', 'n': 'ⁿ'}

    def replace_superscript(match):
        content = match.group(1)
        return ''.join(superscripts.get(c, c) for c in content)

    text = re.sub(r'\^\{([^}]*)\}', replace_superscript, text)
    text = re.sub(r'\^(\d)', lambda m: superscripts.get(m.group(1), m.group(1)), text)

    # Подстрочные цифры: _{2} → ₂ или _2 → ₂
    subscripts = {'0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄',
                  '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉',
                  '+': '₊', '-': '₋', 'a': 'ₐ', 'e': 'ₑ', 'i': 'ᵢ',
                  'o': 'ₒ', 'n': 'ₙ', 'x': 'ₓ'}

    def replace_subscript(match):
        content = match.group(1)
        return ''.join(subscripts.get(c, c) for c in content)

    text = re.sub(r'_\{([^}]*)\}', replace_subscript, text)
    text = re.sub(r'_(\d)', lambda m: subscripts.get(m.group(1), m.group(1)), text)

    # Греческие буквы и математические символы
    latex_symbols = {
        # Греческие (строчные)
        '\\alpha': 'α', '\\beta': 'β', '\\gamma': 'γ', '\\delta': 'δ',
        '\\epsilon': 'ε', '\\zeta': 'ζ', '\\eta': 'η', '\\theta': 'θ',
        '\\iota': 'ι', '\\kappa': 'κ', '\\lambda': 'λ', '\\mu': 'μ',
        '\\nu': 'ν', '\\xi': 'ξ', '\\pi': 'π', '\\rho': 'ρ',
        '\\sigma': 'σ', '\\tau': 'τ', '\\phi': 'φ', '\\chi': 'χ',
        '\\psi': 'ψ', '\\omega': 'ω',
        # Греческие (заглавные)
        '\\Gamma': 'Γ', '\\Delta': 'Δ', '\\Theta': 'Θ', '\\Lambda': 'Λ',
        '\\Pi': 'Π', '\\Sigma': 'Σ', '\\Phi': 'Φ', '\\Psi': 'Ψ', '\\Omega': 'Ω',
        # Математические операторы
        '\\cdot': '·', '\\times': '×', '\\div': '÷', '\\pm': '±',
        '\\approx': '≈', '\\neq': '≠', '\\leq': '≤', '\\geq': '≥',
        '\\ll': '≪', '\\gg': '≫', '\\equiv': '≡', '\\sim': '∼',
        '\\propto': '∝', '\\infty': '∞',
        # Стрелки
        '\\to': '→', '\\rightarrow': '→', '\\leftarrow': '←',
        '\\leftrightarrow': '↔', '\\Rightarrow': '⇒',
        # Прочее
        '\\sum': 'Σ', '\\prod': 'Π', '\\int': '∫',
        '\\partial': '∂', '\\nabla': '∇', '\\degree': '°',
        '\\circ': '°', '\\bullet': '•',
    }

    # Сортируем по длине (длинные сначала), чтобы \lambda не перекрыл \lam
    for latex_cmd, unicode_char in sorted(latex_symbols.items(), key=lambda x: -len(x[0])):
        text = text.replace(latex_cmd, unicode_char)

    # Убираем LaTeX-пробелы: \, \; \! \quad \qquad
    text = re.sub(r'\\[,;!]', ' ', text)
    text = re.sub(r'\\q?quad', ' ', text)

    # Убираем \left и \right (скобки остаются)
    text = re.sub(r'\\(?:left|right|big|Big|bigg|Bigg)', '', text)

    # Убираем оставшиеся \commandname (неизвестные команды) — но оставляем \n, \t
    text = re.sub(r'\\([a-zA-Z]+)', r'\1', text)

    # Чистим двойные пробелы
    text = re.sub(r'  +', ' ', text)

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
            value = re.sub(r'(\*\*|__)(.*?)\1', r'\2', value)
            value = re.sub(r'(\*|_)(.*?)\1', r'\2', value)
            value = value.replace('`', '')
            value = re.sub(r'\s+', ' ', value).strip()
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
        joined_lines = '\n'.join(aligned_lines)
        table_blocks.append(f"<pre>{joined_lines}</pre>")
        return placeholder

    # Паттерн для таблиц: строки с | в начале и конце
    table_pattern = r'(?:^\|.+\|$\n?)+'
    text = re.sub(table_pattern, wrap_table, text, flags=re.MULTILINE)

    # 2) Разбиваем на код-блоки, чтобы не трогать их содержимое
    parts = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', text)

    result_parts = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # Код-блок или inline code
            if part.startswith('```'):
                # Многострочный код-блок: снимаем ``` и язык
                code_match = re.match(r'```(\w*)\n?([\s\S]*?)```', part)
                if code_match:
                    lang = code_match.group(1)
                    code = code_match.group(2).rstrip()
                    code = escape_html(code)
                    if lang:
                        result_parts.append(f'<pre><code class="language-{lang}">{code}</code></pre>')
                    else:
                        result_parts.append(f'<pre>{code}</pre>')
                else:
                    result_parts.append(f'<pre>{escape_html(part[3:-3])}</pre>')
            else:
                # Inline code
                code = part[1:-1]
                code = escape_html(code)
                result_parts.append(f'<code>{code}</code>')
        else:
            # Обычный текст: применяем форматирование
            fragment = part

            # Экранируем HTML-спецсимволы
            fragment = escape_html(fragment)

            # 3) Заголовки: ### Header -> <b>Header</b>
            fragment = re.sub(r'^\s*#{1,6}\s+(.*?)\s*$', r'<b>\1</b>\n', fragment, flags=re.MULTILINE)

            # 4) Жирный: **text** -> <b>text</b>
            # Используем [^*]+ вместо.*? для корректной работы с кавычками
            fragment = re.sub(r'\*\*([^*]+(?:\*(?!\*)[^*]*)*)\*\*', r'<b>\1</b>', fragment)

            # 5) Курсив: *text* или _text_ -> <i>text</i>
            fragment = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', fragment)
            fragment = re.sub(r'(?<!_)_([^_\n]+)_(?!_)', r'<i>\1</i>', fragment)

            # 6) Зачёркнутый: ~~text~~ -> <s>text</s>
            fragment = re.sub(r'~~(.*?)~~', r'<s>\1</s>', fragment)

            # 7) Списки: * item или - item -> • item
            fragment = re.sub(r'^\s*[\*\-]\s+', '• ', fragment, flags=re.MULTILINE)

            # 8) Ссылки: [text](url) -> <a href="url">text</a>
            def replace_link(match):
                link_text = match.group(1)
                url = match.group(2).replace('"', '&quot;')
                return f'<a href="{url}">{link_text}</a>'

            fragment = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', replace_link, fragment)

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
    paragraphs = text.split('\n\n')

    for paragraph in paragraphs:
        if len(paragraph) > max_length:
            if current_part:
                parts.append(current_part.strip())
                current_part = ""
            lines = paragraph.split('\n')
            for line in lines:
                if len(line) > max_length:
                    for i in range(0, len(line), max_length):
                        parts.append(line[i:i + max_length])
                elif len(current_part) + len(line) + 1 > max_length:
                    parts.append(current_part.strip())
                    current_part = line + '\n'
                else:
                    current_part += line + '\n'
        elif len(current_part) + len(paragraph) + 2 > max_length:
            parts.append(current_part.strip())
            current_part = paragraph + '\n\n'
        else:
            current_part += paragraph + '\n\n'

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
                parse_mode='HTML',
                reply_to_message_id=update.message.message_id if i == 0 else None
            )
        except Exception:
            try:
                await update.message.reply_text(
                    part,
                    reply_to_message_id=update.message.message_id if i == 0 else None
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
                timeout=TIMEOUT_MEDIUM  # Увеличен таймаут для поиска
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
            if any(code in error_str for code in ['429', '503', '500']):
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
                    model=model_name,
                    contents=prompt
                )
            ),
            timeout=TIMEOUT_LONG
        )

        for part in response.parts:
            if part.inline_data is not None:
                return part.inline_data.data, model_key

        raise ValueError("API не вернул изображение")

    except asyncio.TimeoutError:
        raise TimeoutError(f"Превышено время генерации ({TIMEOUT_LONG} сек)")


async def edit_image(images_bytes: list[bytes], prompt: str, user_id: int, model_key: str = 'pro') -> tuple[bytes, str]:
    """
    Редактирует изображение(я) через Gemini Image API.
    Принимает список байтов (одно или несколько фото) и текстовый промпт.
    """
    model_name = IMAGE_MODELS.get(model_key, IMAGE_MODELS['pro'])

    try:
        pil_images = []
        for img_bytes in images_bytes:
            pil_images.append(Image.open(io.BytesIO(img_bytes)))

        contents = pil_images + [prompt]

        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=model_name,
                    contents=contents
                )
            ),
            timeout=TIMEOUT_LONG
        )

        for img in pil_images:
            img.close()

        if not response or not response.parts:
            raise ValueError("API вернул пустой ответ")

        for part in response.parts:
            if part.inline_data is not None:
                return part.inline_data.data, model_key

        raise ValueError("API не вернул данные изображения (проверьте безопасность промпта)")

    except asyncio.TimeoutError:
        raise TimeoutError(f"Превышено время редактирования ({TIMEOUT_LONG} сек)")


async def handle_image_generation(update: Update, context, prompt: str, user_id: int):
    """Общая функция генерации изображения (устраняет дублирование)"""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    model_key = get_user_image_model(user_id, context)
    model_icon = "💎" if model_key == 'pro' else "⚡"
    thinking_msg = await update.message.reply_text(
        f"🎨 {model_icon} Генерирую изображение...",
        reply_to_message_id=update.message.message_id
    )

    try:
        result_data, used_model = await generate_image(prompt, context, user_id)
        await thinking_msg.delete()

        # Сначала текст с названием модели
        model_text = f"Модель: {used_model.capitalize()}{model_icon}"
        await update.message.reply_text(
            model_text,
            reply_to_message_id=update.message.message_id
        )

        # Сохраняем промпт для возможной перегенерации
        context.user_data['last_image_prompt'] = prompt

        # Кнопки под картинкой
        image_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Ещё", callback_data="img_regen"),
                InlineKeyboardButton("✏️ Изменить запрос", callback_data="img_change_prompt")
            ]
        ])


        # Потом сама картинка с кнопками
        await update.message.reply_photo(
            photo=result_data,
            reply_markup=image_keyboard,
            reply_to_message_id=update.message.message_id
        )


        # Логируем активность
        log_activity(user_id, update.effective_user.username, "img_gen", prompt[:30])

    except Exception as e:
        try: await thinking_msg.delete()
        except Exception as del_err: logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("IMAGE_GEN", str(e), user_id)
        error_msg = format_gemini_error(e, "IMAGE_GEN")
        await update.message.reply_text(
            error_msg,
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )

# --- YOUTUBE SUMMARIZER ---


def extract_video_id(url: str) -> str | None:
    """Извлекает video_id из YouTube ссылки"""
    patterns = [
        r'(?:youtube\.com\/watch\?v=)([\w-]+)',
        r'(?:youtu\.be\/)([\w-]+)',
        r'(?:youtube\.com\/embed\/)([\w-]+)',
        r'(?:youtube\.com\/shorts\/)([\w-]+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def get_youtube_preview(url: str) -> dict:

    """
    Получает превью и название YouTube видео через oEmbed API
    Источник: https://oembed.com/ и https://developers.google.com/youtube/oembed
    """
    video_id = extract_video_id(url)
    if not video_id:
        return {"success": False, "error": "🔗 Не удалось распознать ссылку YouTube"}
    
    try:
        # oEmbed API YouTube (без API ключа)
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        response = requests.get(oembed_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Thumbnail URL (максимальное качество)
        # oEmbed часто возвращает hqdefault, если maxresdefault нет
        thumbnail_url = data.get("thumbnail_url") or f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
        
        return {
            "success": True,
            "title": data.get("title", "Без названия"),
            "author": data.get("author_name", "YouTube"),
            "thumbnail_url": thumbnail_url,
            "original_url": url
        }
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return {"success": False, "error": "🔒 Видео не найдено или недоступно"}
        elif e.response.status_code == 401:
            return {"success": False, "error": "🔞 Видео с ограниченным доступом"}
        else:
            return {"success": False, "error": f"❌ Ошибка YouTube API: {e.response.status_code}"}
    except requests.exceptions.Timeout:
        return {"success": False, "error": "⏱️ Превышено время ожидания ответа от YouTube"}
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "🌐 Ошибка подключения к YouTube"}
    except Exception as e:
        logger.error(f"YouTube Preview: {e}")
        return {"success": False, "error": f"❌ Ошибка: {str(e)[:100]}"}


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
            fetched_transcript = ytt_api.fetch(video_id, languages=['ru', 'en'])
            full_text = ' '.join([snippet['text'] for snippet in fetched_transcript.to_raw_data()])
            logger.info(f"YouTube: Субтитры ({fetched_transcript.language_code}), {len(full_text)} символов")
            return {"success": True, "text": full_text, "language": fetched_transcript.language_code}
        except Exception as e:
            # Если не нашли ru/en, получаем дефолтные
            logger.debug(f"Языки ru/en недоступны, пробуем дефолтные: {e}")
            fetched_transcript = ytt_api.fetch(video_id)
            full_text = ' '.join([snippet['text'] for snippet in fetched_transcript.to_raw_data()])
            logger.info(f"YouTube: Субтитры ({fetched_transcript.language_code}), {len(full_text)} символов")
            return {"success": True, "text": full_text, "language": fetched_transcript.language_code}

    except Exception as e:
        error_str = str(e).lower()
        logger.error(f"YouTube: Ошибка получения субтитров: {e}")

        # Классификация ошибок для понятного отображения пользователю
        # Источник: https://github.com/jdepoix/youtube-transcript-api#exceptions
        if 'subtitles are disabled' in error_str or 'disabled' in error_str:
            return {
                "success": False,
                "error": "🚫 Субтитры отключены автором видео",
                "error_type": "disabled"
            }
        elif 'no transcript' in error_str or 'could not retrieve' in error_str:
            return {
                "success": False,
                "error": "📭 Субтитры недоступны для этого видео",
                "error_type": "not_available"
            }
        elif 'video unavailable' in error_str or 'video is unavailable' in error_str:
            return {
                "success": False,
                "error": "🔒 Видео недоступно (удалено или приватное)",
                "error_type": "video_unavailable"
            }
        elif 'age restricted' in error_str or 'age-restricted' in error_str:
            return {
                "success": False,
                "error": "🔞 Видео с возрастным ограничением",
                "error_type": "age_restricted"
            }
        elif 'connection' in error_str or 'timeout' in error_str or 'network' in error_str:
            return {
                "success": False,
                "error": f"🌐 Ошибка сети: {str(e)[:80]}",
                "error_type": "network"
            }
        else:
            # Техническая ошибка скрипта — показываем полную информацию
            return {
                "success": False,
                "error": f"Техническая ошибка: {str(e)[:150]}",
                "error_type": "script_error"
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
                    model=MODELS['flash'],
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION_FLASH
                    )
                )
            ),
            timeout=TIMEOUT_SHORT
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
        if 'quota' in error_str or 'rate limit' in error_str or '429' in error_str:
            return f"🚦 [GEMINI QUOTA] Превышен лимит запросов. Попробуй позже.\n`{error_full[:100]}`"
        elif 'blocked' in error_str or 'safety' in error_str or 'harmful' in error_str:
            return f"🛡️ [GEMINI SAFETY] Контент заблокирован фильтром безопасности.\n`{error_full[:100]}`"
        elif 'api key' in error_str or 'invalid' in error_str or '401' in error_str or '403' in error_str:
            return f"🔑 [GEMINI AUTH] Проблема с API ключом.\n`{error_full[:150]}`"
        elif 'model' in error_str and ('not found' in error_str or 'unavailable' in error_str):
            return f"🤖 [GEMINI MODEL] Модель недоступна.\n`{error_full[:100]}`"
        elif 'connection' in error_str or 'timeout' in error_str or 'network' in error_str:
            return f"🌐 [GEMINI NETWORK] Ошибка сети.\n`{error_full[:100]}`"
        elif '500' in error_str or '503' in error_str or 'internal' in error_str:
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
    if not transcript_result['success']:
        return {"success": False, "error": transcript_result['error'], "error_type": transcript_result.get('error_type')}

    summary = await create_summary(transcript_result['text'])

    return {"success": True, "summary": summary, "language": transcript_result.get('language')}

# --- ОБРАБОТЧИКИ КОМАНД ---


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id):
        # Уведомление для неавторизованных пользователей
        message = (
            "Привет! Вы можете сделать такого же бота бесплатно, "
            "по моему <a href=\"https://t.me/ChoronoNotes/107\">гайду</a>. "
            "Или написать мне в канал, я помогу."
        )
        return await update.message.reply_text(message, parse_mode='HTML', disable_web_page_preview=True)
    reset_session(context)
    model_key = get_model_key(context)
    model_icon = "💎" if model_key == 'pro' else "⚡"
    await update.message.reply_text(
        f"🔄 Контекст сброшен!\n{model_icon} Модель: <b>{model_key.upper()}</b>",
        parse_mode='HTML'
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статус бота (только для админа)"""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return  # Только админ видит статус

    model_key = get_model_key(context)
    model_name = MODELS[model_key]
    has_session = 'chat_session' in context.user_data
    last_time = context.user_data.get('last_activity', 0)

    uptime_sec = int(time.time() - bot_stats['start_time'])
    uptime_hours = uptime_sec // 3600
    uptime_min = (uptime_sec % 3600) // 60

    # Системная статистика
    cpu_usage = psutil.cpu_percent()
    ram = psutil.virtual_memory()
    # Кросс-платформенный путь диска
    disk_path = 'C:' if platform.system() == 'Windows' else '/'
    disk = psutil.disk_usage(disk_path)

    # Конвертация байт в ГБ
    ram_total_gb = f"{ram.total / (1024**3):.1f}"
    ram_used_gb = f"{ram.used / (1024**3):.1f}"

    disk_total_gb = f"{disk.total / (1024**3):.1f}"
    disk_used_gb = f"{disk.used / (1024**3):.1f}"

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
        # Статистика за сегодняшний день
        day_start = get_day_start()
        today_activity = [a for a in user_activity if a['timestamp'] >= day_start]
        
        # Считаем именно запросы к AI (текст, голос, фото, инлайн)
        ai_actions = ['chat', 'voice', 'image_gen', 'photo_analyze', 'photo_edit', 'inline', 'youtube_summary', 'translate']
        today_requests_count = len([a for a in today_activity if a['action'] in ai_actions])
        
        status_text += f"""
━━━━━━━━━━━━━━━━━━━━
💻 **Сервер** ({platform.system()})

🖥 CPU: {cpu_usage}%
💾 RAM: {ram_used_gb}/{ram_total_gb} GB ({ram.percent}%)
💿 Disk: {disk_used_gb}/{disk_total_gb} GB ({disk.percent}%)

━━━━━━━━━━━━━━━━━━━━
🔧 **Статистика бота**

⏱ Аптайм: {uptime_hours}ч {uptime_min}м
💬 Сообщений: {bot_stats['messages_count']}
🎤 Голосовых: {bot_stats['voice_count']}
❌ Ошибок: {bot_stats['errors_count']}
👤 Пользователей: {len(allowed_users)}
📈 Запросов сегодня: <b>{today_requests_count} / 1500</b>
"""
        if bot_stats['last_errors']:
            status_text += "\n📋 **Последние ошибки:**\n"
            for err in bot_stats['last_errors'][-5:]:
                err_msg = err['msg'][:40] if err['msg'] else 'unknown'
                status_text += f"`{err['time']}` {err['type']}: {err_msg}\n"

        # Статистика за сегодняшний день
        day_start = get_day_start()
        today_activity = [a for a in user_activity if a['timestamp'] >= day_start]

        # Группируем по пользователям
        user_stats = {}
        for act in today_activity:
            uid = act['user_id']
            if uid not in user_stats:
                user_stats[uid] = {
                    'username': act['username'],
                    'text': 0,
                    'voice': 0,
                    'img_gen': 0,
                    'img_analyze': 0,
                    'img_edit': 0
                }

            action = act['action']
            if action == 'text':
                user_stats[uid]['text'] += 1
            elif action == 'voice':
                user_stats[uid]['voice'] += 1
            elif action == 'img_gen':
                user_stats[uid]['img_gen'] += 1
            elif action == 'img_analyze':
                user_stats[uid]['img_analyze'] += 1
            elif action == 'img_edit':
                user_stats[uid]['img_edit'] += 1

        # Текущее время по Киеву
        now_kyiv = datetime.now(KYIV_TZ).strftime("%H:%M")

        status_text += "\n━━━━━━━━━━━━━━━━━━━━\n"
        status_text += f"📅 **Сегодня** (Киев {now_kyiv})\n\n"

        if user_stats:
            for uid, stats in user_stats.items():
                username = f"@{stats['username']}" if stats['username'] != 'Unknown' else f"ID:{uid}"
                total = sum([stats['text'], stats['voice'], stats['img_gen'], stats['img_analyze'], stats['img_edit']])

                status_text += f"👤 {username}: **{total}** действий\n"
                if stats['text'] > 0:
                    status_text += f"   💬 Текст: {stats['text']}\n"
                if stats['voice'] > 0:
                    status_text += f"   🎤 Голос: {stats['voice']}\n"
                if stats['img_gen'] > 0:
                    status_text += f"   🖼️ Генерация: {stats['img_gen']}\n"
                if stats['img_analyze'] > 0:
                    status_text += f"   Анализ: {stats['img_analyze']}\n"
                if stats['img_edit'] > 0:
                    status_text += f"   ✏️ Редактирование: {stats['img_edit']}\n"
        else:
            status_text += "Нет активности за сегодня\n"

    await update.message.reply_text(format_for_telegram(status_text), parse_mode='HTML')


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
    await update.message.reply_text(f"Ваш ID: <code>{update.effective_user.id}</code>", parse_mode='HTML')


async def set_pro_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    
    context.user_data['model'] = 'pro'
    reset_session(context)
    
    await update.message.reply_text(
        "💎 <b>Gemini Pro</b>\n\n"
        "Установлена мощная модель Pro.\n"
        "⚠️ _Примечание: если лимиты Free Tier исчерпаны, бот вернет ошибку квоты._",
        parse_mode='HTML'
    )


async def set_flash_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    context.user_data['model'] = 'flash'
    reset_session(context)
    await update.message.reply_text(
        f"⚡ Модель: <b>Gemini Flash</b>\n"
        f"\n"
        f"{MODELS['flash']}",
        parse_mode='HTML'
    )


async def youtube_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает режим YouTube саммари"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")

    context.user_data['mode'] = 'youtube_mode'
    await update.message.reply_text(
        "📺 Отправьте ссылку на YouTube видео:",
        reply_to_message_id=update.message.message_id
    )
    log_activity(user_id, update.effective_user.username, 'youtube_cmd', 'Режим активирован')

# --- КОМАНДЫ ДЛЯ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ ---


async def set_image_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает на Pro модель и активирует режим генерации изображений"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    
    uid_str = str(user_id)
    if uid_str not in user_settings:
        user_settings[uid_str] = {}
    user_settings[uid_str]['image_model'] = 'pro'
    save_user_settings()
    
    context.user_data['image_model'] = 'pro'
    context.user_data.pop('mode', None)
    
    await update.message.reply_text(
        "🎨 <b>Image Pro</b>\n\n"
        "💎 Установлена модель Pro высокого качества.\n"
        "⚠️ _Примечание: на бесплатном тарифе (Free Tier) может выдавать ошибку квоты._",
        parse_mode='HTML'
    )
    log_activity(user_id, update.effective_user.username, 'image_pro_mode', 'установлена вручную')
    
    uid_str = str(user_id)
    if uid_str not in user_settings:
        user_settings[uid_str] = {}
    user_settings[uid_str]['image_model'] = model_to_set
    save_user_settings()
    
    context.user_data['image_model'] = model_to_set
    context.user_data.pop('mode', None)
    log_activity(user_id, update.effective_user.username, 'image_pro_blocked', 'попытка выбора заблокирована')


async def set_image_flash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает на Flash модель и активирует режим генерации изображений"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    uid_str = str(user_id)
    if uid_str not in user_settings:
        user_settings[uid_str] = {}
    user_settings[uid_str]['image_model'] = 'flash'
    save_user_settings()

    context.user_data['image_model'] = 'flash'
    context.user_data.pop('mode', None)
    await update.message.reply_text(
        f"🎨 Глобальная модель для изображения:\n⚡ <b>Flash</b> {IMAGE_MODELS['flash']}",
        parse_mode='HTML'
    )
    log_activity(user_id, update.effective_user.username, 'image_flash_mode', 'установлена глобально')

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

**📄 Документы:** PDF, TXT, CSV, JSON → суммаризация

**⏱ Сброс:**
**.** — полный сброс | **выход** — выход из режима
🎙️ Голос → текст (Flash)

**👤 Админ:** /add ID /del ID"""
    await update.message.reply_text(format_for_telegram(help_text), parse_mode='HTML')


# --- ОБРАБОТЧИК ГОЛОСА ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")

    bot_stats['voice_count'] += 1
    thinking_msg = await update.message.reply_text("🎤 Слушаю...")

    try:
        voice_file = await update.message.voice.get_file()
        voice_data = await voice_file.download_as_bytearray()

        # Используем Flash для распознавания речи (быстрее)

        # Шаг 1: Распознаём речь в текст
        recognition_response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS['flash'],
                    contents=[
                        "Распознай речь в текст. Выведи ТОЛЬКО распознанный текст, без комментариев:",
                        genai_types.Part.from_bytes(data=bytes(voice_data), mime_type="audio/ogg")
                    ]
                )
            ),
            timeout=60.0
        )

        recognized_text = recognition_response.text if recognition_response and recognition_response.text else None

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

        await thinking_msg.delete()

        # Проверка на пустой ответ
        response_text = response.text if response and response.text else "Пустой ответ от API"

        # Формируем финальный ответ с показом распознанного текста
        final_text = f"🎤 *Распознано:* {recognized_text}\n\n{response_text}"
        await send_safe_message(update, final_text)

        # Логируем активность
        log_activity(user_id, update.effective_user.username, "voice", recognized_text[:30])

    except asyncio.TimeoutError:
        try: await thinking_msg.delete()
        except Exception as del_err: logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("VOICE_TIMEOUT", "Таймаут", user_id)
        await update.message.reply_text("Превышено время ожидания.", reply_to_message_id=update.message.message_id)

    except Exception as e:
        try: await thinking_msg.delete()
        except Exception as del_err: logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("VOICE", str(e), user_id)
        error_msg = format_gemini_error(e, "VOICE")
        await update.message.reply_text(error_msg, parse_mode='HTML', reply_to_message_id=update.message.message_id)

        if user_id != ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🚨 Voice Error\nUser: {user_id}\n<code>{str(e)[:200]}</code>",
                    parse_mode='HTML'
                )
            except Exception as notify_err: logger.debug(f"Не удалось уведомить админа: {notify_err}")

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

    # --- Обработка альбомов (media_group) ---
    # Если это часть альбома — собираем все фото
    if media_group_id:
        # Скачиваем это фото
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_bytes = bytes(await photo_file.download_as_bytearray())

        # Проверяем, есть ли уже данные об этом альбоме
        if media_group_id not in pending_albums:
            # Первое фото альбома — создаём запись
            pending_albums[media_group_id] = {
                'photos': [photo_bytes],
                'caption': caption,
                'user_id': user_id,
                'chat_id': update.effective_chat.id,
                'message_id': update.message.message_id,
                'timestamp': time.time(),
                'context': context  # Сохраняем context для обработки
            }

            # Запускаем отложенную обработку альбома
            asyncio.create_task(process_album_delayed(media_group_id, update, context))
            return
        else:
            # Дополнительное фото — добавляем к альбому
            if len(pending_albums[media_group_id]['photos']) < MAX_ALBUM_PHOTOS:
                pending_albums[media_group_id]['photos'].append(photo_bytes)
            # Обновляем caption если первое было пустым
            if not pending_albums[media_group_id]['caption'] and caption:
                pending_albums[media_group_id]['caption'] = caption
            return

    # --- Одиночное фото (без media_group_id) ---

    # Проверяем режим перевода -> перевод текста на изображении
    if context.user_data.get('mode') == 'translate' or caption_lower in ['перевод', 'пр', 'translate']:
        thinking_msg = await update.message.reply_text("Перевожу текст на изображении...", reply_to_message_id=update.message.message_id)

        try:
            # Получаем фото
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            photo_bytes = bytes(await photo_file.download_as_bytearray())

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
            result_data, used_model = await edit_image([photo_bytes], prompt, user_id, 'flash')

            await delete_safe(thinking_msg)
            
            # Сохраняем результат для кнопок
            context.user_data['last_generated_photo'] = result_data
            context.user_data['last_image_prompt'] = prompt

            # Кнопки под переведенной картинкой
            translate_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Еще", callback_data="img_regen"),
                ]
            ])

            await update.message.reply_photo(
                photo=result_data, 
                reply_markup=translate_keyboard,
                reply_to_message_id=update.message.message_id
            )

            context.user_data.pop('mode', None)
            log_activity(user_id, update.effective_user.username, "img_translate_image", used_model)
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
                            model=MODELS['flash'],
                            contents=[
                                genai_types.Part.from_bytes(data=photo_bytes, mime_type="image/jpeg"),
                                ocr_prompt
                            ]
                        )
                    ),
                    timeout=60.0
                )

                try: await delete_safe(thinking_msg)
                except Exception: pass
                
                response_text = response.text if response and response.text else "Не удалось распознать текст"

                await send_safe_message(update, response_text)

                context.user_data.pop('mode', None)
                log_activity(user_id, update.effective_user.username, "img_translate", "OCR+translate")
                return
            
            except Exception as fallback_error:
                try: await delete_safe(thinking_msg)
                except Exception: pass
                log_error("IMAGE_TRANSLATE_FALLBACK", str(fallback_error), user_id)
                error_msg = format_gemini_error(fallback_error, "IMAGE_TRANSLATE_FALLBACK")
                await send_safe_message(update, error_msg)
                context.user_data.pop('mode', None)
                return

        except asyncio.TimeoutError:
            try: await thinking_msg.delete()
            except Exception as del_err: logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
            log_error("IMAGE_TRANSLATE_TIMEOUT", "Таймаут", user_id)
            await update.message.reply_text("Превышено время обработки.", reply_to_message_id=update.message.message_id)
            context.user_data.pop('mode', None)
            return

        except Exception as e:
            try: await thinking_msg.delete()
            except Exception as del_err: logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
            log_error("IMAGE_TRANSLATE", str(e), user_id)
            await update.message.reply_text(f"Ошибка: <code>{escape_html(str(e)[:150])}</code>", parse_mode='HTML', reply_to_message_id=update.message.message_id)
            context.user_data.pop('mode', None)
            return

    if context.user_data.get('mode') == 'awaiting_edit_photo':
        try:
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            photo_bytes = bytes(await photo_file.download_as_bytearray())

            # Сохраняем фото и переходим в режим ожидания промпта
            context.user_data['photo_task'] = {
                'photos': [photo_bytes],
                'message_id': update.message.message_id,
                'timestamp': time.time()
            }
            context.user_data['mode'] = 'awaiting_edit_prompt'

            model_key = get_user_image_model(user_id, context)
            model_icon = "💎" if model_key == 'pro' else "⚡"

            await update.message.reply_text(
                f"📷 Фото получено! {model_icon}\n\n✏️ Опишите что нужно сделать с изображением:",
                reply_to_message_id=update.message.message_id
            )
            log_activity(user_id, update.effective_user.username, "edit_photo_received", "awaiting prompt")
            return
        except Exception as e:
            log_error("EDIT_PHOTO_RECEIVE", str(e), user_id)
            context.user_data.pop('mode', None)
            await update.message.reply_text(f"Ошибка: {str(e)[:100]}")
            return

    # Проверяем команду редактирования (Р/Редактировать)
    is_edit_short = caption_lower.startswith('р ') or caption_lower == 'р'
    is_edit_long = caption_lower.startswith('редактировать ') or caption_lower == 'редактировать'

    if is_edit_short or is_edit_long:
        # Логика редактирования остаётся ниже
        pass

    # Если фото без подписи и не в режиме перевода — предлагаем выбор (кнопки)
    if not (is_edit_short or is_edit_long):
        try:
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            photo_bytes = await photo_file.download_as_bytearray()

            # Сохраняем во временное хранилище как СПИСОК (для совместимости с альбомами)
            context.user_data['photo_task'] = {
                'photos': [bytes(photo_bytes)],  # Список изображений
                'caption': caption,  # Подпись к фото (используется при анализе)
                'message_id': update.message.message_id,
                'timestamp': time.time()
            }

            keyboard = [
                [
                    InlineKeyboardButton("🔍 Анализировать", callback_data="photo_analyze"),
                    InlineKeyboardButton("✏️ Редактировать", callback_data="photo_edit")
                ],
                [
                    InlineKeyboardButton("📝 Добавить описание", callback_data="photo_add_caption")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                "Что сделать с этим фото?",
                reply_markup=reply_markup,
                reply_to_message_id=update.message.message_id
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
            photo_file = await photo.get_file()
            photo_bytes = bytes(await photo_file.download_as_bytearray())

            context.user_data['photo_task'] = {
                'photos': [photo_bytes],
                'message_id': update.message.message_id,
                'timestamp': time.time()
            }
            context.user_data['mode'] = 'awaiting_edit_prompt'

            model_key = get_user_image_model(user_id, context)
            model_icon = "💎" if model_key == 'pro' else "⚡"

            return await update.message.reply_text(
                f"📷 Фото получено! {model_icon}\n\n✏️ Опишите что нужно сделать с изображением:",
                reply_to_message_id=update.message.message_id
            )
        except Exception as e:
            log_error("EDIT_PHOTO_SAVE", str(e), user_id)
            return await update.message.reply_text("Ошибка при сохранении фото")

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    thinking_msg = await update.message.reply_text("🎨 Редактирую изображение...", reply_to_message_id=update.message.message_id)

    try:
        # Получаем фото (берём самое большое разрешение)
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_bytes = await photo_file.download_as_bytearray()

        # Получаем модель для изображений
        model_key = get_user_image_model(user_id, context)
        model_icon = "💎" if model_key == 'pro' else "⚡"

        # Редактируем
        result_data, used_model = await edit_image([bytes(photo_bytes)], prompt, user_id, model_key)
        await thinking_msg.delete()

        # Сохраняем данные для перегенерации
        context.user_data['last_edit_data'] = {
            'photos': [bytes(photo_bytes)],
            'prompt': prompt,
            'model_key': model_key
        }

        # Сначала текстовое сообщение с информацией
        await update.message.reply_text(
            f"{model_icon} Отредактировано через <b>{IMAGE_MODELS[used_model]}</b>\n\n✏️ Запрос: {prompt}",
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )

        # Кнопки под отредактированной картинкой
        edit_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Еще", callback_data="img_edit_regen"),
                InlineKeyboardButton("✏️ Сменить промт", callback_data="img_edit_change_prompt")
            ]
        ])


        # Потом фото с кнопками
        await update.message.reply_photo(photo=result_data, reply_markup=edit_keyboard)


        # Логируем активность
        log_activity(user_id, update.effective_user.username, "img_edit", f"{used_model}: {prompt[:20]}")

    except Exception as e:
        try: await thinking_msg.delete()
        except Exception as del_err: logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("IMAGE_EDIT", str(e), user_id)
        error_msg = format_gemini_error(e, "IMAGE_EDIT")
        await update.message.reply_text(
            error_msg,
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )


async def process_album_delayed(media_group_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отложенная обработка альбома после сбора всех фото."""
    # Ждём пока все фото альбома придут
    await asyncio.sleep(ALBUM_WAIT_TIME)

    # Получаем данные альбома
    if media_group_id not in pending_albums:
        return  # Альбом уже обработан или удалён

    album_data = pending_albums.pop(media_group_id)
    photos_bytes = album_data['photos']
    caption = album_data['caption']
    user_id = album_data['user_id']
    chat_id = album_data['chat_id']
    message_id = album_data['message_id']

    caption_lower = caption.strip().lower()
    photos_count = len(photos_bytes)

    # Режим ожидания фото для редактирования (команда "р") для альбомов
    if context.user_data.get('mode') == 'awaiting_edit_photo':
        context.user_data['photo_task'] = {
            'photos': photos_bytes,
            'message_id': message_id,
            'timestamp': time.time()
        }
        context.user_data['mode'] = 'awaiting_edit_prompt'

        model_key = get_user_image_model(user_id, context)
        model_icon = "💎" if model_key == 'pro' else "⚡"

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📷 Получено {photos_count} фото (альбом)! {model_icon}\n\n✏️ Опишите что нужно сделать с изображениями:",
            reply_to_message_id=message_id
        )
        return

    # Проверяем команду редактирования (Р/Редактировать)
    is_edit_short = caption_lower.startswith('р ') or caption_lower == 'р'
    is_edit_long = caption_lower.startswith('редактировать ') or caption_lower == 'редактировать'

    if is_edit_short or is_edit_long:
        # Извлекаем промт
        if is_edit_long:
            prompt = caption.strip()[13:].strip()
        else:
            prompt = caption.strip()[1:].strip()

        if not prompt:
            # Нет промта — сохраняем альбом и переходим в режим ожидания промта
            context.user_data['photo_task'] = {
                'photos': photos_bytes,
                'message_id': message_id,
                'timestamp': time.time()
            }
            context.user_data['mode'] = 'awaiting_edit_prompt'

            model_key = get_user_image_model(user_id, context)
            model_icon = "💎" if model_key == 'pro' else "⚡"

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📷 Получено {photos_count} фото (альбом)! {model_icon}\n\n✏️ Опишите что нужно сделать с изображениями:",
                reply_to_message_id=message_id
            )
            return

        # Редактируем альбом
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        thinking_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🎨 Редактирую изображение",
            reply_to_message_id=message_id
        )

        try:
            # Получаем модель для изображений
            model_key = get_user_image_model(user_id, context)
            model_icon = "💎" if model_key == 'pro' else "⚡"

            result_data, used_model = await edit_image(photos_bytes, prompt, user_id, model_key)
            await delete_safe(thinking_msg)

            # Сначала текстовое сообщение с информацией
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{model_icon} Отредактировано {photos_count} фото через <b>{IMAGE_MODELS[used_model]}</b>\n\n✏️ Запрос: {prompt}",
                parse_mode='HTML',
                reply_to_message_id=message_id
            )

            # Потом фото отдельно
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=result_data
            )

            log_activity(user_id, update.effective_user.username, "img_edit_album", f"{used_model}, {photos_count} photos: {prompt[:15]}")

        except Exception as e:
            await delete_safe(thinking_msg)
            log_error("IMAGE_EDIT_ALBUM", str(e), user_id)
            error_msg = format_gemini_error(e, "IMAGE_EDIT_ALBUM")
            await context.bot.send_message(
                chat_id=chat_id,
                text=error_msg,
                parse_mode='HTML',
                reply_to_message_id=message_id
            )
    else:
        # Альбом без команды редактирования — показываем кнопки
        # Сохраняем все фото альбома в photo_task
        context.user_data['photo_task'] = {
            'photos': photos_bytes,  # Список всех изображений альбома
            'message_id': message_id,
            'timestamp': time.time()
        }

        keyboard = [
            [
                InlineKeyboardButton("Анализировать", callback_data="photo_analyze"),
                InlineKeyboardButton("✏️ Редактировать", callback_data="photo_edit")
            ],
            [
                InlineKeyboardButton("📝 Добавить описание", callback_data="photo_add_caption")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📷 Получено {photos_count} фото. Что сделать с альбомом?",
            reply_markup=reply_markup,
            reply_to_message_id=message_id
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

    # Получаем модель пользователя
    model_key = get_model_key(context)
    model_icon = "💎" if model_key == 'pro' else "⚡"

    # Проверяем MIME тип
    mime_type = document.mime_type or "application/octet-stream"
    supported_mimes = [
        'application/pdf',
        'text/plain',
        'text/csv',
        'text/html',
        'text/markdown',
        'application/json',
    ]

    # Проверяем поддержку формата
    is_supported = mime_type in supported_mimes or mime_type.startswith('text/')
    if not is_supported:
        return await update.message.reply_text(
            f"Формат `{mime_type}` не поддерживается.\nПоддерживаемые: PDF, TXT, CSV, JSON, HTML, Markdown",
            parse_mode='HTML'
        )

    # Подпись или дефолтный промт
    caption = update.message.caption or ""
    prompt = caption if caption else "Суммаризируй содержимое этого документа. Выдели ключевые моменты."

    thinking_msg = await update.message.reply_text(
        f"{model_icon} Анализирую документ...",
        reply_to_message_id=update.message.message_id
    )

    try:
        # Скачиваем файл
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()

        # Отправляем в Gemini через новый SDK
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS[model_key],
                    contents=[
                        genai_types.Part.from_bytes(data=bytes(file_bytes), mime_type=mime_type),
                        prompt
                    ]
                )
            ),
            timeout=120.0  # Больше времени для документов
        )

        await thinking_msg.delete()
        response_text = response.text if response and response.text else "Не удалось проанализировать документ"
        await send_safe_message(update, response_text)

        # Логируем
        log_activity(user_id, update.effective_user.username, "doc_analyze", f"{document.file_name[:20]}")

    except asyncio.TimeoutError:
        try: await thinking_msg.delete()
        except Exception as del_err: logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("DOC_ANALYZE_TIMEOUT", "Таймаут", user_id)
        await update.message.reply_text("Превышено время анализа документа.", reply_to_message_id=update.message.message_id)

    except Exception as e:
        try: await thinking_msg.delete()
        except Exception as del_err: logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("DOC_ANALYZE", str(e), user_id)
        error_msg = format_gemini_error(e, "DOC_ANALYZE")
        await update.message.reply_text(
            error_msg,
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ handle_message ---
# Рефакторинг: вынесены для снижения цикломатической сложности (Radon F → B)


async def _process_photo_edit_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
) -> bool:
    """
    Обрабатывает ввод промта для редактирования фото (mode='awaiting_edit_prompt').
    Возвращает True если обработано.
    """
    if context.user_data.get('mode') != 'awaiting_edit_prompt':
        return False

    if 'photo_task' not in context.user_data:
        context.user_data.pop('mode', None)
        await update.message.reply_text("Данные фото потеряны. Отправьте фото заново.")
        return True

    text = update.message.text
    prompt = text
    photo_task = context.user_data['photo_task']
    photos_bytes = photo_task['photos']
    photos_count = len(photos_bytes)
    orig_msg_id = photo_task['message_id']

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    thinking_msg = await update.message.reply_text(
        f"🎨 Редактирую {photos_count} изображения..." if photos_count > 1 else "🎨 Редактирую изображение...",
        reply_to_message_id=update.message.message_id
    )

    try:
        model_key = context.user_data.get('image_model', 'pro')
        model_icon = "💎" if model_key == 'pro' else "⚡"

        result_data, used_model = await edit_image(photos_bytes, prompt, user_id, model_key)
        await thinking_msg.delete()

        # Сохраняем данные для перегенерации
        context.user_data['last_edit_data'] = {
            'photos': photos_bytes,
            'prompt': prompt,
            'model_key': model_key
        }

        # Формируем caption
        if photos_count > 1:
            caption = f"{model_icon} Отредактировано {photos_count} фото через <b>{IMAGE_MODELS[used_model]}</b>\n\n✏️ Запрос: {prompt}"
        else:
            caption = f"{model_icon} Отредактировано через <b>{IMAGE_MODELS[used_model]}</b>\n\n✏️ Запрос: {prompt}"

        await update.message.reply_text(
            caption,
            parse_mode='HTML',
            reply_to_message_id=orig_msg_id
        )

        # Кнопка перегенерации редактирования
        regen_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Ещё", callback_data="img_edit_regen")]
        ])

        await update.message.reply_photo(photo=result_data, reply_markup=regen_keyboard)

        log_activity(user_id, update.effective_user.username, "img_edit_btn_done", f"{used_model}, {photos_count} photos: {prompt[:15]}")
        context.user_data.pop('mode', None)
        context.user_data.pop('photo_task', None)

    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("IMAGE_EDIT_BTN", str(e), user_id)
        error_msg = format_gemini_error(e, "IMAGE_EDIT_BTN")
        await update.message.reply_text(error_msg, parse_mode='HTML')
        context.user_data.pop('mode', None)

    return True


async def _process_photo_analyze_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
) -> bool:
    """
    Обрабатывает текст пользователя после кнопки "Добавить описание" для анализа фото.
    """
    if context.user_data.get('mode') != 'awaiting_photo_analyze_prompt':
        return False

    if 'photo_task' not in context.user_data:
        context.user_data.pop('mode', None)
        await update.message.reply_text("Данные фото потеряны. Отправьте фото заново.")
        return True

    prompt = (update.message.text or "").strip()
    if not prompt:
        await update.message.reply_text("Введите описание или вопрос к фото.", reply_to_message_id=update.message.message_id)
        return True

    photo_task = context.user_data['photo_task']
    photos_bytes = photo_task['photos']
    photos_count = len(photos_bytes)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    thinking_msg = await update.message.reply_text(
        f"⚡ Анализирую {photos_count} фото и описание..." if photos_count > 1 else "⚡ Анализирую фото и описание...",
        reply_to_message_id=update.message.message_id
    )

    try:
        contents = [
            genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            for img_bytes in photos_bytes
        ] + [
            "Проанализируй изображение(я) и текст пользователя как единый контекст. "
            "Дай точный и практичный ответ.\n\n"
            f"Текст пользователя: {prompt}"
        ]

        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS['flash'],
                    contents=contents
                )
            ),
            timeout=TIMEOUT_SHORT
        )

        await thinking_msg.delete()
        response_text = response.text if response and response.text else "Не удалось проанализировать изображение"
        await send_safe_message(update, response_text)

        context.user_data['active_image'] = {
            'photo_bytes': photos_bytes[0],
            'timestamp': time.time()
        }

        context.user_data.pop('mode', None)
        context.user_data.pop('photo_task', None)
        log_activity(user_id, update.effective_user.username, "img_analyze_prompt", prompt[:40])

    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("IMG_ANALYZE_PROMPT", str(e), user_id)
        error_msg = format_gemini_error(e, "IMG_ANALYZE_PROMPT")
        await update.message.reply_text(error_msg, parse_mode='HTML', reply_to_message_id=update.message.message_id)
        context.user_data.pop('mode', None)

    return True


async def _process_exit_commands(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lower_text: str
) -> bool:
    """
    Сбрасывает активный режим по команде выхода (выход/exit/quit/stop).
    Возвращает True если обработано.
    """
    if lower_text not in ['выход', 'exit', 'quit', 'stop']:
        return False

    current_mode = context.user_data.get('mode')
    if not current_mode:
        return False

    context.user_data.pop('mode', None)

    messages = {
        'translate': "✅ Режим переводчика выключен.",
        'image_gen': "✅ Режим генерации изображений выключен.",
        'youtube_mode': "✅ Режим YouTube саммари выключен.",
        'youtube_preview_mode': "✅ Режим YouTube превью выключен."
    }

    msg = messages.get(current_mode, "✅ Режим выключен.")
    await update.message.reply_text(msg, reply_to_message_id=update.message.message_id)
    return True


async def _process_fast_commands(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    stripped: str,
    lower_text: str,
    user_id: int
) -> bool:
    """
    Обрабатывает быстрые команды: п, ф, к, ю, пр, .
    Возвращает True если команда обработана.
    """
    # Включение режима переводчика (без текста)
    if lower_text in ['пр', 'перевод', 'translate']:
        context.user_data['mode'] = 'translate'
        await update.message.reply_text(
            "🗣 Отправьте текст для перевода на русский:",
            reply_to_message_id=update.message.message_id
        )
        return True

    # Мгновенный перевод с текстом (пр <текст>)
    if lower_text.startswith('пр ') or lower_text.startswith('перевод ') or lower_text.startswith('translate '):
        if lower_text.startswith('translate '):
            text_to_translate = stripped[10:].strip()
        elif lower_text.startswith('перевод '):
            text_to_translate = stripped[8:].strip()
        else:
            text_to_translate = stripped[3:].strip()

        if text_to_translate:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            prompt_text = f"Переведи этот текст на русский язык максимально точно и литературно, сохраняя стиль оригинала. Не добавляй никаких комментариев, только перевод:\n\n{text_to_translate}"

            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: gemini_client.models.generate_content(
                            model=MODELS.get('lite', MODELS['flash']),
                            contents=prompt_text
                        )
                    ),
                    timeout=TIMEOUT_SHORT
                )
                response_text = response.text if response and response.text else "Не удалось перевести"
                await send_safe_message(update, response_text)
                log_activity(user_id, update.effective_user.username, 'translate', text_to_translate[:30])
            except Exception as e:
                log_error("TRANSLATE", str(e), user_id)
                error_msg = format_gemini_error(e, "TRANSLATE")
                await update.message.reply_text(error_msg, parse_mode='HTML', reply_to_message_id=update.message.message_id)
            return True

    # Включение режима YouTube саммари (без ссылки)
    if lower_text in ['ю', 'ютуб', 'youtube', 'самари']:
        context.user_data['mode'] = 'youtube_mode'
        await update.message.reply_text(
            "📺 Отправьте ссылку на YouTube видео:",
            reply_to_message_id=update.message.message_id
        )
        log_activity(user_id, update.effective_user.username, 'youtube_request', 'Режим активирован')
        return True

    # Мгновенное саммари YouTube со ссылкой (ю <ссылка>)
    if lower_text.startswith('ю ') or lower_text.startswith('ютуб ') or lower_text.startswith('youtube ') or lower_text.startswith('самари '):
        if lower_text.startswith('youtube '):
            url = stripped[8:].strip()
        elif lower_text.startswith('самари '):
            url = stripped[7:].strip()
        elif lower_text.startswith('ютуб '):
            url = stripped[5:].strip()
        else:
            url = stripped[2:].strip()

        if url:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            thinking_msg = await update.message.reply_text(
                "⏳ Загружаю субтитры и создаю саммари...",
                reply_to_message_id=update.message.message_id
            )

            try:
                result = await summarize_youtube(url)
                await delete_safe(thinking_msg)

                if result['success']:
                    await send_safe_message(update, result['summary'])
                    log_activity(user_id, update.effective_user.username, 'youtube_summary', url)
                else:
                    await update.message.reply_text(
                        f"❌ {result['error']}",
                        reply_to_message_id=update.message.message_id
                    )
                    log_activity(user_id, update.effective_user.username, 'youtube_error', result['error'])
            except Exception as e:
                await delete_safe(thinking_msg)
                log_error("YOUTUBE", str(e), user_id)
                error_msg = format_gemini_error(e, "YOUTUBE")
                await update.message.reply_text(
                    error_msg,
                    parse_mode='HTML',
                    reply_to_message_id=update.message.message_id
                )
            return True

    # --- YOUTUBE ПРЕВЬЮ ---
    # Включение режима YouTube превью (без ссылки)
    if lower_text in ['превью', 'пре']:
        context.user_data['mode'] = 'youtube_preview_mode'
        await update.message.reply_text(
            "🖼️ Отправьте ссылку на YouTube видео для превью:",
            reply_to_message_id=update.message.message_id
        )
        log_activity(user_id, update.effective_user.username, 'preview_request', 'Режим активирован')
        return True

    # Мгновенное превью со ссылкой (превью <ссылка>)
    if lower_text.startswith('превью ') or lower_text.startswith('пре '):
        if lower_text.startswith('пре '):
            url = stripped[4:].strip()
        else:
            url = stripped[7:].strip()

        if url:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
            
            result = get_youtube_preview(url)
            
            if result['success']:
                # Формируем подпись: название + ссылка
                caption = f"🎬 {result['title']}\n{result['original_url']}"
                
                try:
                    await update.message.reply_photo(
                        photo=result['thumbnail_url'],
                        caption=caption,
                        reply_to_message_id=update.message.message_id
                    )
                    log_activity(user_id, update.effective_user.username, 'youtube_preview', url)
                except Exception as e:
                    logger.error(f"YouTube Preview send error: {e}")
                    await update.message.reply_text(
                        f"❌ Ошибка отправки превью: {str(e)[:100]}",
                        reply_to_message_id=update.message.message_id
                    )
            else:
                await update.message.reply_text(
                    result['error'],
                    reply_to_message_id=update.message.message_id
                )
            return True

    # Переключение моделей (Про / Флэш)
    if lower_text in ['п', 'про', 'pro']:

        context.user_data['model'] = 'pro'
        reset_session(context)
        await update.message.reply_text("Pro 💎", parse_mode='HTML', reply_to_message_id=update.message.message_id)
        return True

    if lower_text in ['ф', 'флеш', 'flash']:
        context.user_data['model'] = 'flash'
        reset_session(context)
        await update.message.reply_text("Flash ⚡", parse_mode='HTML', reply_to_message_id=update.message.message_id)
        return True

    # Сброс контекста
    if stripped == '.':
        was_in_mode = context.user_data.get('mode')
        reset_session(context)
        if was_in_mode == 'image_gen':
            await update.message.reply_text("🔄 Режим генерации отменён.", reply_to_message_id=update.message.message_id)
        elif was_in_mode == 'translate':
            await update.message.reply_text("🔄 Режим перевода отменён.", reply_to_message_id=update.message.message_id)
        else:
            await update.message.reply_text("🔄 Контекст сброшен.", reply_to_message_id=update.message.message_id)
        return True

    # КОМАНДА "К" или "КАРТИНКА" - генерация изображений
    if lower_text in ['к', 'картинка']:
        context.user_data['mode'] = 'image_gen'
        model_key = get_user_image_model(user_id, context)
        model_icon = "💎" if model_key == 'pro' else "⚡"
        await update.message.reply_text(
            f"🎨 {model_icon} Опишите что нарисовать:",
            reply_to_message_id=update.message.message_id
        )
        return True

    # Переключение модели картинок через "к про" или "к флеш"
    if lower_text in ['к про', 'к pro']:
        uid_str = str(user_id)
        if uid_str not in user_settings:
            user_settings[uid_str] = {}
        user_settings[uid_str]['image_model'] = 'pro'
        save_user_settings()

        context.user_data['image_model'] = 'pro'
        context.user_data.pop('mode', None)
        await update.message.reply_text(
            f"🎨 Глобальная модель для изображения:\n💎 <b>Pro</b> {IMAGE_MODELS['pro']}",
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )
        return True

    if lower_text in ['к флеш', 'к flash']:
        uid_str = str(user_id)
        if uid_str not in user_settings:
            user_settings[uid_str] = {}
        user_settings[uid_str]['image_model'] = 'flash'
        save_user_settings()

        context.user_data['image_model'] = 'flash'
        context.user_data.pop('mode', None)
        await update.message.reply_text(
            f"🎨 Глобальная модель для изображения:\n⚡ <b>Flash</b> {IMAGE_MODELS['flash']}",
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )
        return True

    # С промтом сразу после команды
    if lower_text.startswith('к ') or lower_text.startswith('картинка '):
        if lower_text.startswith('картинка '):
            prompt = stripped[9:].strip()
        else:
            prompt = stripped[2:].strip()

        await handle_image_generation(update, context, prompt, user_id)
        return True

    # КОМАНДА "Р" или "РЕДАКТИРОВАТЬ" - режим ожидания фото для редактирования
    if lower_text in ['р', 'редактировать', 'edit']:
        context.user_data['mode'] = 'awaiting_edit_photo'
        model_key = get_user_image_model(user_id, context)
        model_icon = "💎" if model_key == 'pro' else "⚡"
        await update.message.reply_text(
            f"✏️ {model_icon} Отправьте фото (или альбом) для редактирования:",
            reply_to_message_id=update.message.message_id
        )
        return True

    return False


async def _process_twitter_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    user_id: int
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
    context.user_data['pending_tweet'] = {
        'id': tweet_id,
        'url': tweet_url
    }

    # Кнопки действий
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Обсудить", callback_data="twitter_discuss"),
        InlineKeyboardButton("📤 Отправить", callback_data="twitter_send")
    ]])

    await update.message.reply_text(
        "Что вы хотите с этим сделать?",
        reply_markup=keyboard,
        reply_to_message_id=update.message.message_id
    )

    log_activity(user_id, update.effective_user.username, "twitter_link", tweet_url[:60])
    return True


async def _process_reply_to_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
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
    model_icon = "💎" if model_key == 'pro' else "⚡"

    thinking_msg = await update.message.reply_text(f"{model_icon} Анализирую...", reply_to_message_id=update.message.message_id)

    try:
        photo = update.message.reply_to_message.photo[-1]
        photo_file = await photo.get_file()
        photo_bytes = await photo_file.download_as_bytearray()

        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS[model_key],
                    contents=[
                        genai_types.Part.from_bytes(data=bytes(photo_bytes), mime_type="image/jpeg"),
                        prompt
                    ]
                )
            ),
            timeout=60.0
        )

        await thinking_msg.delete()
        response_text = response.text if response and response.text else "Не удалось проанализировать"
        await send_safe_message(update, response_text)
        bot_stats['messages_count'] += 1
        log_activity(user_id, update.effective_user.username, "img_analyze", f"reply: {prompt[:20]}")

    except asyncio.TimeoutError:
        await delete_safe(thinking_msg)
        log_error("IMAGE_ANALYZE_TIMEOUT", "Таймаут", user_id)
        await update.message.reply_text("Превышено время анализа.", reply_to_message_id=update.message.message_id)

    except Exception as e:
        await delete_safe(thinking_msg)
        log_error("IMAGE_ANALYZE", str(e), user_id)
        await update.message.reply_text(f"Ошибка: <code>{escape_html(str(e)[:150])}</code>", parse_mode='HTML', reply_to_message_id=update.message.message_id)

    return True


async def _process_translation_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    user_id: int
) -> None:
    """Переводит текст на русский (mode='translate')"""
    prompt_text = f"Переведи этот текст на русский язык максимально точно и литературно, сохраняя стиль оригинала. Не добавляй никаких комментариев, только перевод:\n\n{text}"

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS.get('lite', MODELS['flash']),
                    contents=prompt_text
                )
            ),
            timeout=TIMEOUT_SHORT
        )
        response_text = response.text if response and response.text else "Не удалось перевести"
        await send_safe_message(update, response_text)
        context.user_data.pop('mode', None)
    except Exception as e:
        log_error("TRANSLATE", str(e), user_id)
        await update.message.reply_text(f"Ошибка перевода: {str(e)[:100]}")


async def _process_youtube_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    user_id: int
) -> None:
    """Создаёт саммари YouTube видео (mode='youtube_mode')"""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    thinking_msg = await update.message.reply_text(
        "⏳ Загружаю субтитры и создаю саммари...",
        reply_to_message_id=update.message.message_id
    )

    try:
        result = await summarize_youtube(text)
        await thinking_msg.delete()

        if result['success']:
            await send_safe_message(update, result['summary'])
            log_activity(user_id, update.effective_user.username, 'youtube_summary', text)
        else:
            await update.message.reply_text(
                f"❌ {result['error']}",
                reply_to_message_id=update.message.message_id
            )
            log_activity(user_id, update.effective_user.username, 'youtube_error', result['error'])
    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("YOUTUBE", str(e), user_id)
        await update.message.reply_text(
            f"❌ Ошибка обработки YouTube: {str(e)[:100]}",
            reply_to_message_id=update.message.message_id
        )


async def _process_image_gen_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    user_id: int
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
    bot_stats['messages_count'] += 1

    # Режим изменения промпта для генерации (новое)
    if context.user_data.get('mode') == 'awaiting_new_image_prompt':
        context.user_data.pop('mode', None)
        return await _process_image_gen_mode(update, context, text, user_id)

    # Режим изменения промпта для редактирования (новое)
    if context.user_data.get('mode') == 'awaiting_new_edit_prompt':
        context.user_data.pop('mode', None)
        # Подставляем старые фото, но новый промпт
        last_edit = context.user_data.get('last_edit_data')
        if not last_edit:
            await update.message.reply_text("Данные фото потеряны. Отправьте фото заново.")
            return
        
        # Обновляем промпт в сохраненных данных
        last_edit['prompt'] = text
        context.user_data['photo_task'] = {
            'photos': last_edit['photos'],
            'message_id': update.message.message_id,
            'timestamp': time.time()
        }
        # Используем существующий обработчик промпта для редактирования
        context.user_data['mode'] = 'awaiting_edit_prompt'
        return await _process_photo_edit_prompt(update, context, user_id)

    # Режим YouTube саммари
    if context.user_data.get('mode') == 'youtube_mode':
        context.user_data.pop('mode', None)
        return await _process_youtube_mode(update, context, text, user_id)

    # Режим YouTube превью
    if context.user_data.get('mode') == 'youtube_preview_mode':
        context.user_data.pop('mode', None)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
        
        result = get_youtube_preview(text)
        
        if result['success']:
            # Формируем подпись: название + ссылка
            caption = f"🎬 {result['title']}\n{result['original_url']}"
            
            try:
                await update.message.reply_photo(
                    photo=result['thumbnail_url'],
                    caption=caption,
                    reply_to_message_id=update.message.message_id
                )
                log_activity(user_id, update.effective_user.username, 'youtube_preview', text)
            except Exception as e:
                logger.error(f"YouTube Preview send error: {e}")
                await update.message.reply_text(
                    f"❌ Ошибка отправки превью: {str(e)[:100]}",
                    reply_to_message_id=update.message.message_id
                )
        else:
            await update.message.reply_text(
                result['error'],
                reply_to_message_id=update.message.message_id
            )
        return

    # Режим переводчика
    if context.user_data.get('mode') == 'translate':

        return await _process_translation_mode(update, context, text, user_id)

    # 6. ОБЫЧНЫЙ ТЕКСТОВЫЙ ЧАТ

    # Проверяем активное изображение в контексте
    active_image = context.user_data.get('active_image')
    if active_image:
        elapsed = time.time() - active_image['timestamp']
        if elapsed > IMAGE_CONTEXT_TIMEOUT:
            context.user_data.pop('active_image', None)
            active_image = None

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    thinking_msg = await update.message.reply_text("❇️ Думаю...", reply_to_message_id=update.message.message_id)

    try:
        clean_text = text.replace(f'@{bot_username}', '').strip() if bot_username else text

        # Мультимодальный запрос с активным изображением
        if active_image:
            model_key = get_model_key(context)
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: gemini_client.models.generate_content(
                        model=MODELS[model_key],
                        contents=[
                            genai_types.Part.from_bytes(data=active_image['photo_bytes'], mime_type="image/jpeg"),
                            clean_text
                        ]
                    )
                ),
                timeout=TIMEOUT_SHORT
            )
        else:
            # Обычный текстовый чат с поиском
            chat = get_or_create_session(context)
            response = await send_with_retry(chat, clean_text)

        await delete_safe(thinking_msg)

        response_text = response.text if response and response.text else "Пустой ответ от API"
        await send_safe_message(update, response_text)

        model_key = get_model_key(context)
        log_activity(user_id, update.effective_user.username, "text", f"Model: {model_key}")

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
                    parse_mode='HTML'
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
        return ('gemini', text)

    cmd_word = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    # Перевод: пр / перевод
    if cmd_word in ('пр', 'перевод'):
        return ('translate', arg)

    # YouTube саммари: ю / ютуб
    if cmd_word in ('ю', 'ютуб'):
        return ('youtube', arg)

    # YouTube превью: пре / превью
    if cmd_word in ('пре', 'превью'):
        return ('preview', arg)

    # Всё остальное — Gemini
    return ('gemini', text)



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
                thumbnail_url=avatar_url
            )
        ]
        await query.answer(
            results,
            cache_time=1,
            button=InlineQueryResultsButton(
                text="➡️➡️➡️【Жми на меня】⬅️⬅️⬅️",
                start_parameter="guide"
            )
        )
        return

    # Пустой запрос — подсказка с доступными командами
    if not text:
        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="💡 Введите вопрос или команду",
                description="пр <текст> · ю <ссылка> · превью <ссылка> · или просто вопрос",
                input_message_content=InputTextMessageContent(
                    message_text="💡 Команды: пр, ю, превью — или просто вопрос",
                    parse_mode='HTML'
                ),
                thumbnail_url=avatar_url
            )
        ]
        await query.answer(results, cache_time=60)
        return

    # Определяем команду по префиксу
    cmd_type, cmd_arg = _parse_inline_command(text)

    # ВАЖНО: reply_markup обязательна! Без InlineKeyboardMarkup Telegram
    # не передаёт inline_message_id в ChosenInlineResult, и edit_message_text невозможен.
    loading_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳", callback_data="inline_loading")]
    ])

    # --- ПЕРЕВОД ---
    if cmd_type == 'translate':
        if not cmd_arg:
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="🌐 Перевод",
                    description="Введите: пр <текст для перевода>",
                    input_message_content=InputTextMessageContent(
                        message_text="🌐 Используйте: @bot пр <текст>"
                    ),
                    thumbnail_url=avatar_url
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
                    message_text="🌐 Перевожу...",
                    parse_mode='HTML'
                ),
                reply_markup=loading_keyboard,
                thumbnail_url=avatar_url
            )
        ]
        await query.answer(results, cache_time=0)
        return

    # --- YOUTUBE САММАРИ ---
    if cmd_type == 'youtube':
        if not cmd_arg:
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="📺 YouTube Саммари",
                    description="Введите: ю <ссылка на видео>",
                    input_message_content=InputTextMessageContent(
                        message_text="📺 Используйте: @bot ю <ссылка>"
                    ),
                    thumbnail_url=avatar_url
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
                    message_text="📺 Загружаю саммари...",
                    parse_mode='HTML'
                ),
                reply_markup=loading_keyboard,
                thumbnail_url=avatar_url
            )
        ]
        await query.answer(results, cache_time=0)
        return

    # --- YOUTUBE ПРЕВЬЮ ---
    if cmd_type == 'preview':
        if not cmd_arg:
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="🖼️ YouTube Превью",
                    description="Введите: превью <ссылка на видео>",
                    input_message_content=InputTextMessageContent(
                        message_text="🖼️ Используйте: @bot превью <ссылка>"
                    ),
                    thumbnail_url=avatar_url
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
                    thumbnail_url=avatar_url
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
                reply_markup=loading_keyboard
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
                message_text="Ищу ответ (╭ರ_•́)╭",
                parse_mode='HTML'
            ),
            reply_markup=loading_keyboard,
            thumbnail_url=avatar_url
        )
    ]
    await query.answer(results, cache_time=0)


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        if cmd_type == 'translate' and cmd_arg:
            prompt_text = f"Переведи этот текст на русский язык максимально точно и литературно, сохраняя стиль оригинала. Не добавляй никаких комментариев, только перевод:\n\n{cmd_arg}"
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: gemini_client.models.generate_content(
                        model=MODELS['flash'],
                        contents=prompt_text
                    )
                ),
                timeout=TIMEOUT_SHORT
            )
            response_text = response.text if response and response.text else "Не удалось перевести"
            formatted_text = format_for_telegram(response_text)
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=f"<b>🌐 Перевод:</b>\n{formatted_text}",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([])
            )
            log_activity(user.id, user.username, "inline_translate", cmd_arg[:30])
            return

        # --- YOUTUBE САММАРИ ---
        if cmd_type == 'youtube' and cmd_arg:
            result_yt = await summarize_youtube(cmd_arg)
            if result_yt['success']:
                formatted_text = format_for_telegram(result_yt['summary'])
                await context.bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=f"<b>📺 YouTube Саммари:</b>\n{formatted_text}",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup([])
                )
            else:
                await context.bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=f"❌ {result_yt['error']}",
                    reply_markup=InlineKeyboardMarkup([])
                )
            log_activity(user.id, user.username, "inline_youtube", cmd_arg[:30])
            return

        # --- YOUTUBE ПРЕВЬЮ ---
        if cmd_type == 'preview' and cmd_arg:
            preview = await asyncio.to_thread(get_youtube_preview, cmd_arg)
            if preview['success']:
                thumb_url = preview['thumbnail_url']
                title = preview['title']
                
                # Заменяем фото-заглушку на реальное превью видео
                await context.bot.edit_message_media(
                    inline_message_id=inline_message_id,
                    media=InputMediaPhoto(
                        media=thumb_url,
                        caption=f'🎬 <b>{escape_html(title)}</b>\n{preview["original_url"]}',
                        parse_mode='HTML'
                    ),
                    reply_markup=InlineKeyboardMarkup([])
                )
            else:
                # Если ошибка, меняем подпись у заглушки
                await context.bot.edit_message_caption(
                    inline_message_id=inline_message_id,
                    caption=f"❌ {preview['error']}",
                    reply_markup=InlineKeyboardMarkup([])
                )
            log_activity(user.id, user.username, "inline_preview", cmd_arg[:30])
            return

        # --- GEMINI (по умолчанию) ---
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS['flash'],
                    contents=text,
                    config=genai_types.GenerateContentConfig(
                        system_instruction="Отвечай кратко, но если тема обширная — выдели главное, опусти второстепенное. Используй интернет для поиска актуальной информации.",
                        tools=SEARCH_TOOLS
                    )
                )
            ),
            timeout=TIMEOUT_MEDIUM
        )

        response_text = response.text if response and response.text else "Не удалось получить ответ"
        formatted_text = format_for_telegram(response_text)

        # Длинные ответы сворачиваем в expandable blockquote
        if len(formatted_text) > 500:
            body = f"<blockquote expandable>{formatted_text}</blockquote>"
        else:
            body = formatted_text

        await context.bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=f"<b>✦ Gemini:</b> {body}\nฅ≽^◕⩊◕^≼⊃━✧゜",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([])
        )
        log_activity(user.id, user.username, "inline", text[:30])

    except asyncio.TimeoutError:
        logger.warning(f"Inline chosen timeout for user {user.id}")
        await context.bot.edit_message_text(
            inline_message_id=inline_message_id,
            text="⏱️ Превышено время ожидания. Попробуйте снова.",
            reply_markup=InlineKeyboardMarkup([])
        )

    except Exception as e:
        logger.warning(f"Inline chosen error: {e}")
        try:
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=f"❌ Ошибка: {str(e)[:200]}",
                reply_markup=InlineKeyboardMarkup([])
            )
        except Exception:
            pass


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки под фото/альбом и перегенерацию картинок"""
    query = update.callback_query
    user_id = query.from_user.id
    action = query.data

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

        if 'photo_task' not in context.user_data:
            return await query.edit_message_text("Данные фото устарели или отсутствуют. Отправьте фото заново.")

        # Проверяем таймаут (3 минуты)
        photo_data = context.user_data['photo_task']
        elapsed_time = time.time() - photo_data.get('timestamp', 0)

        if elapsed_time > PHOTO_BUTTON_TIMEOUT:
            # Данные устарели — удаляем и сообщаем
            context.user_data.pop('photo_task', None)
            return await query.edit_message_text(f"⏱ Время ожидания истекло ({PHOTO_BUTTON_TIMEOUT // 60} мин). Отправьте фото заново.")

    # Подготавливаем данные для фото-кнопок (если есть)
    photo_data = context.user_data.get('photo_task', {})
    photos_bytes = photo_data.get('photos', [])
    photos_count = len(photos_bytes)

    if action == "photo_analyze":
        await query.edit_message_text(f"Анализирую {photos_count} фото..." if photos_count > 1 else "Анализирую...")

        # Используем модель пользователя
        model_key = get_model_key(context)
        model_icon = "💎" if model_key == 'pro' else "⚡"

        # Формируем prompt: если есть подпись от пользователя — используем её
        user_caption = photo_data.get('caption', '').strip()
        if user_caption:
            prompt = user_caption
        else:
            prompt = "Сделай анализ фото"

        try:
            # Формируем contents: все изображения + prompt
            contents = [
                genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
                for img_bytes in photos_bytes
            ] + [prompt]

            response = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: gemini_client.models.generate_content(
                        model=MODELS[model_key],
                        contents=contents
                    )
                ),
                timeout=60.0
            )

            response_text = response.text if response and response.text else "Не удалось проанализировать изображение"

            # Отправляем ответ новым сообщением
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{model_icon} <b>Результат анализа ({photos_count} фото):</b>\n\n{format_for_telegram(response_text)}" if photos_count > 1 else f"{model_icon} <b>Результат анализа:</b>\n\n{format_for_telegram(response_text)}",
                parse_mode='HTML',
                reply_to_message_id=photo_data['message_id']
            )

            # Сохраняем первое изображение в контексте для последующих вопросов
            context.user_data['active_image'] = {
                'photo_bytes': photos_bytes[0],
                'timestamp': time.time()
            }

            # Очищаем временные данные кнопок
            context.user_data.pop('photo_task', None)

            log_activity(user_id, query.from_user.username, "img_analyze_btn", f"{model_key}, {photos_count} photos")

        except Exception as e:
            log_error("BTN_ANALYZE", str(e), user_id)
            error_msg = format_gemini_error(e, "BTN_ANALYZE")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=error_msg, parse_mode='HTML')

    elif action == "photo_add_caption":
        context.user_data['mode'] = 'awaiting_photo_analyze_prompt'
        await query.edit_message_text(
            "📝 Жду описания"
        )

    elif action == "img_regen":
        # Перегенерация картинки по сохранённому промпту
        last_prompt = context.user_data.get('last_image_prompt')
        if not last_prompt:
            return await query.answer("Промпт не найден. Сгенерируйте картинку заново.", show_alert=True)

        await query.answer("🔄 Перегенерирую...")
        model_key = get_user_image_model(user_id, context)
        model_icon = "💎" if model_key == 'pro' else "⚡"

        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
            result_data, used_model = await generate_image(last_prompt, context, user_id)

            # Сохраняем результат для возможности анализа
            context.user_data['last_generated_photo'] = result_data

            # Кнопки под картинкой
            image_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Ещё", callback_data="img_regen"),
                    InlineKeyboardButton("✏️ Изменить запрос", callback_data="img_change_prompt")
                ]
            ])


            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=result_data,
                reply_markup=image_keyboard
            )
            log_activity(user_id, query.from_user.username, "img_regen", last_prompt[:30])


        except Exception as e:
            log_error("IMG_REGEN", str(e), user_id)
            error_msg = format_gemini_error(e, "IMG_REGEN")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=error_msg,
                parse_mode='HTML'
            )

    elif action == "img_edit_regen":
        # Перегенерация редактирования по сохранённым данным
        last_edit = context.user_data.get('last_edit_data')
        if not last_edit:
            return await query.answer("Данные редактирования не найдены. Отправьте фото заново.", show_alert=True)

        await query.answer("🔄 Перегенерирую...")

        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
            result_data, used_model = await edit_image(
                last_edit['photos'],
                last_edit['prompt'],
                user_id,
                last_edit.get('model_key', 'pro')
            )

            # Кнопки под отредактированной картинкой
            edit_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 В ту же степь", callback_data="img_edit_regen"),
                    InlineKeyboardButton("✏️ Другие правки", callback_data="img_edit_change_prompt")
                ]
            ])


            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=result_data,
                reply_markup=edit_keyboard
            )
            log_activity(user_id, query.from_user.username, "img_edit_regen", last_edit['prompt'][:30])


        except Exception as e:
            log_error("IMG_EDIT_REGEN", str(e), user_id)
            error_msg = format_gemini_error(e, "IMG_EDIT_REGEN")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=error_msg,
                parse_mode='HTML'
            )

    elif action == "photo_edit":
        # Переводим в режим ожидания промта для редактирования
        # Редактирование всегда использует gemini-3-pro-image-preview (IMAGE_MODELS['pro'])
        context.user_data['mode'] = 'awaiting_edit_prompt'

        if photos_count > 1:
            msg = f"✏️ Введите описание того, что нужно сделать с {photos_count} фото:\n\n💎 Используется: <b>gemini-3-pro-image-preview</b>"
        else:
            msg = "✏️ Введите описание того, что нужно изменить или добавить на этом фото:\n\n💎 Используется: <b>gemini-3-pro-image-preview</b>"

        await query.edit_message_text(msg, parse_mode='HTML')
        # Данные фото не удаляем, они понадобятся в handle_message

    elif action == "photo_analyze_last":
        # Анализ последнего сгенерированного/отредактированного изображения
        photo_bytes = context.user_data.get('last_generated_photo')
        if not photo_bytes:
            # Пытаемся достать из данных редактирования если там пусто
            last_edit = context.user_data.get('last_edit_data')
            if last_edit and 'photos' in last_edit:
                # В данном контексте "последнее" это результат, но если его нет, 
                # берём оригинал для анализа. На самом деле нужно сохранять результат.
                await query.answer("Сначала сгенерируйте фото", show_alert=True)
                return

        context.user_data['photo_task'] = {
            'photos': [photo_bytes] if isinstance(photo_bytes, bytes) else photo_bytes,
            'message_id': query.message.message_id,
            'timestamp': time.time()
        }
        context.user_data['mode'] = 'awaiting_photo_analyze_prompt'
        await query.edit_message_text("🔍 О чем спросить у этого изображения?")

    elif action == "img_change_prompt":
        context.user_data['mode'] = 'awaiting_new_image_prompt'
        await query.edit_message_text("✏️ Введите новый запрос для генерации:")

    elif action == "img_edit_change_prompt":
        context.user_data['mode'] = 'awaiting_new_edit_prompt'
        await query.edit_message_text("✏️ Опишите другие правки для этого фото:")

    # --- TWITTER КНОПКИ ---

    elif action == "twitter_discuss":
        # Обсуждение твита через Gemini.
        # FxTwitter даёт нам текст твита (Twitter блокирует url_context Gemini).
        # Текст вставляем в промпт — Gemini анализирует и отвечает.
        tweet_data = context.user_data.get('pending_tweet')
        if not tweet_data:
            await query.answer("Данные устарели. Отправьте ссылку заново.", show_alert=True)
            return

        await query.answer()
        tweet_url = tweet_data['url']
        tweet_id = tweet_data['id']

        await query.edit_message_text("💬 Загружаю твит...")

        # Получаем текст твита через FxTwitter API (бесплатно, без авторизации)
        tweet_text = ""
        author = ""
        try:
            api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
            resp = await asyncio.wait_for(
                asyncio.to_thread(requests.get, api_url, timeout=10),
                timeout=15.0
            )
            if resp.status_code == 200:
                data = resp.json()
                tw = data.get('tweet', {})
                tweet_text = tw.get('text', '')
                author = tw.get('author', {}).get('screen_name', '')
        except Exception as e:
            logger.warning(f"FxTwitter fetch error (discuss): {e}")

        # Формируем промпт для Gemini с текстом твита
        if tweet_text:
            prompt = (
                f"Обсудим этот твит от @{author}:\n\n\"{tweet_text}\"\n\n"
                f"Ссылка: {tweet_url}"
            )
        else:
            # Фоллбек: если FxTwitter не вернул текст — даём только URL
            prompt = f"Обсудим этот твит: {tweet_url}"

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        thinking_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚡ Анализирую твит..."
        )

        try:
            chat = get_or_create_session(context)
            response_gemini = await asyncio.wait_for(
                asyncio.to_thread(chat.send_message, prompt),
                timeout=TIMEOUT_MEDIUM
            )
            await delete_safe(thinking_msg)

            response_text = response_gemini.text if response_gemini and response_gemini.text else "Не удалось получить ответ"
            formatted = format_for_telegram(response_text)

            # Разбиваем на части если длинный ответ
            for chunk_start in range(0, len(formatted), MAX_MESSAGE_LENGTH):
                chunk = formatted[chunk_start:chunk_start + MAX_MESSAGE_LENGTH]
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=chunk,
                    parse_mode='HTML'
                )

            log_activity(user_id, query.from_user.username, "twitter_discuss", tweet_url[:50])
            context.user_data.pop('pending_tweet', None)

        except Exception as e:
            await delete_safe(thinking_msg)
            log_error("TWITTER_DISCUSS", str(e), user_id)
            error_msg = format_gemini_error(e, "TWITTER_DISCUSS")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=error_msg,
                parse_mode='HTML'
            )

    elif action == "twitter_send":
        # Отправка медиа из твита (запрос к FxTwitter делается здесь)
        tweet_data = context.user_data.get('pending_tweet')
        if not tweet_data:
            await query.answer("Данные устарели. Отправьте ссылку заново.", show_alert=True)
            return

        await query.answer()
        tweet_url = tweet_data['url']
        tweet_id = tweet_data['id']

        await query.edit_message_text("📤 Загружаю медиа из твита...")

        # Получаем данные твита через FxTwitter API
        try:
            api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
            response = await asyncio.wait_for(
                asyncio.to_thread(requests.get, api_url, timeout=10),
                timeout=15.0
            )

            if response.status_code != 200:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"❌ Не удалось получить данные твита (HTTP {response.status_code})."
                )
                return

            data = response.json()
            tw = data.get('tweet', {})
            tweet_text = tw.get('text', '')
            author = tw.get('author', {}).get('screen_name', '')
            author_name = tw.get('author', {}).get('name', '')
            likes = tw.get('likes', 0)
            retweets = tw.get('retweets', 0)
            photos = []

            media = tw.get('media', {})
            if media:
                photos = [p['url'] for p in media.get('photos', [])]

            # Формируем подпись: автор + текст + статистика + ссылка
            caption_parts = []
            if author:
                caption_parts.append(f"🐦 {author_name} (@{author})")
            if tweet_text:
                caption_parts.append(tweet_text)
            if likes or retweets:
                stats = []
                if likes:
                    stats.append(f"❤️ {likes:,}")
                if retweets:
                    stats.append(f"🔁 {retweets:,}")
                caption_parts.append(" · ".join(stats))
            caption_parts.append(tweet_url)

            caption = "\n\n".join(caption_parts)[:1024]  # Лимит Telegram

            if not photos:
                # Нет медиа — просто отправляем текст с инфой
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=caption or "Медиа в этом твите не найдено."
                )
            elif len(photos) == 1:
                # Одно фото — Telegram сам скачивает по URL с CDN Twitter
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=photos[0],
                    caption=caption
                )
            else:
                # Несколько фото — альбом (MediaGroup)
                # Лимит Telegram: 10 фото в альбоме
                media_group = [
                    InputMediaPhoto(
                        media=url,
                        caption=caption if i == 0 else None  # Подпись только у первого
                    )
                    for i, url in enumerate(photos[:10])
                ]
                await context.bot.send_media_group(
                    chat_id=update.effective_chat.id,
                    media=media_group
                )

            log_activity(user_id, query.from_user.username, "twitter_send", f"{len(photos)} photos")
            context.user_data.pop('pending_tweet', None)

        except asyncio.TimeoutError:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⏱ Превышено время загрузки твита. Попробуйте ещё раз."
            )
        except Exception as e:
            log_error("TWITTER_SEND", str(e), user_id)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ Ошибка: <code>{escape_html(str(e)[:200])}</code>",
                parse_mode='HTML'
            )


# --- ЗАПУСК ---
if __name__ == '__main__':
    cleanup_log_files()
    load_activity_log()
    logger.info(f"Загружено {len(user_activity)} записей за сегодня")
    load_users()
    logger.info(f"Загружено {len(allowed_users)} пользователей")
    load_user_settings()
    logger.info(f"Загружены настройки пользователей: {len(user_settings)} шт.")

async def post_init(app: Application):
    """Настройка команд меню и уведомление админа после старта"""
    await app.bot.set_my_commands([
        ("start", "🔄 Сбросить контекст"),
        ("status", "📊 Статус бота"),
        ("youtube", "📺 YouTube Саммари"),
        ("imagepro", "🎨💎Image Pro"),
        ("imageflash", "🎨⚡Image Flash"),
        ("1model", "💎Text Gemini Pro"),
        ("2model", "⚡Text Gemini Flash"),
        ("help", "❓ Справка"),
    ])
    logger.info("Меню команд установлено")

    if ADMIN_ID:
        try:
            now = datetime.now(KYIV_TZ)
            start_time = now.strftime('%H:%M:%S')
            start_date = now.strftime('%d.%m.%Y')
            pro_model = MODELS.get('pro', '?')
            flash_model = MODELS.get('flash', '?')
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🟢 <b>Бот запущен!</b>\n"
                    f"📅 {start_date}\n"
                    f"⏰ {start_time}\n"
                    f"💎 Pro: <code>{pro_model}</code>\n"
                    f"⚡ Flash: <code>{flash_model}</code>"
                ),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление о старте: {e}")


def main():
    """Основная функция запуска бота"""
    # Инициализация моделей (безопасная, не роняет бот при старте без сети)
    initialize_models()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Команды
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('status', status_command))
    application.add_handler(CommandHandler('youtube', youtube_command))
    application.add_handler(CommandHandler('add', add_user))
    application.add_handler(CommandHandler('del', del_user))
    application.add_handler(CommandHandler('1model', set_pro_model))
    application.add_handler(CommandHandler('2model', set_flash_model))
    application.add_handler(CommandHandler('id', my_id))
    application.add_handler(CommandHandler('imagepro', set_image_pro))
    application.add_handler(CommandHandler('imageflash', set_image_flash))

    # Обработчики сообщений
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(InlineQueryHandler(handle_inline_query))
    application.add_handler(ChosenInlineResultHandler(handle_chosen_inline_result))

    # Глобальный обработчик ошибок
    application.add_error_handler(global_error_handler)

    logger.info(f"🚀 BOT STARTED. Pro: {MODELS.get('pro')} | Flash: {MODELS.get('flash')}")

    application.run_polling(drop_pending_updates=True)


# --- ЗАПУСК ---
if __name__ == '__main__':
    cleanup_log_files()
    load_activity_log()
    logger.info(f"Загружено {len(user_activity)} записей за сегодня")
    load_users()
    logger.info(f"Загружено {len(allowed_users)} пользователей")
    load_user_settings()
    logger.info(f"Загружены настройки пользователей: {len(user_settings)} шт.")
    
    main()
