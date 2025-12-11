import os
import json
import logging
import time
import asyncio
import io
import re
from datetime import datetime, timezone, timedelta
import google.generativeai as genai
from google import genai as genai_image
from PIL import Image
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# --- КОНФИГУРАЦИЯ ---

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("❌ Не найдены переменные окружения! Проверьте файл .env")

# Настройки
MEMORY_TIMEOUT = 10 * 60  # 10 минут
MAX_RETRIES = 2

# Системная инструкция для краткости и скорости
SYSTEM_INSTRUCTION = """Ты — интеллектуальный помощник с фокусом на глубину и качество мысли.
Главный принцип: МАКСИМУМ СМЫСЛА В МИНИМУМЕ СЛОВ.

1. Избегай "воды": клише, пустых вступлений ("Хороший вопрос..."), повторов и очевидных вещей.
2. Если тема позволяет — рассуждай глубоко, философски и нестандартно.
3. Не упрощай сложные темы, но объясняй их ясным и коротким языком.
4. Для простых вопросов ("как дела?", "переведи") — отвечай предельно кратко.
"""

# Файл с доступами
USERS_FILE = 'allowed_users.json'

# Настройка Gemini (текстовый чат)
genai.configure(api_key=GEMINI_API_KEY)

# Клиент для генерации изображений (google-genai SDK)
image_client = genai_image.Client(api_key=GEMINI_API_KEY)

# Модели для генерации изображений (Nano Banana)
IMAGE_MODELS = {
    'pro': 'gemini-3-pro-image-preview',  # Nano Banana Pro (, thinking mode)
    'flash': 'gemini-2.5-flash-image'     # Nano Banana Flash (1024px, быстрый)
}

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ФИКСИРОВАННЫЕ МОДЕЛИ С ПРОВЕРКОЙ ДОСТУПНОСТИ ---
def get_latest_models():
    """
    Использует только конкретные версии моделей.
    Источник: https://ai.google.dev/gemini-api/docs/gemini-3
    """
    required_pro = 'gemini-3-pro-preview'
    required_flash = 'gemini-flash-latest'  # Автоматически указывает на последнюю версию Flash
    
    try:
        # Получаем список доступных моделей
        available_models = genai.list_models()
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

MODELS = get_latest_models()
print(f"✅ Модели: Pro={MODELS['pro']}, Flash={MODELS['flash']}")

# --- ПАМЯТЬ БОТА ---
chat_sessions = {}
last_activity = {}
user_models = {}
allowed_users = set()
user_image_models = {}  # Модель для генерации изображений
user_modes = {}  # Режимы работы (chat, translate)

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
        'time': time.strftime('%H:%M:%S'),
        'type': error_type,
        'msg': str(error_msg)[:100],
        'user': user_id
    }
    bot_stats['errors_count'] += 1
    bot_stats['last_errors'].append(error_entry)
    if len(bot_stats['last_errors']) > 10:
        bot_stats['last_errors'].pop(0)
    logger.error(f"{error_type}: {str(error_msg)[:200]}")

# --- ЛОГИРОВАНИЕ АКТИВНОСТИ ПОЛЬЗОВАТЕЛЕЙ ---
# Timezone для Украины (Киев)
KYIV_TZ = timezone(timedelta(hours=2))  # UTC+2

# Файл для логов активности
ACTIVITY_LOG_FILE = 'activity_log.json'

# Структура логов
user_activity = []

def get_day_start():
    """Возвращает timestamp начала текущего дня по Киеву (00:00)"""
    now_kyiv = datetime.now(KYIV_TZ)
    day_start = now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.timestamp()

def log_activity(user_id: int, username: str, action: str, details: str = ""):
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
    
    # Периодически сохраняем (каждые 10 записей)
    if len(user_activity) % 10 == 0:
        save_activity_log()

def save_activity_log():
    """Сохраняет логи в файл"""
    try:
        with open(ACTIVITY_LOG_FILE, 'w') as f:
            json.dump(user_activity, f)
    except Exception as e:
        logger.warning(f"Activity log save error: {e}")

def load_activity_log():
    """Загружает логи из файла"""
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
def load_users():
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

def save_users():
    try:
        with open(USERS_FILE, 'w') as f: 
            json.dump(list(allowed_users), f)
    except Exception as e:
        logger.warning(f"Ошибка сохранения пользователей: {e}")

def check_access(user_id):
    return user_id == ADMIN_ID or user_id in allowed_users

def get_model_key(user_id):
    return user_models.get(user_id, 'flash')

def get_model_instance(model_key):
    return genai.GenerativeModel(
        model_name=MODELS[model_key],
        system_instruction=SYSTEM_INSTRUCTION
    )

# --- ФУНКЦИЯ СБРОСА КОНТЕКСТА ---
def reset_session(user_id):
    model_key = get_model_key(user_id)
    model = get_model_instance(model_key)
    chat_sessions[user_id] = model.start_chat(history=[])
    last_activity[user_id] = time.time()
    # Сбрасываем режим перевода при сбросе сессии
    if user_id in user_modes:
        del user_modes[user_id]
    return chat_sessions[user_id]

def get_or_create_session(user_id):
    """Получает сессию или создаёт новую если нужно"""
    current_time = time.time()
    last_time = last_activity.get(user_id, 0)
    
    # Проверяем таймаут
    if user_id not in chat_sessions or (current_time - last_time) > MEMORY_TIMEOUT:
        reset_session(user_id)
    else:
        last_activity[user_id] = current_time
    
    return chat_sessions[user_id]

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def clean_markdown(text: str) -> str:
    """Адаптирует Markdown для Telegram (Legacy)"""
    if not text: return ""
    
    # 1. Заголовки (### Header) -> *Header*
    text = re.sub(r'^\s*#{1,6}\s+(.*?)\s*$', r'*\1*\n', text, flags=re.MULTILINE)
    
    parts = re.split(r'(```[\s\S]*?```|`[^`]+`)', text)
    
    for i in range(0, len(parts), 2):
        fragment = parts[i]
        
        # 2. Убираем звёздочки (Telegram Legacy показывает их как текст)
        fragment = re.sub(r'\*\*(.*?)\*\*', r'\1', fragment)  # **text** -> text
        fragment = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'\1', fragment)  # *text* -> text
        
        # 3. Списки: * item -> • item
        fragment = re.sub(r'^\s*[\*\-]\s+', r'• ', fragment, flags=re.MULTILINE)
        
        # 4. Экранирование _ (Legacy Markdown не любит _)
        fragment = fragment.replace('_', r'\_')
        
        parts[i] = fragment
        
    return "".join(parts)

def split_message(text: str, max_length: int = 4000) -> list:
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
                        parts.append(line[i:i+max_length])
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
    """Отправляет сообщение с Markdown, разбивает длинные"""
    if not text:
        text = "⚠️ Пустой ответ от API"
    
    text = clean_markdown(text)
    parts = split_message(text, 4000)
    
    for i, part in enumerate(parts):
        if len(parts) > 1:
            part = f"📄 [{i+1}/{len(parts)}]\n\n{part}"
        
        try:
            await update.message.reply_text(
                part, 
                parse_mode='Markdown', 
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
    """Отправляет в Gemini с повторными попытками"""
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(chat.send_message, text),
                timeout=60.0
            )
            return response
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
                    logger.warning(f"Retry {attempt+1}/{retries} через {wait_time}с")
                    await asyncio.sleep(wait_time)
                    continue
            raise e
    raise last_error

# --- ФУНКЦИИ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ ---
async def generate_image(prompt: str, user_id: int) -> tuple[bytes, str]:
    """
    Генерирует изображение по промту через Gemini.
    Источник: https://ai.google.dev/gemini-api/docs/image-generation
    """
    model_key = user_image_models.get(user_id, 'pro')  # По умолчанию pro
    model_name = IMAGE_MODELS[model_key]
    
    try:
        # Генерация изображения - по документации просто contents
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: image_client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
            ),
            timeout=120.0
        )
        
        # Ищем изображение в ответе (по документации: response.parts)
        for part in response.parts:
            if part.inline_data is not None:
                # Напрямую берём байты из ответа API
                # Источник: https://ai.google.dev/gemini-api/docs/image-generation
                return part.inline_data.data, model_key
        
        raise ValueError("API не вернул изображение")
        
    except asyncio.TimeoutError:
        raise TimeoutError("Превышено время генерации (120 сек)")

async def edit_image(image_bytes: bytes, prompt: str, user_id: int) -> bytes:
    """
    Редактирует изображение по промту.
    Источник: https://ai.google.dev/gemini-api/docs/image-generation
    """
    model_key = user_image_models.get(user_id, 'pro')
    model_name = IMAGE_MODELS[model_key]
    
    try:
        # Открываем изображение через PIL для передачи в API
        input_image = Image.open(io.BytesIO(image_bytes))
        
        # Редактирование - передаём prompt и изображение
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: image_client.models.generate_content(
                    model=model_name,
                    contents=[prompt, input_image]
                )
            ),
            timeout=120.0
        )
        
        # Ищем изображение в ответе (по документации: response.parts)
        for part in response.parts:
            if part.inline_data is not None:
                # Напрямую берём байты из ответа API
                # Источник: https://ai.google.dev/gemini-api/docs/image-generation
                return part.inline_data.data
        
        raise ValueError("API не вернул изображение")
        
    except asyncio.TimeoutError:
        raise TimeoutError("Превышено время редактирования (120 сек)")

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    reset_session(user_id)
    model_key = get_model_key(user_id)
    model_icon = "💎" if model_key == 'pro' else "⚡"
    await update.message.reply_text(
        f"🔄 Контекст сброшен!\n{model_icon} Модель: **{model_key.upper()}**",
        parse_mode='Markdown'
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статус бота"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    
    model_key = get_model_key(user_id)
    model_name = MODELS[model_key]
    has_session = user_id in chat_sessions
    last_time = last_activity.get(user_id, 0)
    
    uptime_sec = int(time.time() - bot_stats['start_time'])
    uptime_hours = uptime_sec // 3600
    uptime_min = (uptime_sec % 3600) // 60
    
    if last_time:
        minutes_ago = int((time.time() - last_time) / 60)
        activity_text = f"{minutes_ago} мин. назад" if minutes_ago > 0 else "только что"
    else:
        activity_text = "нет данных"
    
    status_text = f"""📊 **Статус**

🤖 Модель: **{model_key.upper()}**
`{model_name}`

💬 Сессия: {'активна ✅' if has_session else 'нет ❌'}
⏱ Активность: {activity_text}
⏳ Таймаут: {MEMORY_TIMEOUT // 60} мин.
"""
    
    if user_id == ADMIN_ID:
        status_text += f"""
━━━━━━━━━━━━━━━━━━━━
🔧 **Статистика бота**

⏱ Аптайм: {uptime_hours}ч {uptime_min}м
💬 Сообщений: {bot_stats['messages_count']}
🎤 Голосовых: {bot_stats['voice_count']}
❌ Ошибок: {bot_stats['errors_count']}
👥 Сессий: {len(chat_sessions)}
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
                    status_text += f"   🔍 Анализ: {stats['img_analyze']}\n"
                if stats['img_edit'] > 0:
                    status_text += f"   ✏️ Редактирование: {stats['img_edit']}\n"
        else:
            status_text += "Нет активности за сегодня\n"
    
    await update.message.reply_text(status_text, parse_mode='Markdown')

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
            if target_id in chat_sessions: 
                del chat_sessions[target_id]
            if target_id in last_activity:
                del last_activity[target_id]
            await update.message.reply_text(f"🚫 ID {target_id} удален.")
        else: 
            await update.message.reply_text("Нет в списке.")
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш ID: `{update.effective_user.id}`", parse_mode='Markdown')

async def set_pro_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id): 
        return await update.message.reply_text("⛔️ Нет доступа.")
    user_models[user_id] = 'pro'
    reset_session(user_id)
    await update.message.reply_text(f"💎 Модель: *Gemini Pro*\n`{MODELS['pro']}`", parse_mode='Markdown')

async def set_flash_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id): 
        return await update.message.reply_text("⛔️ Нет доступа.")
    user_models[user_id] = 'flash'
    reset_session(user_id)
    await update.message.reply_text(f"⚡ Модель: *Gemini Flash*\n`{MODELS['flash']}`", parse_mode='Markdown')

# --- КОМАНДЫ ДЛЯ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ ---
async def set_image_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает на Pro модель для генерации изображений"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    user_image_models[user_id] = 'pro'
    await update.message.reply_text(
        f"🖼️ 💎 *Pro*\n`{IMAGE_MODELS['pro']}`\n\n💎 Высокое качество, медленнее",
        parse_mode='Markdown'
    )

async def set_image_flash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает на Flash модель для генерации изображений"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    user_image_models[user_id] = 'flash'
    await update.message.reply_text(
        f"🖼️⚡ *Flash*\n`{IMAGE_MODELS['flash']}`\n\n🚀 Быстрая генерация",
        parse_mode='Markdown'
    )

# --- ДИНАМИЧЕСКИЙ ТРЕКЕР МОДЕЛЕЙ ---

KNOWN_MODELS_FILE = 'known_models.json'

# Даты выпуска моделей (из официального changelog Google)
MODEL_RELEASE_DATES = {
    # Gemini 3 (Preview) - декабрь 2025
    'gemini-3-pro-preview': '12.2025',
    'gemini-3-pro-image-preview': '12.2025',
    
    # Gemini 2.5 Pro
    'gemini-2.5-pro': '03.2025',
    'gemini-2.5-pro-preview-tts': '11.2025',
    
    # Gemini 2.5 Flash
    'gemini-2.5-flash': '05.2025',
    'gemini-2.5-flash-lite': '06.2025',
    'gemini-2.5-flash-image': '05.2025',
    'gemini-2.5-flash-image-preview': '05.2025',
    'gemini-2.5-flash-preview-tts': '11.2025',
    'gemini-2.5-flash-live-preview': '10.2025',
    'gemini-2.5-flash-native-audio-latest': '09.2025',
    'gemini-2.5-computer-use': '10.2025',
    
    # Gemini Live
    'gemini-live-2.5-flash-preview': '11.2025',
    
    # Gemini 2.0
    'gemini-2.0-flash': '02.2025',
    'gemini-2.0-flash-001': '02.2025',
    'gemini-2.0-flash-exp': '12.2024',
    'gemini-2.0-flash-exp-image-generation': '02.2025',
    'gemini-2.0-flash-lite': '02.2025',
    'gemini-2.0-flash-lite-001': '02.2025',
    'gemini-2.0-flash-lite-preview': '02.2025',
    'gemini-2.0-flash-live-001': '02.2025',
    'gemini-2.0-flash-thinking-exp': '12.2024',
    'gemini-2.0-pro-exp': '02.2025',
    
    # Imagen 4
    'imagen-4.0-generate-001': '06.2025',
    'imagen-4.0-fast-generate-001': '06.2025',
    'imagen-4.0-ultra-generate-001': '06.2025',
    
    # Veo 3
    'veo-3.0-generate-001': '05.2025',
    'veo-3.0-fast-generate-001': '05.2025',
    'veo-3.1-generate-preview': '11.2025',
    'veo-3.1-fast-generate-preview': '11.2025',
    'veo-2.0-generate-001': '12.2024',
    
    # Embedding
    'text-embedding-004': '03.2024',
    'text-embedding-005': '09.2024',
    'embedding-001': '12.2023',
    'embedding-gecko-001': '06.2023',
    'gemini-embedding-001': '03.2025',
    'gemini-embedding-exp': '03.2025',
}

def load_known_models() -> set:
    """Загружает список известных моделей"""
    if os.path.exists(KNOWN_MODELS_FILE):
        try:
            with open(KNOWN_MODELS_FILE, 'r') as f:
                return set(json.load(f))
        except:
            pass
    return set()

def save_known_models(models: set):
    """Сохраняет список известных моделей"""
    try:
        with open(KNOWN_MODELS_FILE, 'w') as f:
            json.dump(list(models), f)
    except Exception as e:
        logger.warning(f"Ошибка сохранения known_models: {e}")

def parse_date_from_name(model_name: str) -> str:
    """Извлекает дату из названия модели"""
    
    # Паттерн MM-YYYY (например: -09-2025)
    match = re.search(r'-(\d{2})-(\d{4})$', model_name)
    if match:
        month, year = match.groups()
        return f"{month}.{year}"
    
    # Паттерн DD-MM в конце (например: -06-06, -03-07)
    match = re.search(r'-(\d{2})-(\d{2})$', model_name)
    if match:
        first, second = match.groups()
        # Если второе число <= 12, это скорее всего месяц.год
        # Если первое число > 12, это скорее всего день.месяц
        if int(first) > 12:
            # DD-MM формат -> DD.MM.2025
            return f"{first}.{second}.2025"
        else:
            # MM-YY или MM.2025 формат
            return f"{first}.2025"
    
    return None

def get_model_date(model_name: str) -> str:
    """Получает дату выпуска модели"""
    # Точное совпадение в словаре
    if model_name in MODEL_RELEASE_DATES:
        return MODEL_RELEASE_DATES[model_name]
    
    # Частичное совпадение в словаре
    for key, date in MODEL_RELEASE_DATES.items():
        if key in model_name:
            return date
    
    # Парсинг из названия
    parsed = parse_date_from_name(model_name)
    if parsed:
        return parsed
    
    return '—'

def categorize_model(name: str) -> str:
    """Определяет категорию модели"""
    name_lower = name.lower()
    
    if 'veo' in name_lower:
        return '🎬 Video'
    if 'imagen' in name_lower or ('image' in name_lower and 'gemini' in name_lower):
        return '🖼️ Image'
    if 'tts' in name_lower or 'audio' in name_lower or 'speech' in name_lower:
        return '🎤 Audio/TTS'
    if 'thinking' in name_lower:
        return '💭 Thinking'
    if 'gemini' in name_lower:
        return '🔤 Text'
    
    return None  # Вернёт None для моделей которые не показываем

async def all_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_access(user_id): 
        return await update.message.reply_text("⛔️ Нет доступа.")
    
    try:
        # Загружаем известные модели
        known_models = load_known_models()
        
        # Получаем все модели из API
        api_models = list(genai.list_models())
        
        # Фильтруем: только Gemini, Veo, Imagen (без Gemma, без старых)
        all_model_names = []
        for model in api_models:
            name = model.name.replace('models/', '')
            # Исключаем Gemma и устаревшие
            if 'gemma' in name.lower():
                continue
            if 'aqa' in name.lower():
                continue
            # Берём только актуальные версии (без embedding)
            if any(ver in name for ver in ['2.0', '2.5', '3', 'veo', 'imagen']):
                # Пропускаем embedding
                if 'embedding' in name.lower():
                    continue
                all_model_names.append(name)
        
        # Находим новые модели
        current_set = set(all_model_names)
        new_models = current_set - known_models
        new_categories = set()
        
        # Определяем новые категории
        old_categories = {categorize_model(m) for m in known_models}
        for m in new_models:
            cat = categorize_model(m)
            if cat not in old_categories:
                new_categories.add(cat)
        
        # Группируем по категориям
        categories = {}
        for name in all_model_names:
            cat = categorize_model(name)
            if cat is None:  # Пропускаем модели без категории
                continue
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(name)
        
        # Сортировка категорий
        cat_order = ['🔤 Text', '🎤 Audio/TTS', '🖼️ Image', '🎬 Video', '💭 Thinking']
        sorted_cats = sorted(categories.keys(), key=lambda x: cat_order.index(x) if x in cat_order else 99)
        
        # Формируем сообщение
        messages = []
        
        # Уведомление о новых моделях
        if new_models and known_models:  # Только если это не первый запуск
            new_msg = f"🆕 *Обнаружено новых моделей: {len(new_models)}*\n\n"
            for m in sorted(new_models):
                date = get_model_date(m)
                new_msg += f"• **{m}** — {date}\n"
            messages.append(new_msg)
        
        # Основной список
        main_msg = "📋 **Модели Gemini**\n"
        
        for cat in sorted_cats:
            models = categories[cat]
            # Сортируем модели по дате (новые сверху)
            def sort_by_date(model_name):
                date = get_model_date(model_name)
                # Парсим дату для сортировки (MM.YYYY или DD.MM.YYYY)
                if date == '—':
                    return (0, 0)  # Без даты - в конец
                parts = date.split('.')
                try:
                    if len(parts) == 2:  # MM.YYYY
                        return (int(parts[1]), int(parts[0]))
                    elif len(parts) == 3:  # DD.MM.YYYY
                        return (int(parts[2]), int(parts[1]), int(parts[0]))
                except:
                    pass
                return (0, 0)
            models.sort(key=sort_by_date, reverse=True)
            
            # Метка новой категории
            cat_label = f"*{cat}*" if cat in new_categories else f"*{cat}*"
            if cat in new_categories and known_models:
                cat_label += " 🆕"
            
            main_msg += f"\n{cat_label}\n"
            
            for m in models:
                date = get_model_date(m)
                # Новые модели выделяем жирным
                if m in new_models and known_models:
                    main_msg += f"• *{m}* — {date}\n"
                else:
                    main_msg += f"• `{m}` — {date}\n"
        
        messages.append(main_msg)
        
        # Сохраняем обновлённый список
        save_known_models(current_set)
        
        # Отправляем сообщения
        for msg in messages:
            # Разбиваем если слишком длинное
            if len(msg) > 4000:
                parts = split_message(msg, 4000)
                for part in parts:
                    await update.message.reply_text(part, parse_mode='Markdown')
            else:
                await update.message.reply_text(msg, parse_mode='Markdown')
                
    except Exception as e: 
        log_error("MODELS", str(e))
        await update.message.reply_text(f"⚠️ Ошибка: {str(e)[:100]}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """🤖 *Справка*

*Команды чата:*
/start - Сбросить контекст
/status - Статус
/1model - Gemini Pro
/2model - Gemini Flash
/models - Список моделей

*🖼️ Изображения:*
/imagepro - 🖼️ 💎 Pro (nano banana pro)
/imageflash - 🖼️⚡ Flash (nano banana flash)
*Отправьте просто текст в чат:*
🖼️Отправьте букву *К* — для генерации новой картинки
🖼️Отправьте картинку + сподписью *Р* или *Редактировать* — Генерация новой картинки с вашим описанием
📷 + *С* или *Смотри* — описание изображения
📷 → Reply: *С* или *Смотри* — описание изображения


*Быстрые:*
*П* — Gemini Pro | *Ф* — Gemini Flash | *.* — сброс контекста
*Пр* или *Перевод* — режим переводчика

*Голос:* отправьте голосовое → Бот ответит текстом

*Админ:* /add ID /del ID
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')



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
        
        # Используем Flash для голоса (быстрее)
        flash_model = get_model_instance('flash')
        
        response = await asyncio.wait_for(
            asyncio.to_thread(
                flash_model.generate_content,
                ["Распознай речь и ответь:", {"mime_type": "audio/ogg", "data": bytes(voice_data)}]
            ),
            timeout=120.0
        )
        
        await thinking_msg.delete()
        
        # Проверка на пустой ответ
        response_text = response.text if response and response.text else "⚠️ Не удалось распознать речь"
        await send_safe_message(update, response_text)
        
        # Логируем активность
        log_activity(user_id, update.effective_user.username, "voice", "")
        
    except asyncio.TimeoutError:
        try: await thinking_msg.delete()
        except: pass
        log_error("VOICE_TIMEOUT", "Таймаут", user_id)
        await update.message.reply_text("⚠️ Превышено время ожидания.", reply_to_message_id=update.message.message_id)
        
    except Exception as e:
        try: await thinking_msg.delete()
        except: pass
        log_error("VOICE", str(e), user_id)
        await update.message.reply_text("⚠️ Ошибка обработки голоса.", reply_to_message_id=update.message.message_id)
        
        if user_id != ADMIN_ID:
            try: 
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🚨 Voice Error\nUser: {user_id}\n`{str(e)[:200]}`",
                    parse_mode='Markdown'
                )
            except: pass

# --- ОБРАБОТЧИК ФОТО (РЕДАКТИРОВАНИЕ) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает фото с подписью 'С'/'Смотри' для анализа или 'Р' для редактирования"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        return await update.message.reply_text("⛔️ Нет доступа.")
    
    caption = update.message.caption or ""
    caption_lower = caption.strip().lower()
    
    # Проверяем режим ожидания фото для анализа
    current_mode = user_modes.get(user_id)
    is_analyze_mode = current_mode == 'image_analyze'
    
    # Если в режиме анализа ИЛИ подпись начинается с "С"/"Смотри"
    if is_analyze_mode or caption_lower.startswith('смотри') or caption_lower.startswith('с ') or caption_lower == 'с':
        # Извлекаем промт
        if caption_lower.startswith('смотри'):
            prompt = caption.strip()[6:].strip()  # после "смотри"
        else:
            prompt = caption.strip()[1:].strip()  # после "с"
        
        if not prompt:
            prompt = "Опиши подробно что на этом изображении"
        
        thinking_msg = await update.message.reply_text("🔍 Анализирую изображение...", reply_to_message_id=update.message.message_id)
        
        try:
            # Получаем фото (берём самое большое разрешение)
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            
            # Используем Flash-модель для анализа
            flash_model = get_model_instance('flash')
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    flash_model.generate_content,
                    [{"mime_type": "image/jpeg", "data": bytes(photo_bytes)}, prompt]
                ),
                timeout=60.0
            )
            
            await thinking_msg.delete()
            
            # Проверка на пустой ответ
            response_text = response.text if response and response.text else "⚠️ Не удалось проанализировать изображение"
            await send_safe_message(update, response_text)
            bot_stats['messages_count'] += 1
            
            # Логируем активность
            log_activity(user_id, update.effective_user.username, "img_analyze", prompt[:30])
            
            # Сбрасываем режим ожидания
            if user_id in user_modes and user_modes[user_id] == 'image_analyze':
                del user_modes[user_id]
            return
            
        except asyncio.TimeoutError:
            try: await thinking_msg.delete()
            except: pass
            log_error("IMAGE_ANALYZE_TIMEOUT", "Таймаут", user_id)
            await update.message.reply_text("⚠️ Превышено время анализа.", reply_to_message_id=update.message.message_id)
            return
            
        except Exception as e:
            try: await thinking_msg.delete()
            except: pass
            log_error("IMAGE_ANALYZE", str(e), user_id)
            await update.message.reply_text(
                f"⚠️ Ошибка анализа:\n`{str(e)[:150]}`",
                parse_mode='Markdown',
                reply_to_message_id=update.message.message_id
            )
            return
    
    # КОМАНДА "Р" или "РЕДАКТИРОВАТЬ" - редактирование изображения
    is_edit_short = caption_lower.startswith('р ') or caption_lower == 'р'
    is_edit_long = caption_lower.startswith('редактировать ') or caption_lower == 'редактировать'

    if not (is_edit_short or is_edit_long):
        return  # Не редактирование, игнорируем
    
    # Извлекаем промт
    if is_edit_long:
        prompt = caption.strip()[13:].strip() # "редактировать" = 13 символов
    else:
        prompt = caption.strip()[1:].strip()
    if not prompt:
        return await update.message.reply_text(
            "⚠️ Укажите описание редактирования после команды",
            parse_mode='Markdown'
        )
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    thinking_msg = await update.message.reply_text("🎨 Редактирую изображение...", reply_to_message_id=update.message.message_id)
    
    try:
        # Получаем фото (берём самое большое разрешение)
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        
        # Редактируем
        result_data = await edit_image(bytes(photo_bytes), prompt, user_id)
        await thinking_msg.delete()
        
        model_key = user_image_models.get(user_id, 'pro')
        model_icon = "💎" if model_key == 'pro' else "⚡"
        
        await update.message.reply_photo(
            photo=result_data,
            caption=f"{model_icon} Отредактировано через *{model_key.upper()}*",
            parse_mode='Markdown',
            reply_to_message_id=update.message.message_id
        )
        
        # Логируем активность
        log_activity(user_id, update.effective_user.username, "img_edit", prompt[:30])
        
    except Exception as e:
        try: await thinking_msg.delete()
        except: pass
        log_error("IMAGE_EDIT", str(e), user_id)
        await update.message.reply_text(
            f"⚠️ Ошибка редактирования:\n`{str(e)[:200]}`",
            parse_mode='Markdown',
            reply_to_message_id=update.message.message_id
        )

# --- ОБРАБОТЧИК СООБЩЕНИЙ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверка на существование сообщения
    if not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    text = update.message.text
    
    if not check_access(user_id):
        if chat_type == ChatType.PRIVATE: 
            await update.message.reply_text("⛔️ Нет доступа.")
        return

    bot_username = context.bot.username
    
    # Безопасная проверка reply_to_message
    is_reply_to_bot = False
    if update.message.reply_to_message:
        reply_user = update.message.reply_to_message.from_user
        if reply_user:
            is_reply_to_bot = reply_user.id == context.bot.id
    
    is_mentioned = bot_username and bot_username in text
    
    if chat_type != ChatType.PRIVATE and not (is_reply_to_bot or is_mentioned): 
        return

    # Быстрые команды
    stripped = text.strip()
    lower_text = stripped.lower()
    
    # Сброс режима (выход)
    if lower_text in ['выход', 'exit', 'quit', 'stop']:
        current_mode = user_modes.get(user_id)
        if current_mode:
            del user_modes[user_id]
            if current_mode == 'translate':
                await update.message.reply_text("✅ Режим переводчика выключен.", reply_to_message_id=update.message.message_id)
            elif current_mode == 'image_gen':
                await update.message.reply_text("✅ Режим генерации изображений выключен.", reply_to_message_id=update.message.message_id)
            return

    # Включение режима переводчика (одноразовый)
    if lower_text in ['пр', 'перевод', 'translate']:
        user_modes[user_id] = 'translate'
        await update.message.reply_text(
            "🗣 Отправьте текст для перевода на русский:",
            reply_to_message_id=update.message.message_id
        )
        return

    # Переключение моделей (Про / Флэш)
    if lower_text in ['п', 'про', 'pro', '1']:
        user_models[user_id] = 'pro'
        reset_session(user_id)
        await update.message.reply_text("*Pro* 💎", parse_mode='Markdown', reply_to_message_id=update.message.message_id)
        return

    if lower_text == 'ф':
        user_models[user_id] = 'flash'
        reset_session(user_id)
        await update.message.reply_text("*Flash* ⚡", parse_mode='Markdown', reply_to_message_id=update.message.message_id)
        return

    if stripped == '.':
        reset_session(user_id)
        await update.message.reply_text("🔄 Контекст сброшен.", reply_to_message_id=update.message.message_id)
        return

    # КОМАНДА "К" или "КАРТИНКА" - генерация изображений
    # Включение режима ожидания промта (без текста после команды)
    if lower_text in ['к', 'картинка']:
        user_modes[user_id] = 'image_gen'
        model_key = user_image_models.get(user_id, 'pro')
        model_icon = "💎" if model_key == 'pro' else "⚡"
        await update.message.reply_text(
            f"🎨 {model_icon} Опишите что нарисовать:",
            reply_to_message_id=update.message.message_id
        )
        return
    
    # КОМАНДА "С" или "СМОТРИ" - режим ожидания фото для анализа
    if lower_text in ['с', 'смотри']:
        user_modes[user_id] = 'image_analyze'
        await update.message.reply_text(
            "🔍 Отправьте фото для анализа:",
            reply_to_message_id=update.message.message_id
        )
        return
    
    # С промтом сразу после команды
    if lower_text.startswith('к ') or lower_text.startswith('картинка '):
        # Извлекаем промт
        if lower_text.startswith('картинка '):
            prompt = stripped[9:].strip()  # после "картинка "
        else:
            prompt = stripped[2:].strip()  # после "к "
        
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
        model_key = user_image_models.get(user_id, 'pro')
        model_icon = "💎" if model_key == 'pro' else "⚡"
        thinking_msg = await update.message.reply_text(
            f"🎨 {model_icon} Генерирую изображение...",
            reply_to_message_id=update.message.message_id
        )
        
        try:
            # Генерируем изображение через текущую модель
            result_data, used_model = await generate_image(prompt, user_id)
            await thinking_msg.delete()
            
            await update.message.reply_photo(
                photo=result_data,
                caption=f"{model_icon} *{used_model.upper()}*",
                parse_mode='Markdown',
                reply_to_message_id=update.message.message_id
            )
            
            # Логируем активность
            log_activity(user_id, update.effective_user.username, "img_gen", prompt[:30])
            
        except Exception as e:
            try: await thinking_msg.delete()
            except: pass
            log_error("IMAGE_GEN", str(e), user_id)
            await update.message.reply_text(
                f"⚠️ Ошибка генерации:\n`{str(e)[:200]}`",
                parse_mode='Markdown',
                reply_to_message_id=update.message.message_id
            )
        return

    # КОМАНДА "СМОТРИ" или "С" - анализ изображения (reply на фото)
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        if lower_text.startswith('смотри') or lower_text.startswith('с ') or lower_text == 'с':
            # Извлекаем промт
            if lower_text.startswith('смотри'):
                prompt = text.strip()[6:].strip()  # после "смотри"
            else:
                prompt = text.strip()[1:].strip()  # после "с"
            
            if not prompt:
                prompt = "Опиши подробно что на этом изображении"
            
            thinking_msg = await update.message.reply_text("🔍 Анализирую изображение...", reply_to_message_id=update.message.message_id)
            
            try:
                # Получаем фото из replied сообщения
                photo = update.message.reply_to_message.photo[-1]
                photo_file = await photo.get_file()
                photo_bytes = await photo_file.download_as_bytearray()
                
                # Используем Flash-модель для анализа
                flash_model = get_model_instance('flash')
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        flash_model.generate_content,
                        [{"mime_type": "image/jpeg", "data": bytes(photo_bytes)}, prompt]
                    ),
                    timeout=60.0
                )
                
                await thinking_msg.delete()
                
                # Проверка на пустой ответ
                response_text = response.text if response and response.text else "⚠️ Не удалось проанализировать изображение"
                await send_safe_message(update, response_text)
                bot_stats['messages_count'] += 1
                return
                
            except asyncio.TimeoutError:
                try: await thinking_msg.delete()
                except: pass
                log_error("IMAGE_ANALYZE_TIMEOUT", "Таймаут", user_id)
                await update.message.reply_text("⚠️ Превышено время анализа.", reply_to_message_id=update.message.message_id)
                return
                
            except Exception as e:
                try: await thinking_msg.delete()
                except: pass
                log_error("IMAGE_ANALYZE", str(e), user_id)
                await update.message.reply_text(
                    f"⚠️ Ошибка анализа:\n`{str(e)[:150]}`",
                    parse_mode='Markdown',
                    reply_to_message_id=update.message.message_id
                )
                return

    # Команда "К" — генерация изображения в фоне (одношаговая)
    if lower_text.startswith('к '):
        # Извлекаем промт после "К "
        prompt = text[2:].strip()
        if not prompt:
            await update.message.reply_text(
                "⚠️ Укажите описание после *К*\n\nПример: `К красивый закат на море`",
                parse_mode='Markdown'
            )
            return
        
        model_key = user_image_models.get(user_id, 'pro')
        model_icon = "💎" if model_key == 'pro' else "⚡"
        
        # Уведомляем о начале генерации
        status_msg = await update.message.reply_text(
            f"🎨 Начинаю генерацию...\n{model_icon} Модель: *{model_key.upper()}*\n\n⏳ Вы можете продолжать диалог!",
            parse_mode='Markdown',
            reply_to_message_id=update.message.message_id
        )
        
        # Запускаем генерацию в фоне
        async def background_image_generation():
            try:
                await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
                image_data, used_model_key = await generate_image(prompt, user_id)
                
                used_icon = "💎" if used_model_key == 'pro' else "⚡"
                await update.message.reply_photo(
                    photo=image_data,
                    caption=f"{used_icon} Сгенерировано: *{prompt[:50]}{'...' if len(prompt) > 50 else ''}*",
                    parse_mode='Markdown',
                    reply_to_message_id=update.message.message_id
                )
                
                # Логируем активность
                log_activity(user_id, update.effective_user.username, "img_gen", prompt[:30])
                await status_msg.delete()
                
            except Exception as e:
                log_error("IMAGE_GEN", str(e), user_id)
                try:
                    await status_msg.edit_text(
                        f"⚠️ Ошибка генерации:\n`{str(e)[:150]}`",
                        parse_mode='Markdown'
                    )
                except:
                    await update.message.reply_text(
                        f"⚠️ Ошибка генерации:\n`{str(e)[:150]}`",
                        parse_mode='Markdown'
                    )
        
        # Запускаем как фоновую задачу
        asyncio.create_task(background_image_generation())
        return

    # Подсчет сообщений
    bot_stats['messages_count'] += 1
    
    # Получаем или создаём сессию
    chat = get_or_create_session(user_id)
    
    # Если включен режим генерации изображений
    if user_modes.get(user_id) == 'image_gen':
        prompt = text.strip()
        # Выключаем режим сразу (одноразовый)
        del user_modes[user_id]
        
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
        model_key = user_image_models.get(user_id, 'pro')
        model_icon = "💎" if model_key == 'pro' else "⚡"
        thinking_msg = await update.message.reply_text(
            f"🎨 {model_icon} Генерирую изображение...",
            reply_to_message_id=update.message.message_id
        )
        
        try:
            result_data, used_model = await generate_image(prompt, user_id)
            await thinking_msg.delete()
            
            await update.message.reply_photo(
                photo=result_data,
                caption=f"{model_icon} *{used_model.upper()}*",
                parse_mode='Markdown',
                reply_to_message_id=update.message.message_id
            )
            
            # Логируем активность
            log_activity(user_id, update.effective_user.username, "img_gen", prompt[:30])
            
        except Exception as e:
            try: await thinking_msg.delete()
            except: pass
            log_error("IMAGE_GEN", str(e), user_id)
            await update.message.reply_text(
                f"⚠️ Ошибка генерации:\n`{str(e)[:200]}`",
                parse_mode='Markdown',
                reply_to_message_id=update.message.message_id
            )
        return
    
    # Если включен режим переводчика
    if user_modes.get(user_id) == 'translate':
        prompt_text = f"Переведи этот текст на русский язык максимально точно и литературно, сохраняя стиль оригинала. Не добавляй никаких комментариев, только перевод:\n\n{text}"
        # Для перевода лучше использовать Flash (быстрее)
        model_instance = get_model_instance('flash')
        
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(model_instance.generate_content, prompt_text),
                timeout=60.0
            )
            response_text = response.text if response and response.text else "⚠️ Не удалось перевести"
            await send_safe_message(update, response_text)
            # Одноразовый перевод — выключаем режим после использования
            del user_modes[user_id]
            return
        except Exception as e:
            log_error("TRANSLATE", str(e), user_id)
            await update.message.reply_text(f"⚠️ Ошибка перевода: {str(e)[:100]}")
            return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    thinking_msg = await update.message.reply_text("🤔 Думаю...", reply_to_message_id=update.message.message_id)
    
    try:
        clean_text = text.replace(f'@{bot_username}', '').strip() if bot_username else text
        response = await send_with_retry(chat, clean_text)
        await thinking_msg.delete()
        
        # Проверка на пустой ответ
        response_text = response.text if response and response.text else "⚠️ Пустой ответ от API"
        await send_safe_message(update, response_text)
        
        # Логируем активность
        model_key = get_model_key(user_id)
        log_activity(user_id, update.effective_user.username, "text", f"Model: {model_key}")        
    except Exception as e:
        try: await thinking_msg.delete()
        except: pass
        
        error_msg = str(e)
        log_error("API", error_msg, user_id)
        
        if "429" in error_msg: 
            error_text = "⚠️ *Лимит запросов.* Подождите минуту."
        elif "401" in error_msg: 
            error_text = "⚠️ *Ошибка авторизации API*"
        elif "503" in error_msg or "500" in error_msg: 
            error_text = "⚠️ *Google временно недоступен.* Попробуйте позже."
        elif "timeout" in error_msg.lower():
            error_text = "⚠️ *Таймаут.* Попробуйте ещё раз."
        else:
            error_text = f"⚠️ *Ошибка*\n`{error_msg[:100]}`"
        
        await send_safe_message(update, error_text)
        
        if chat_type == ChatType.PRIVATE and user_id != ADMIN_ID:
            try: 
                await context.bot.send_message(
                    chat_id=ADMIN_ID, 
                    text=f"🚨 API Error\nUser: {user_id}\n`{error_msg[:200]}`", 
                    parse_mode='Markdown'
                )
            except: pass

# --- ЗАПУСК ---
if __name__ == '__main__':
    load_activity_log()
    logger.info(f"Загружено {len(user_activity)} записей за сегодня")
    load_users()
    logger.info(f"Загружено {len(allowed_users)} пользователей")
    
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Команды
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('status', status_command))
    application.add_handler(CommandHandler('add', add_user))
    application.add_handler(CommandHandler('del', del_user))
    application.add_handler(CommandHandler('id', my_id))
    application.add_handler(CommandHandler('1model', set_pro_model))
    application.add_handler(CommandHandler('2model', set_flash_model))
    application.add_handler(CommandHandler('models', all_models))
    application.add_handler(CommandHandler('help', help_command))
    # Команды для изображений
    application.add_handler(CommandHandler('imagepro', set_image_pro))
    application.add_handler(CommandHandler('imageflash', set_image_flash))
    
    # Обработчики сообщений
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))  # Обработчик фото
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    async def post_init(app):
        await app.bot.set_my_commands([
            ("start", "🔄 Сбросить контекст"),
            ("help", "❓ Справка"),
            ("status", "📊 Статус бота"),
            ("imagepro", "🖼️ 💎 Pro"),
            ("imageflash", "🖼️⚡ Flash"),
            ("1model", "💎 Gemini Pro"),
            ("2model", "⚡ Gemini Flash"),
            ("models", "📋 Список моделей"),
        ])
        logger.info("Меню команд установлено")
    
    application.post_init = post_init
    
    print("=" * 40)
    print("🤖 Бот (v6.0 + Image Gen) запущен")
    print(f"📊 Pro: {MODELS['pro']}")
    print(f"⚡ Flash: {MODELS['flash']}")
    print(f"🖼️ 💎 Pro: {IMAGE_MODELS['pro']}")
    print(f"🖼️⚡ Flash: {IMAGE_MODELS['flash']}")
    print("=" * 40)
    
    application.run_polling(drop_pending_updates=True)