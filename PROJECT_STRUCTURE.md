# 📁 Структура Проекта

> **Быстрый справочник** по архитектуре `googlebot.py`

---

## 🎛 Используемые модели

| Функция | Pro модель | Flash модель | Если без выбора Pro/Flash |
|:---|:---|:---|:---|
| 💬 Текстовый чат | `gemini-3-flash-preview` временно вместо Pro | `gemini-2.5-flash` | — |
| 🌐 Чат с интернетом / tools | `gemini-3-flash-preview` временно вместо Pro | `gemini-2.5-flash` | Google Search + URL context работают |
| 🎤 Голос | — | — | `gemini-2.5-flash` |
| 🎨 Генерация картинок | `gemini-3.1-flash-image-preview` | `gemini-3.1-flash-image-preview` | проверять по ключу |
| ✏️ Редактирование картинок | `gemini-3.1-flash-image-preview` | `gemini-3.1-flash-image-preview` | проверять по ключу |
| 🌐 Перевод текста | — | — | `gemini-2.5-flash-lite` |
| 🌐 Перевод фото | — | — | `gemini-3.1-flash-image-preview` |
| 📺 YouTube саммари | — | — | `gemini-2.5-flash` |
| 🔍 Инлайн | — | — | `gemini-2.5-flash` |
| 📄 Документы | `gemini-3-flash-preview` временно вместо Pro | `gemini-2.5-flash` | — |

> Примечание: по тесту на текущем API-ключе `gemini-2.5-flash` работает и без tools, и с Google Search + URL context. `gemini-3-flash-preview` (используемая как Pro) работает без tools, но с tools получает `429 RESOURCE_EXHAUSTED`.

---

## ⚡ Быстрые команды

| Префикс / команда | Действие | Основная функция | Пример |
|:---|:---|:---|:---|
| `/start` | 🔄 Сброс контекста | `start()` | `/start` |
| `/status` | 📊 Статус бота | `status_command()` | `/status` |
| `/help` | ❓ Справка | `help_command()` | `/help` |
| `/1model` | 💎 Text Pro | `set_pro_model()` | `/1model` |
| `/2model` | ⚡ Text Flash | `set_flash_model()` | `/2model` |
| `/imagepro` | 🎨 Image Pro | `set_image_pro()` | `/imagepro` |
| `/imageflash` | 🎨 Image Flash | `set_image_flash()` | `/imageflash` |
| `к` / `картинка` | 🎨 Генерация изображения | `_process_fast_commands()` | `к кот в космосе` |
| `р` / `редактировать` | ✏️ Редактирование фото | `handle_photo()` | `р сделай ярче` |
| `пр` / `перевод` | 🌐 Перевод текста/фото | `_process_translation_mode()` / `handle_photo()` | `пр hello` |
| `ю` / `ютуб` | 📺 YouTube саммари | `_process_youtube_mode()` | `ю https://youtu.be/...` |
| `превью` / `пре` | 🖼️ YouTube превью | `get_youtube_preview()` | `превью https://youtu.be/...` |
| `.` | 🧹 Сброс режима | `_process_exit_commands()` | `.` |
| `выход` / `exit` | ⏹️ Выход из режима | `_process_exit_commands()` | `выход` |

---

## 📱 Меню команд Telegram

```text
/start - 🔄 Сбросить контекст
/status - 📊 Статус бота
/youtube - 📺 YouTube Саммари
/imagepro - 🎨💎Image Pro
/imageflash - 🎨⚡Image Flash
/1model - 💎Text Gemini Pro
/2model - ⚡Text Gemini Flash
/help - ❓ Справка
```
---

## 🧩 Архитектура `googlebot.py` (всего ~5027 строк)

| Строки | Раздел | Что внутри |
|:---:|:---|:---|
| `1-206` | Импорты и конфигурация | env, лимиты, TTL, системные промпты |
| `207-310` | Логи и модели | `cleanup_log_files()`, `get_latest_models()`, `initialize_models()` |
| `311-544` | RAM / errors / temp / HTTP | RSS, `sanitize_error()`, `delete_safe()`, `httpx.AsyncClient` |
| `545-680` | Activity log | JSONL, daily counters, Киевское время |
| `681-760` | Пользователи и настройки | users, settings, выбор моделей |
| `761-840` | MediaRef и изображения | `MediaRef`, download, resize/compress |
| `841-942` | Cleanup task | TTL, albums, temp files, session limits |
| `943-1250` | Gemini sessions | `reset_session()`, `get_or_create_session()` |
| `1251-1550` | Форматирование и отправка | errors, HTML/LaTeX, `send_safe_message` |
| `1551-1800` | Image API | `generate_image()`, `edit_image()` |
| `1801-2250` | Генерация и YouTube | YouTube preview, transcript, summary |
| `2251-2295` | Команды | `/start`, `/status`, `/help`, управление моделями |
| `2296-3076` | Медиа handlers | voice, photo, albums, documents |
| `3077-3837` | Message helpers | _process_ functions (fast commands, translate, YouTube) |
| `3838-4085` | Text dispatcher | `handle_message()` |
| `4086-4425` | Inline | inline query и chosen result |
| `4426-4908` | Callback-и | кнопки фото, image regen, Twitter/X |
| `4909-4967` | Lifecycle | `post_init()`, `post_shutdown()` |
| `4968+` | Запуск | `main()` |

---

## 📦 Основные функции

| Функция | Строка | Назначение |
|:---|:---:|:---|
| `get_process_rss_mb()` | `352` | RSS процесса |
| `log_memory()` | `361` | лог RAM при `MEMORY_DEBUG=1` |
| `sanitize_error()` | `395` | очистка текста ошибок |
| `safe_delete_file()` | `439` | безопасное удаление файла |
| `get_http_client()` | `499` | общий `httpx.AsyncClient` |
| `log_activity()` | `619` | лог активности |
| `save_activity_log()` | `633` | запись в `activity_log.jsonl` |
| `prepare_image_for_gemini()` | `785` | сжатие изображения перед API |
| `get_or_create_session()` | `984` | Gemini chat session |
| `generate_image()` | `1563` | генерация картинки |
| `edit_image()` | `1591` | редактирование картинки |
| `status_command()` | `1943` | `/status` |
| `handle_voice()` | `2296` | голосовые сообщения |
| `handle_photo()` | `2412` | фото и альбомы |
| `process_album_delayed()` | `2821` | сборка и обработка альбомов |
| `handle_document()` | `2979` | документы |
| `handle_message()` | `3838` | текстовые сообщения (диспетчер) |
| `handle_inline_query()` | `4086` | inline-запросы |
| `button_callback()` | `4426` | callback-кнопки |
| `post_init()` | `4909` | запуск cleanup/http/menu |
| `main()` | `4968` | запуск Application |

---

## 🧠 Память и cleanup

| Компонент | Что делает |
|:---|:---|
| `MediaRef` | Использует `file_id` вместо хранения `bytes` в RAM |
| `pending_albums` | Временное хранилище для сборки медиа-групп |
| `cleanup_loop()` | Фоновая очистка старых сессий, альбомов и файлов |
| `tmp_media/` | Папка для временных файлов |
| `gc_collect_after_media()` | Принудительный сбор мусора после тяжелых задач |

---

## 🔧 Переменные окружения `.env`

| Переменная | Описание |
|:---|:---|
| `TELEGRAM_TOKEN` | токен Telegram бота |
| `GEMINI_API_KEY` | ключ Gemini API |
| `ADMIN_ID` | Telegram ID администратора |
| `MEMORY_DEBUG` | `1` для детальных логов RAM |

---

## 📊 Логи

| Файл | Назначение |
|:---|:---|
| `bot.log` | Технические логи системы |
| `activity_log.jsonl` | Лог действий пользователей (Киевское время) |
