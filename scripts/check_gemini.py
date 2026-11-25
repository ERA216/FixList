"""Check a configured Gemini connection using synthetic input and an isolated database."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import config, db, llm
from backend.schemas import BaseModel


class ConnectionReply(BaseModel):
    ok: bool


def main():
    if config.LLM_PROVIDER != "gemini" or not config.ai_enabled():
        print("Gemini не настроен. Задайте LLM_PROVIDER=gemini и GEMINI_API_KEY в .env или окружении сервера.")
        return 2
    previous_path = config.DB_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="fixlist-gemini-check-") as folder:
            config.DB_PATH = Path(folder) / "connection.db"
            db.init_db()
            reply = llm.call_json('Верни только JSON: {"ok":true}.', "Проверка подключения FixList.", ConnectionReply, "connection_check")
            if not reply.ok:
                print("Модель ответила, но проверка не пройдена.")
                return 1
            print("Gemini ответил корректно. Подключение работает. Пользовательские данные не использовались.")
            return 0
    except llm.LLMError as error:
        print(f"Проверка не пройдена: {error.user_message}")
        return 1
    finally:
        config.DB_PATH = previous_path


if __name__ == "__main__":
    raise SystemExit(main())
