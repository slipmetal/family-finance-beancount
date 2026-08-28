"""Всё, что бот говорит.

Отдельным модулем по той же причине, по какой у CLI отдельный класс Console:
формулировки правятся чаще логики, и держать их вперемешку с ней неудобно.
Проверять их так тоже проще — тест сравнивает с этими же константами, а не
с копией фразы, набранной второй раз.

Разметка — HTML: имена файлов и получателей платежей приходят извне, и всё,
что подставляется, экранируется. Markdown для этого не годится — в нём
подчёркивание в имени файла ломает разбор.
"""

from __future__ import annotations

from html import escape

from finance.inbox import Row
from finance.pipeline import Extraction

#: Сколько проводок без категории показывать списком. Столько же показывает
#: и терминал: дальше список перестаёт помещаться в экран, а решение
#: принимается и по первым.
SHOWN = 15

START = (
    "Присылайте выписки файлом — я разложу их по счетам.\n\n"
    "/inbox — что уже лежит и ждёт разбора\n"
    "/import — разобрать и перенести в леджер\n"
    "/help — то же самое ещё раз"
)

#: Фото приходят пережатыми, и импортёру от них никакого толку.
NOT_A_FILE = (
    "Пришлите выписку файлом, а не фотографией: Telegram пережимает "
    "картинки, а разобрать нужно исходник."
)

WRONG_SUFFIX = "Не похоже на выписку: понимаю csv, xls, xlsx и pdf."

TOO_BIG = "Файл великоват — принимаю до {limit} МБ."

EMPTY_INBOX = "В inbox пусто. Присылайте выписки — разберу."

BUSY = "Уже разбираю, подождите немного."

NOTHING_NEW = (
    "Новых проводок нет — переносить нечего.\n"
    "Выписки остались в inbox, архивировать их нечем."
)

STALE = "Этого файла в inbox больше нет — посмотрите /inbox."

CANCELLED = "Не переношу. Разобранное никуда не делось, /import повторит."

SKIPPED = "Хорошо, оставляю как есть."

CONFUSED = "Не понял. Пришлите выписку файлом или наберите /import."


def statement(result: str) -> str:
    """Итог раскладки одного файла."""
    return escape(result)


def listing(rows: list[Row]) -> str:
    """Что лежит в inbox — строкой на файл."""
    lines = []
    for row in rows:
        name = escape(row.name)
        if row.settled:
            lines.append(f"✅ <b>{name}</b> → {escape(row.account)}")
        elif row.choices:
            lines.append(f"❓ <b>{name}</b> — чей это счёт?")
        else:
            lines.append(f"⚠️ <b>{name}</b> — {escape(row.problem)}")
    return "\n".join(lines)


def report(extraction: Extraction) -> str:
    """Что получилось при разборе — тот же отчёт, что печатает терминал."""
    lines = [
        f"Разобрано проводок: <b>{extraction.transactions}</b>, "
        f"сочтено дубликатами: {extraction.duplicates}"
    ]
    if extraction.uncategorized:
        lines.append(f"\nБез категории: {len(extraction.uncategorized)}")
        shown = extraction.uncategorized[:SHOWN]
        rows = "\n".join(escape(Extraction.line(txn)) for txn in shown)
        lines.append(f"<pre>{rows}</pre>")
        if len(extraction.uncategorized) > SHOWN:
            lines.append(f"… и ещё {len(extraction.uncategorized) - SHOWN}")
        lines.append(
            "Их можно перенести как есть и разметить потом, а можно "
            "дописать правило в rules.yaml и запустить снова."
        )
    return "\n".join(lines)


#: Сколько символов вывода сбойного шага показывать. В сообщение Telegram
#: влезает 4096, а вывод из двадцати ошибок bean-check длиннее.
OUTPUT_LIMIT = 1000


def failure(name: str, output: str) -> str:
    """Сбойный шаг: что не получилось и что об этом сказали."""
    said = output[:OUTPUT_LIMIT]
    if len(output) > OUTPUT_LIMIT:
        said += "\n…"
    tail = f"\n<pre>{escape(said)}</pre>" if said else ""
    return f"❌ {escape(name)}: не получилось{tail}"


def progress(done: list[str], failed: str = "") -> str:
    """Ход цепочки: что уже прошло и на чём споткнулись."""
    lines = [f"✅ {escape(name)}" for name in done]
    if failed:
        lines.append(f"❌ {escape(failed)}")
    return "\n".join(lines)
