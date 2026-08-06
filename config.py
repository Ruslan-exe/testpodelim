"""
Конфигурация проекта. Все секреты берутся из переменных окружения (.env).
Ничего не хардкодим — это единственное место, куда смотрят остальные модули.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Токен Telegram-бота, выданный @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Публичный HTTPS-адрес, где будет открываться Mini App (frontend/index.html).
# Telegram НЕ откроет Mini App по http:// или по localhost — нужен реальный HTTPS-домен
# (см. README: варианты — Railway/Render/VPS + Caddy, либо ngrok для теста).
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://example.com")

# Ключ провайдера vision-модели для распознавания чеков.
# Клиент — OpenAI-совместимый, но провайдер можно подставить любой, у кого
# есть OpenAI-совместимый endpoint. По умолчанию рекомендуем БЕСПЛАТНЫЙ
# Google Gemini (см. README "Бесплатное распознавание чеков" — там же риск
# по доступности из Узбекистана и запасные варианты, если этот не подойдёт):
#   OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
#   VISION_MODEL=gemini-2.5-flash
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL") or None  # None -> официальный endpoint OpenAI
VISION_MODEL = os.getenv("VISION_MODEL", "gemini-2.5-flash")

# Путь к SQLite базе (для MVP этого достаточно, миграция на Postgres — позже)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./podelim.db")

# Валюта по умолчанию, если модель не смогла её распознать
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "UZS")

# --- Telegram webhook (для бесплатного хостинга без второго процесса) ---
# Бесплатные хостинги (Render free tier и подобные) дают только ОДИН процесс.
# Обычный long-polling бот (отдельный процесс python bot.py) там не проживёт
# постоянно. Поэтому на проде бот принимает сообщения через вебхук —
# то есть Telegram сам стучится в наш веб-сервис, второй процесс не нужен.
# Локально для разработки удобнее polling (см. bot.py) — переключается этим флагом.
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() == "true"
# Публичный адрес BACKEND (не путать с MINI_APP_URL — это адрес API, не фронтенда)
PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "")
# Случайная строка в пути вебхука, чтобы посторонний не мог слать боту "чужие" апдейты.
# Сгенерировать: python3 -c "import secrets; print(secrets.token_urlsafe(24))"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
