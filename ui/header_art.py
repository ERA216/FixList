"""Local, decorative images for each FixList section."""
from base64 import b64encode
from functools import lru_cache
from pathlib import Path


_ILLUSTRATIONS = Path(__file__).with_name("illustrations")
_SECTIONS = {
    "панель задач": "tasks",
    "спросить ии": "assistant",
    "состояние оборудования": "equipment",
    "сводка дня": "daily",
    "повторяющиеся сигналы": "signals",
    "сигналы производства": "signals",
    "деньги и экономия": "savings",
    "план экономии": "savings",
    "история": "history",
    "история и эффект": "history",
    "сотрудники": "team",
    "мои задачи": "assigned",
    "сообщить о проблеме": "report",
    "запись смены": "shift",
    "отчёт об обходе": "rounds",
}


@lru_cache(maxsize=len(set(_SECTIONS.values())))
def _render_art(name):
    try:
        photo = (_ILLUSTRATIONS / f"{name}.webp").read_bytes()
    except FileNotFoundError:
        return (_ILLUSTRATIONS / f"{name}.svg").read_text(encoding="utf-8")

    encoded = b64encode(photo).decode("ascii")
    return f'<img class="art art-photo" src="data:image/webp;base64,{encoded}" width="260" height="195" alt="">'


def get_header_art(kicker):
    section = kicker.rsplit("/", 1)[-1].strip().casefold()
    return _render_art(_SECTIONS.get(section, "tasks"))
