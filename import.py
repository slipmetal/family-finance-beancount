#!/usr/bin/env python3
"""Точка входа для импорта выписок из командной строки.

    python import.py add      ~/Downloads/statement.csv   # всё разом, с вопросами
    python import.py identify inbox                     # что за файлы лежат
    python import.py extract  inbox -e ledger/main.beancount -o out.beancount
    python import.py archive  inbox -o documents

Затем перенести в леджер: python tools/merge_extract.py out.beancount

Первая команда, `add`, делает всё это сама и спрашивает только про то, что
неоднозначно, — см. finance/add.py. Остальные три пришли из beangulp, и вся
цепочка по-прежнему собирается из них руками: `add` их же и вызывает.

То же самое доступно из браузера: fava умеет разбирать выписки теми же
импортёрами, см. fava_import_config.py.

Список импортёров живёт в finance/config.py — он общий для CLI и для fava.
"""

import beangulp

from finance.add import add
from finance.cli import ensure_utf8_mode
from finance.config import build_importers

ensure_utf8_mode()

if __name__ == "__main__":
    ingest = beangulp.Ingest(build_importers())
    # beangulp собирает свой click-группу в конструкторе и оставляет её на
    # виду — этим и пользуемся, чтобы `add` жила рядом с остальными командами,
    # а не отдельным скриптом.
    ingest.cli.add_command(add)
    ingest()
