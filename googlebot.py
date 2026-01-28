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
from datetime import datetime, timezone, timedelta

from google import genai as genai_client
from google.genai import types as genai_types
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, InlineQueryResultsButton
from telegram.constants import ChatType
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, InlineQueryHandler, filters
from telegram.error import NetworkError
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi

# Загрузка переменных окружения
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(dotenv_path=env_path)

# --- КОНФИГУРАЦИЯ ---

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("❌ Не найдены переменные окружения! Проверьте файл .env")

# Проверка ADMIN_ID — если не задан, некоторые команды будут недоступны
if not ADMIN_ID:
    print("ВНИМАНИЕ: ADMIN_ID не задан в .env! Админ-функции будут недоступны.")

# Настройки
MEMORY_TIMEOUT = 5 * 60  # 5 минут
MAX_RETRIES = 2

# Таймауты (в секундах)
TIMEOUT_SHORT = 60        # Перевод, YouTube саммари — обычно 5-15 сек
TIMEOUT_MEDIUM = 90       # Gemini чат с google_search — может искать долго
TIMEOUT_LONG = 180        # Генерация/редактирование изображений — самые долгие
PHOTO_BUTTON_TIMEOUT = 180    # Время жизни кнопок под фото (3 мин)
IMAGE_CONTEXT_TIMEOUT = 300   # Время жизни изображения в контексте (5 мин)

# Telegram лимиты
MAX_MESSAGE_LENGTH = 4000     # Максимальная длина сообщения
ALBUM_WAIT_TIME = 1.5         # Секунды ожидания остальных фото альбома (было 0.5 — слишком мало)
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
USERS_FILE = 'allowed_users.json'

# Настройка Gemini (старый SDK для совместимости)
# Настройка Gemini (старый SDK удален)
# genai.configure(api_key=GEMINI_API_KEY)

# Клиент нового SDK (для чатов с google_search и генерации изображений)
gemini_client = genai_client.Client(api_key=GEMINI_API_KEY)

# Инструменты для интернет-поиска и анализа URL
SEARCH_TOOLS = [
    {"google_search": {}},
    {"url_context": {}}
]

# Модели для генераций изображений (Nano Banana)
IMAGE_MODELS = {
    'pro': 'gemini-3-pro-image-preview',  # Nano Banana Pro (, thinking mode)
    'flash': 'gemini-2.5-flash-image'     # Nano Banana Flash (1024px, быстрый)
}

# Логирование с ротацией (экономия диска на e2-micro)
# Источник: https://docs.python.org/3/library/logging.handlers.html#rotatingfilehandler

# Константы для логирования
LOG_FILE = 'bot.log'
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

        for name in os.listdir('.'):
            if name == LOG_FILE or name.startswith(f"{LOG_FILE}."):
                try:
                    path = os.path.join('.', name)
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
    Использует только конкретные версии моделей.
    Источник: https://ai.google.dev/gemini-api/docs/gemini-3
    """
    required_pro = 'gemini-3-pro-preview'
    required_flash = 'gemini-3-flash-preview'  # Обновлено: gemini-flash-latest -> gemini-3-flash-preview

    try:
        # Получаем список доступных моделей
        available_models = gemini_client.models.list()
        available_names = [model.name.replace('models/', '') for model in available_models]

        # Проверяем доступность требуемых моделей
        if required_pro not in available_names:
            raise RuntimeError(f"❌ Модель {required_pro} недоступна в API! Доступные: {', '.join(available_names[:5])}")

        if required_flash not in available_names:
            raise RuntimeError(f"❌ Модель {required_flash} недоступна в API! Доступные: {', '.join(available_names[:5])}")

        return {'pro': required_pro, 'flash': required_flash}

    except Exception as e:
        logger.error(f"Ошибка при проверке моделей: {e}")
        raise  # Пробрасываем исключение дальше, чтобы бот не запустился с неправильными моделями


# Будет инициализировано в main()
MODELS = {}


def initialize_models() -> None:
    """Инициализирует глобальную переменную MODELS"""
    # global не нужен для update словаря
    try:
        MODELS.update(get_latest_models())
        logger.debug(f"✅ Модели: Pro={MODELS['pro']}, Flash={MODELS['flash']}")
    except Exception as e:
        logger.error(f"Critical Error: {e}")
        # Fallback values if offline to allow bot to start (but generation might fail)
        MODELS.update({
            'pro': 'gemini-3-pro-preview',
            'flash': 'gemini-flash-latest'
        })
        print(f"Работаем с дефолтными моделями (Offline mode): {MODELS}")

# --- ПАМЯТЬ БОТА ---
# Состояние пользователей хранится в context.user_data:
# - 'chat_session': сессия чата с Gemini
# - 'last_activity': timestamp последней активности
# - 'model': текущая модель (pro/flash)
# - 'image_model': модель для генерации изображений
# - 'mode': режим работы (translate, youtube_mode, image_gen, awaiting_edit_prompt)
# - 'photo_task': данные о фото для кнопок {'photos': [bytes], 'message_id': int, 'timestamp': float}
# - 'active_image': активное изображение в контексте {'photo_bytes': bytes, 'timestamp': float}


allowed_users = set()  # Глобальный список разрешённых пользователей

# Хранилище для сбора альбомов (media_group)
# Ключ: media_group_id, значение: {'photos': [bytes], 'caption': str, 'user_id': int, 'chat_id': int, 'message_id': int, 'timestamp': float}
# Источник: https://core.telegram.org/bots/api#message (media_group_id)
pending_albums = {}

# URL аватарки бота для инлайн-результатов (статический, без токена)
# Источник: https://raw.githubusercontent.com/Eniggman/GeminiTelegramBot/main/docs/avatar.jpg
BOT_AVATAR_URL = "https://raw.githubusercontent.com/Eniggman/GeminiTelegramBot/main/docs/avatar.jpg"

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
        'time': time.strftime('%d.%m %H:%M'),  # Дата + время для удобного просмотра
        'type': error_type,
        'msg': str(error_msg)[:100],
        'user': user_id
    }
    bot_stats['errors_count'] += 1
    bot_stats['last_errors'].append(error_entry)
    if len(bot_stats['last_errors']) > 10:
        bot_stats['last_errors'].pop(0)
    logger.error(f"{error_type}: {str(error_msg)[:200]}")


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

# --- ЛОГИРОВАНИЕ АКТИВНОСТИ ПОЛЬЗОВАТЕЛЕЙ ---
# Timezone для Украины (Киев)
KYIV_TZ = timezone(timedelta(hours=2))  # UTC+2

# Файл для логов активности
ACTIVITY_LOG_FILE = 'activity_log.json'

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
        with open(ACTIVITY_LOG_FILE, 'w') as f:
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
            with open(ACTIVITY_LOG_FILE, 'r') as f:
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
            with open(USERS_FILE, 'r') as f:
                allowed_users.update(set(json.load(f)))
        except Exception as e:
            logger.warning(f"Ошибка загрузки {USERS_FILE}: {e}")


def save_users() -> None:
    try:
        with open(USERS_FILE, 'w') as f:
            json.dump(list(allowed_users), f)
    except Exception as e:
        logger.warning(f"Ошибка сохранения пользователей: {e}")


def check_access(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in allowed_users


def get_bot_avatar_url() -> str:
    """
    Возвращает статический URL аватарки бота для инлайн-результатов.
    SECURITY: Используем публичный URL на GitHub вместо api.telegram.org/file/bot{token}/...
    чтобы не раскрывать токен бота в URL (он мог попасть в логи/кеши).
    """
    return BOT_AVATAR_URL


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


def get_or_create_session(context: ContextTypes.DEFAULT_TYPE) -> object:  # Возвращает Chat объект
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

    # Квота / Rate Limit
    if 'quota' in error_str or 'rate limit' in error_str or '429' in error_str:
        return f"🚦 {prefix}[QUOTA] Превышен лимит запросов. Попробуй позже.\n<code>{error_safe[:120]}</code>"

    # Фильтр безопасности
    if 'blocked' in error_str or 'safety' in error_str or 'harmful' in error_str or 'finish_reason' in error_str:
        return f"🛡️ {prefix}[SAFETY] Контент заблокирован фильтром безопасности.\n<code>{error_safe[:120]}</code>"

    # Проблемы с авторизацией
    if 'api key' in error_str or 'invalid' in error_str or '401' in error_str or '403' in error_str:
        return f"🔑 {prefix}[AUTH] Проблема с API ключом.\n<code>{error_safe[:150]}</code>"

    # Модель недоступна
    if 'model' in error_str and ('not found' in error_str or 'unavailable' in error_str or 'does not exist' in error_str):
        return f"🤖 {prefix}[MODEL] Модель недоступна.\n<code>{error_safe[:120]}</code>"

    # Слишком длинный запрос
    if 'token' in error_str and ('limit' in error_str or 'exceed' in error_str or 'too long' in error_str):
        return f"📏 {prefix}[TOKEN LIMIT] Запрос слишком длинный.\n<code>{error_safe[:100]}</code>"

    # Проблемы с сетью
    if 'connection' in error_str or 'timeout' in error_str or 'network' in error_str:
        return f"🌐 {prefix}[NETWORK] Ошибка сети.\n<code>{error_safe[:100]}</code>"

    # Ошибка сервера Google
    if '500' in error_str or '503' in error_str or 'internal' in error_str or 'server' in error_str:
        return f"💥 {prefix}[SERVER] Ошибка сервера Google.\n<code>{error_safe[:100]}</code>"

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

    # 1. Обнаружение и обработка Markdown-таблиц (|---|)
    # Оборачиваем таблицы в <pre> для моноширинного отображения
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

    # Паттерн для таблиц: строки начинающиеся с | и содержащие |
    table_pattern = r'(?:^\|.+\|$\n?)+'
    text = re.sub(table_pattern, wrap_table, text, flags=re.MULTILINE)

    # 2. Разделяем текст на код-блоки и обычный текст
    # Это защищает содержимое ``` блоков от дальнейшей обработки
    parts = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', text)

    result_parts = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # Это код-блок или inline code
            if part.startswith('```'):
                # Многострочный код-блок
                # Удаляем ``` и возможный язык программирования
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
                code = part[1:-1]  # Убираем `
                code = escape_html(code)
                result_parts.append(f'<code>{code}</code>')
        else:
            # Обычный текст - применяем форматирование
            fragment = part

            # Экранируем HTML-спецсимволы
            fragment = escape_html(fragment)

            # 3. Заголовки: ### Header -> <b>Header</b>
            fragment = re.sub(r'^\s*#{1,6}\s+(.*?)\s*$', r'<b>\1</b>\n', fragment, flags=re.MULTILINE)

            # 4. Жирный шрифт: **text** -> <b>text</b>
            # Используем [^*]+ вместо .*? для корректной работы с кавычками
            fragment = re.sub(r'\*\*([^*]+(?:\*(?!\*)[^*]*)*)\*\*', r'<b>\1</b>', fragment)

            # 5. Курсив: *text* или _text_ -> <i>text</i>
            fragment = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', fragment)
            fragment = re.sub(r'(?<!_)_([^_\n]+)_(?!_)', r'<i>\1</i>', fragment)

            # 6. Зачёркнутый: ~~text~~ -> <s>text</s>
            fragment = re.sub(r'~~(.*?)~~', r'<s>\1</s>', fragment)

            # 7. Списки: * item или - item -> • item
            fragment = re.sub(r'^\s*[\*\-]\s+', '• ', fragment, flags=re.MULTILINE)

            # 8. Ссылки: [text](url) -> <a href="url">text</a>
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
            # ПРИМЕЧАНИЕ: Использование chat.send_message обеспечивает автоматическую 
            # передачу Thought Signatures (Подписей мыслей), что необходимо для моделей Gemini 3.
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


async def generate_image(prompt: str, context) -> tuple[bytes, str]:
    """
    Генерирует изображение по промту через Gemini.
    Источник: https://ai.google.dev/gemini-api/docs/image-generation
    """
    model_key = context.user_data.get('image_model', 'pro')  # По умолчанию pro
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

        # Ищем изображение в ответе (по документации: response.parts)
        for part in response.parts:
            if part.inline_data is not None:
                # Напрямую берём байты из ответа API
                # Источник: https://ai.google.dev/gemini-api/docs/image-generation
                return part.inline_data.data, model_key

        raise ValueError("API не вернул изображение")

    except asyncio.TimeoutError:
        raise TimeoutError(f"Превышено время генерации ({TIMEOUT_LONG} сек)")


async def edit_image(images_bytes: list[bytes], prompt: str, user_id: int, model_key: str = 'pro') -> tuple[bytes, str]:
    """
    Редактирует изображение(я) по промту.
    Может принимать одно или несколько изображений.
    Модель зависит от выбора пользователя (/1model или /2model).
    Источник: https://ai.google.dev/gemini-api/docs/image-generation

    Args:
        images_bytes: Список байтов изображений (может быть одно или несколько)
        prompt: Текстовое описание редактирования
        user_id: ID пользователя для логирования
        model_key: 'pro' или 'flash' — какую модель использовать

    Returns:
        tuple[bytes, str]: (Отредактированное изображение, название модели)
    """
    # Используем модель в зависимости от выбора пользователя
    model_name = IMAGE_MODELS.get(model_key, IMAGE_MODELS['pro'])

    try:
        # Открываем все изображения через PIL
        # Источник: https://ai.google.dev/gemini-api/docs/image-generation#editing-images
        pil_images = []
        for img_bytes in images_bytes:
            pil_images.append(Image.open(io.BytesIO(img_bytes)))

        # Формируем contents: сначала изображения, потом промт
        contents = pil_images + [prompt]

        # Отправляем в Gemini API
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=model_name,
                    contents=contents
                )
            ),
            timeout=TIMEOUT_LONG
        )

        # Закрываем все PIL изображения для освобождения памяти
        for img in pil_images:
            img.close()

        # Ищем изображение в ответе (по документации: response.parts)
        for part in response.parts:
            if part.inline_data is not None:
                # Напрямую берём байты из ответа API
                return part.inline_data.data, model_key

        raise ValueError("API не вернул изображение")

    except asyncio.TimeoutError:
        raise TimeoutError(f"Превышено время редактирования ({TIMEOUT_LONG} сек)")


async def handle_image_generation(update: Update, context, prompt: str, user_id: int):
    """
    Общая функция генерации изображения.
    Вынесена для устранения дублирования кода.
    Источник: рефакторинг hello.txt Problem #3
    """
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    model_key = context.user_data.get('image_model', 'pro')
    model_icon = "💎" if model_key == 'pro' else "⚡"
    thinking_msg = await update.message.reply_text(
        f"🎨 {model_icon} Генерирую изображение...",
        reply_to_message_id=update.message.message_id
    )

    try:
        result_data, used_model = await generate_image(prompt, context)
        await thinking_msg.delete()

        # Сначала текст с названием модели
        model_text = f"Модель: {used_model.capitalize()}{model_icon}"
        await update.message.reply_text(
            model_text,
            reply_to_message_id=update.message.message_id
        )

        # Потом сама картинка
        await update.message.reply_photo(
            photo=result_data,
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
    """
    Извлекает video_id из YouTube ссылки
    Источник: Стандартный паттерн для парсинга YouTube URL
    """
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

    status_text = f"""📊 **Статус (🧪 ТЕСТОВЫЙ)**

🤖 Модель: **{model_key.upper()}**
{model_name}

💬 Сессия: {'активна ✅' if has_session else 'нет ❌'}
⏱ Активность: {activity_text}
⏳ Таймаут: {MEMORY_TIMEOUT // 60} мин.
"""

    if user_id == ADMIN_ID:
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
    await update.message.reply_text(f"💎 Модель: <b>Gemini Pro</b>\n{MODELS['pro']}", parse_mode='HTML')


async def set_flash_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    context.user_data['model'] = 'flash'
    reset_session(context)
    await update.message.reply_text(f"⚡ Модель: <b>Gemini Flash</b>\n{MODELS['flash']}", parse_mode='HTML')


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
    context.user_data['image_model'] = 'pro'
    context.user_data['mode'] = 'image_gen'  # Сразу ждём промпт
    await update.message.reply_text(
        f"🎨 💎 <b>Pro</b>\n{IMAGE_MODELS['pro']}\n\n✏️ Опишите что нарисовать:",
        parse_mode='HTML'
    )
    log_activity(user_id, update.effective_user.username, 'image_pro_mode', 'активирован')


async def set_image_flash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает на Flash модель и активирует режим генерации изображений"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    context.user_data['image_model'] = 'flash'
    context.user_data['mode'] = 'image_gen'  # Сразу ждём промпт
    await update.message.reply_text(
        f"🎨 ⚡ <b>Flash</b>\n{IMAGE_MODELS['flash']}\n\n✏️ Опишите что нарисовать:",
        parse_mode='HTML'
    )
    log_activity(user_id, update.effective_user.username, 'image_flash_mode', 'активирован')

# --- ПОМОЩЬ ---


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """🤖 **Справка**

**📋 Команды:**
/start - Сбросить контекст
/status - Статус бота
/1model - 💎 Gemini Pro
/2model - ⚡ Gemini Flash

**⚡ Быстрые команды ввиде букв:**
• **П** — Gemini Pro | **Ф** — Gemini Flash
? **??** ? ??????? ??????/???? (???? ???????????? ? ?????????)
• Автопоиск Google и анализ ссылок

**🖼️ Изображения:**
/imagepro - 💎 Pro модель (Nano banano Pro)
/imageflash - ⚡ Flash модель (Nano banano Flash)
• Напишите в чат букву **К** + `<описание>` — произойдет генерация картинки
• Напишите в чат букву **Р** (или **Редактировать**) — включится режим редактирования, затем отправьте фото
• Напишите в чат букву **Р** + `<инструкция>` + Фото — сразу редактирование фото
? ???????? ? ??? ????? **??** + ???? ? ??????? ?????? ?? ??????????? (???????????? ????????)

**📷 Мультимодальность:**
• Отправьте фото → появляются кнопки Анализировать | ✏️ Редактировать
• 📷 **Альбом (2-10 фото)** → поддержка нескольких изображений!
• После анализа — 5 мин контекста для вопросов об изображении
• Фото + подпись → мгновенный ответ с контекстом

**📄 Бот принимает документы:**
PDF, TXT, CSV, JSON → суммаризация или ответ

**⏱ Сброс и выход:**
**.** — полный сброс (контекст + изображение + режим)
**выход** / **exit** — выход из режима (перевод, YouTube, генерация)
/start — полный сброс сессии
Автосброс — через 5/3 минут бездействия

**🎙️ Голос:** голосовое → текст (только для флеш)

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
    Обрабатывает любое фото или альбом:
    - Режим 'translate' → OCR + перевод текста с изображения
    - Фото + подпись → ответ на вопрос с учётом изображения
    - Фото/альбом без подписи → выбор (анализ или редактирование)
    - Подпись 'Р'/'Редактировать' → редактирование изображения(й)

    Поддержка альбомов:
    - Telegram отправляет альбом как несколько сообщений с одинаковым media_group_id
    - Собираем все фото альбома и обрабатываем вместе
    Источник: https://core.telegram.org/bots/api#message (media_group_id)
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
    if context.user_data.get('mode') == 'translate':
        thinking_msg = await update.message.reply_text("Перевожу текст на изображении...", reply_to_message_id=update.message.message_id)

        try:
            # Получаем фото
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            photo_bytes = bytes(await photo_file.download_as_bytearray())

            model_key = context.user_data.get('image_model', 'pro')

            try:
                # Промпт для перевода прямо на изображении
                prompt = (
                    "Translate all text in the image to Russian. "
                    "Replace the original text in-place while preserving layout, "
                    "font style, size, and colors as closely as possible. "
                    "Keep the rest of the image unchanged. "
                    "Return only the edited image."
                )
                result_data, used_model = await edit_image([photo_bytes], prompt, user_id, model_key)

                await thinking_msg.delete()
                await update.message.reply_photo(photo=result_data, reply_to_message_id=update.message.message_id)

                context.user_data.pop('mode', None)
                log_activity(user_id, update.effective_user.username, "img_translate_image", used_model)
                return
            except Exception as e:
                log_error("IMAGE_TRANSLATE_EDIT", str(e), user_id)

                # Фоллбек: OCR + текстовый перевод
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

                await thinking_msg.delete()
                response_text = response.text if response and response.text else "Не удалось распознать текст"
                await send_safe_message(update, response_text)

                context.user_data.pop('mode', None)
                log_activity(user_id, update.effective_user.username, "img_translate", "OCR+translate")
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

            model_key = context.user_data.get('image_model', 'pro')
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
                'message_id': update.message.message_id,
                'timestamp': time.time()
            }

            keyboard = [
                [
                    InlineKeyboardButton("Анализировать", callback_data="photo_analyze"),
                    InlineKeyboardButton("✏️ Редактировать", callback_data="photo_edit")
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

            model_key = context.user_data.get('image_model', 'pro')
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
        model_key = context.user_data.get('image_model', 'pro')
        model_icon = "💎" if model_key == 'pro' else "⚡"

        # Редактируем
        result_data, used_model = await edit_image([bytes(photo_bytes)], prompt, user_id, model_key)
        await thinking_msg.delete()

        # Сначала текстовое сообщение с информацией
        await update.message.reply_text(
            f"{model_icon} Отредактировано через <b>{IMAGE_MODELS[used_model]}</b>\n\n✏️ Запрос: {prompt}",
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )

        # Потом фото отдельно
        await update.message.reply_photo(photo=result_data)

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
    """
    Отложенная обработка альбома после сбора всех фото.
    Ждёт ALBUM_WAIT_TIME секунд и обрабатывает собранный альбом.
    Источник: https://core.telegram.org/bots/api#message (media_group_id)
    """
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

        model_key = context.user_data.get('image_model', 'pro')
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

            model_key = context.user_data.get('image_model', 'pro')
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
            model_key = context.user_data.get('image_model', 'pro')
            model_icon = "💎" if model_key == 'pro' else "⚡"

            result_data, used_model = await edit_image(photos_bytes, prompt, user_id, model_key)
            await thinking_msg.delete()

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
            try: await thinking_msg.delete()
            except Exception as del_err: logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
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
        await update.message.reply_photo(photo=result_data)

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
        'youtube_mode': "✅ Режим YouTube саммари выключен."
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
                            model=MODELS['flash'],
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
    if lower_text in ['ю', 'ютуб', 'youtube']:
        context.user_data['mode'] = 'youtube_mode'
        await update.message.reply_text(
            "📺 Отправьте ссылку на YouTube видео:",
            reply_to_message_id=update.message.message_id
        )
        log_activity(user_id, update.effective_user.username, 'youtube_request', 'Режим активирован')
        return True

    # Мгновенное саммари YouTube со ссылкой (ю <ссылка>)
    if lower_text.startswith('ю ') or lower_text.startswith('ютуб ') or lower_text.startswith('youtube '):
        if lower_text.startswith('youtube '):
            url = stripped[8:].strip()
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
                await thinking_msg.delete()

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
                try:
                    await thinking_msg.delete()
                except Exception as del_err:
                    logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
                log_error("YOUTUBE", str(e), user_id)
                error_msg = format_gemini_error(e, "YOUTUBE")
                await update.message.reply_text(
                    error_msg,
                    parse_mode='HTML',
                    reply_to_message_id=update.message.message_id
                )
            return True

    # Переключение моделей (Про / Флэш)
    if lower_text in ['п', 'про', 'pro']:
        context.user_data['model'] = 'pro'
        reset_session(context)
        await update.message.reply_text("<i>Pro</i> 💎", parse_mode='HTML', reply_to_message_id=update.message.message_id)
        return True

    if lower_text == 'ф':
        context.user_data['model'] = 'flash'
        reset_session(context)
        await update.message.reply_text("<i>Flash</i> ⚡", parse_mode='HTML', reply_to_message_id=update.message.message_id)
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
        model_key = context.user_data.get('image_model', 'pro')
        model_icon = "💎" if model_key == 'pro' else "⚡"
        await update.message.reply_text(
            f"🎨 {model_icon} Опишите что нарисовать:",
            reply_to_message_id=update.message.message_id
        )
        return True

    # Переключение модели картинок через "к про" или "к флеш"
    if lower_text in ['к про', 'к pro']:
        context.user_data['image_model'] = 'pro'
        context.user_data['mode'] = 'image_gen'
        await update.message.reply_text(
            f"🎨 💎 <b>Pro</b>\n<code>{IMAGE_MODELS['pro']}</code>\n\n✏️ Опишите что нарисовать:",
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )
        return True

    if lower_text in ['к флеш', 'к flash']:
        context.user_data['image_model'] = 'flash'
        context.user_data['mode'] = 'image_gen'
        await update.message.reply_text(
            f"🎨 ⚡ <b>Flash</b>\n<code>{IMAGE_MODELS['flash']}</code>\n\n✏️ Опишите что нарисовать:",
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
        model_key = context.user_data.get('image_model', 'pro')
        model_icon = "💎" if model_key == 'pro' else "⚡"
        await update.message.reply_text(
            f"✏️ {model_icon} Отправьте фото (или альбом) для редактирования:",
            reply_to_message_id=update.message.message_id
        )
        return True

    return False


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
    prompt = text.strip() if text.strip() else "Опиши подробно что на этом изображении"

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
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
        log_error("IMAGE_ANALYZE_TIMEOUT", "Таймаут", user_id)
        await update.message.reply_text("Превышено время анализа.", reply_to_message_id=update.message.message_id)

    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")
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
                    model=MODELS['flash'],
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

    # Команды выхода (выход/exit/quit/stop)
    if await _process_exit_commands(update, context, lower_text):
        return

    # Быстрые команды (п, ф, к, ю, пр, .)
    if await _process_fast_commands(update, context, stripped, lower_text, user_id):
        return

    # Реплай на фото — анализ изображения
    if await _process_reply_to_photo(update, context, user_id):
        return

    # Подсчет сообщений
    bot_stats['messages_count'] += 1

    # Режим генерации изображений
    if context.user_data.get('mode') == 'image_gen':
        context.user_data.pop('mode', None)
        return await _process_image_gen_mode(update, context, text, user_id)

    # Режим YouTube саммари
    if context.user_data.get('mode') == 'youtube_mode':
        context.user_data.pop('mode', None)
        return await _process_youtube_mode(update, context, text, user_id)

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

        await thinking_msg.delete()

        response_text = response.text if response and response.text else "Пустой ответ от API"
        await send_safe_message(update, response_text)

        model_key = get_model_key(context)
        log_activity(user_id, update.effective_user.username, "text", f"Model: {model_key}")

    except Exception as e:
        try:
            await thinking_msg.delete()
        except Exception as del_err:
            logger.debug(f"Не удалось удалить thinking_msg: {del_err}")

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


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает inline-запросы (@bot_name текст).
    Триггер: запрос заканчивается на точку (.) или знак вопроса (?).
    Источник: https://docs.python-telegram-bot.org/en/stable/examples.inlinekeyboard.html
    """
    query = update.inline_query
    user_id = query.from_user.id
    text = query.query.strip()
    
    # Получаем статический URL аватарки бота для thumbnail
    avatar_url = get_bot_avatar_url()
    
    # Проверка доступа — неавторизованные получат кнопку перехода в бота
    if not check_access(user_id):
        
        results = [
            InlineQueryResultArticle(
                id="no_access",
                title="👋 Привет! Это приватный бот Энигмена",
                description="Хочешь такого же? Сделай бесплатно сам, или с его помощью, по гайду.",
                input_message_content=InputTextMessageContent(
                    message_text="Нажми на синюю кнопку"
                ),
                thumbnail_url=avatar_url
            )
        ]
        # Кнопка сверху — сразу перекидывает в бота
        await query.answer(
            results,
            cache_time=1,  # Временно 1 сек для отладки
            button=InlineQueryResultsButton(
                text="【Жми на меня】",
                start_parameter="guide"
            )
        )
        return
    
    # Пустой запрос — показываем подсказку
    if not text:
        results = [
            InlineQueryResultArticle(
                id="hint",
                title="💡 Введите вопрос",
                description="Закончите вопрос точкой (.) или (?)",
                input_message_content=InputTextMessageContent(
                    message_text="💡 Используйте: @bot_name ваш вопрос?"
                ),
                thumbnail_url=avatar_url
            )
        ]
        await query.answer(results, cache_time=60)
        return
    
    # Триггер: текст должен заканчиваться на точку или знак вопроса
    if not (text.endswith('.') or text.endswith('?')):
        results = [
            InlineQueryResultArticle(
                id="waiting",
                title="Поставьте в конце . или ?",
                description=f"Закончите точкой (.) или вопросом (?) — \"{text[:30]}...\"",
                input_message_content=InputTextMessageContent(
                    message_text="Поставьте в конце . или ?"
                ),
                thumbnail_url=avatar_url
            )
        ]
        await query.answer(results, cache_time=1)
        return
    
    try:
        # Уникальный ID для результата (требование Telegram)
        result_id = str(uuid.uuid4())
        
        # Используем Flash модель для быстрого ответа
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model=MODELS['flash'],
                    contents=text,
                    config=genai_types.GenerateContentConfig(
                        system_instruction="Ответ должен быть кратким и по делу. Используй интернет для поиска актуальной информации.",
                        tools=SEARCH_TOOLS
                    )
                )
            ),
            timeout=TIMEOUT_MEDIUM  # Увеличен таймаут для поиска
        )
        
        response_text = response.text if response and response.text else "Не удалось получить ответ"
        
        # Форматируем ответ для Telegram HTML
        formatted_text = format_for_telegram(response_text)
        
        # Формируем результат
        results = [
            InlineQueryResultArticle(
                id=result_id,
                title="💬 Ответ Gemini",
                description=response_text[:200] + "..." if len(response_text) > 200 else response_text,
                input_message_content=InputTextMessageContent(
                    message_text=formatted_text[:4096],
                    parse_mode='HTML'
                ),
                thumbnail_url=avatar_url
            )
        ]
        
        await query.answer(results, cache_time=10)
        log_activity(user_id, query.from_user.username, "inline", text[:30])
        
    except asyncio.TimeoutError:
        logger.warning(f"Inline query timeout for user {user_id}")
        results = [
            InlineQueryResultArticle(
                id="timeout",
                title="⏱️ Превышено время ожидания",
                description="Попробуйте более короткий вопрос",
                input_message_content=InputTextMessageContent(
                    message_text="⏱️ Запрос занял слишком много времени. Попробуйте снова."
                ),
                thumbnail_url=avatar_url
            )
        ]
        await query.answer(results, cache_time=5)
        
    except Exception as e:
        logger.warning(f"Inline query error: {e}")
        results = [
            InlineQueryResultArticle(
                id="error",
                title="Ошибка",
                description=str(e)[:100],
                input_message_content=InputTextMessageContent(
                    message_text=f"Ошибка: {str(e)[:200]}"
                ),
                thumbnail_url=avatar_url
            )
        ]
        await query.answer(results, cache_time=5)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки под фото/альбом"""
    query = update.callback_query
    user_id = query.from_user.id

    # Всегда отвечаем на callback, чтобы убрать часы ожидания на кнопке
    await query.answer()

    if not check_access(user_id):
        await query.answer("⛔️ Нет доступа.", show_alert=False)
        return

    if 'photo_task' not in context.user_data:
        return await query.edit_message_text("Данные фото устарели или отсутствуют. Отправьте фото заново.")

    # Проверяем таймаут (2 минуты = 120 секунд)
    photo_data = context.user_data['photo_task']
    elapsed_time = time.time() - photo_data.get('timestamp', 0)

    if elapsed_time > PHOTO_BUTTON_TIMEOUT:
        # Данные устарели — удаляем и сообщаем
        context.user_data.pop('photo_task', None)
        return await query.edit_message_text(f"⏱ Время ожидания истекло ({PHOTO_BUTTON_TIMEOUT // 60} мин). Отправьте фото заново.")

    action = query.data
    photos_bytes = photo_data['photos']  # Теперь это список!
    photos_count = len(photos_bytes)

    if action == "photo_analyze":
        await query.edit_message_text(f"Анализирую {photos_count} фото..." if photos_count > 1 else "Анализирую...")

        # Используем ОБЫЧНУЮ модель пользователя
        model_key = get_model_key(context)
        model_icon = "💎" if model_key == 'pro' else "⚡"

        # Формируем prompt и содержимое
        if photos_count > 1:
            prompt = f"Опиши подробно что на этих {photos_count} изображениях и как они связаны между собой"
        else:
            prompt = "Опиши подробно что на этом изображении"

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

            # СОХРАНЯЕМ первое изображение в контексте для последующих вопросов
            context.user_data['active_image'] = {
                'photo_bytes': photos_bytes[0],  # Берём первое фото
                'timestamp': time.time()
            }

            # Очищаем временные данные кнопок
            context.user_data.pop('photo_task', None)

            log_activity(user_id, query.from_user.username, "img_analyze_btn", f"{model_key}, {photos_count} photos")

        except Exception as e:
            log_error("BTN_ANALYZE", str(e), user_id)
            error_msg = format_gemini_error(e, "BTN_ANALYZE")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=error_msg, parse_mode='HTML')

    elif action == "photo_edit":
        # Переводим в режим ожидания промта для редактирования
        # Редактирование ВСЕГДА использует gemini-3-pro-image-preview (IMAGE_MODELS['pro'])
        context.user_data['mode'] = 'awaiting_edit_prompt'

        if photos_count > 1:
            msg = f"✏️ Введите описание того, что нужно сделать с {photos_count} фото:\n\n💎 Используется: <code>gemini-3-pro-image-preview</code>"
        else:
            msg = "✏️ Введите описание того, что нужно изменить или добавить на этом фото:\n\n💎 Используется: <code>gemini-3-pro-image-preview</code>"

        await query.edit_message_text(msg, parse_mode='HTML')
        # Данные фото НЕ удаляем, они понадобятся в handle_message


# --- ЗАПУСК ---
if __name__ == '__main__':
    cleanup_log_files()
    load_activity_log()
    logger.info(f"Загружено {len(user_activity)} записей за сегодня")
    load_activity_log()
    logger.info(f"Загружено {len(user_activity)} записей за сегодня")
    load_users()
    logger.info(f"Загружено {len(allowed_users)} пользователей")

    # Функция инициализации (меню)
    async def post_init(app):
        await app.bot.set_my_commands([
            ("start", "🔄 Сбросить контекст"),
            ("help", "❓ Справка"),
            ("youtube", "📺 YouTube Саммари"),
            ("status", "📊 Статус бота"),
            ("1model", "💎 Text Gemini Pro"),
            ("2model", "⚡ Text Gemini Flash"),
            ("imagepro", "Image💎 Pro"),
            ("imageflash", "Image⚡ Flash"),

        ])
        logger.info("Меню команд установлено")

    # Инициализация моделей (безопасная, не роняет бот при старте без сети)
    initialize_models()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Команды
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('youtube', youtube_command))
    application.add_handler(CommandHandler('status', status_command))

    application.add_handler(CommandHandler('add', add_user))
    application.add_handler(CommandHandler('del', del_user))
    application.add_handler(CommandHandler('1model', set_pro_model))
    application.add_handler(CommandHandler('2model', set_flash_model))
    application.add_handler(CommandHandler('id', my_id))

    # Команды для изображений
    application.add_handler(CommandHandler('imagepro', set_image_pro))
    application.add_handler(CommandHandler('imageflash', set_image_flash))

    # Обработчики сообщений
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))  # Обработчик фото
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))  # Обработчик документов
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(InlineQueryHandler(handle_inline_query))  # Инлайн-режим

    # Глобальный обработчик ошибок
    application.add_error_handler(global_error_handler)

    logger.info(f"🚀 BOT STARTED. Pro: {MODELS.get('pro')} | Flash: {MODELS.get('flash')}")

    application.run_polling(drop_pending_updates=True)
