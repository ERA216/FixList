"""Настройки из переменных окружения (.env). Ключи в коде не хранятся."""
import os
import hashlib
import math
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "smena.db")))

# gemini | openai | anthropic | mock. Без ключа автоматически включается mock (резервный режим).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
GEMINI_API_KEY_2 = os.getenv("GEMINI_API_KEY_2", "")  # запасной ключ: используется при лимите или отказе первого
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

DEFAULT_MODELS = {
    "gemini": "gemini-3.5-flash-lite",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}
LLM_MODEL = os.getenv("LLM_MODEL", "").strip() or DEFAULT_MODELS.get(LLM_PROVIDER, "")
# если модель недоступна (404), пробуем следующую
GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-flash-latest"]


def _number_setting(name, default, minimum, maximum, *, integer=False):
    try:
        value = float(os.getenv(name, str(default)))
        if not math.isfinite(value) or not minimum <= value <= maximum or integer and not value.is_integer():
            return default
        return int(value) if integer else value
    except ValueError:
        return default


LLM_TIMEOUT = _number_setting("LLM_TIMEOUT", 12.0, 1, 60)
LLM_TOTAL_BUDGET = _number_setting("LLM_TOTAL_BUDGET", 30.0, 1, 120)
LLM_MAX_RETRIES = _number_setting("LLM_MAX_RETRIES", 2, 0, 3, integer=True)

ANALYSIS_WINDOW_DAYS = int(os.getenv("ANALYSIS_WINDOW_DAYS", "30"))
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.7"))

# Отправитель уведомлений. Адрес получателя задаётся отдельно для каждой компании.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
try:
    SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
except ValueError:
    SMTP_PORT = 0  # Ошибка почтовой настройки не должна мешать работе самого приложения.
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").replace(" ", "")
SMTP_TIMEOUT = 10


def api_keys() -> list[str]:
    raw = {"gemini": [GEMINI_API_KEY, GEMINI_API_KEY_2], "openai": [OPENAI_API_KEY], "anthropic": [ANTHROPIC_API_KEY]}.get(LLM_PROVIDER, [])
    keys = [k.strip() for item in raw for k in item.split(",") if k.strip()]
    return list(dict.fromkeys(keys))


def api_key() -> str:
    keys = api_keys()
    return keys[0] if keys else ""


def ai_enabled() -> bool:
    return LLM_PROVIDER in DEFAULT_MODELS and bool(api_key())


def ai_settings_signature() -> tuple:
    """Invalidate server-side previews when the model or credentials change, without exposing keys."""
    digest = hashlib.sha256("\0".join(api_keys()).encode()).hexdigest()
    return LLM_PROVIDER, LLM_MODEL, digest


def mode_label() -> str:
    if ai_enabled():
        n = len(api_keys())
        return f"ИИ: {LLM_PROVIDER} · {LLM_MODEL}" + (f" · ключей: {n}" if n > 1 else "")
    return "Резервный режим: разбор по правилам, без ИИ (нет API-ключа)"
