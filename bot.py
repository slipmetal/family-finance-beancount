#!/usr/bin/env python3
"""Точка входа бота в Telegram.

    python bot.py

Отдельным скриптом в корне, а не `python -m finance.bot`, по той же причине,
что и import.py: ensure_utf8_mode() перезапускает интерпретатор по sys.argv,
а у модуля, запущенного через -m, в argv лежит путь к __main__.py. Повторный
запуск положил бы в sys.path папку пакета вместо корня, и `import finance`
перестал бы работать.

Настроек нет — бот берёт их из окружения, см. finance/bot/settings.py. Без
TELEGRAM_TOKEN он не запускается вовсе и говорит об этом: леджер живёт и без
бота, и ронять из-за него ничего нельзя.
"""

from __future__ import annotations

import logging
import sys

from finance.bot import BotApp
from finance.bot.settings import SettingsError
from finance.cli import ensure_utf8_mode

ensure_utf8_mode()


def main() -> int:
    """Поднять бота. Код возврата — как у обычной программы."""
    logging.basicConfig(
        level=logging.INFO,
        format="[bot] %(message)s",
        stream=sys.stdout,
    )
    try:
        app = BotApp.build()
    except SettingsError as error:
        logging.error("%s", error)
        return 1

    if app is None:
        logging.info("TELEGRAM_TOKEN не задан — бот не нужен")
        return 0

    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
