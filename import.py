#!/usr/bin/env python3
"""Точка входа для импорта выписок из командной строки.

    python import.py identify inbox                     # что за файлы лежат
    python import.py extract  inbox -e ledger/main.beancount -o out.beancount
    python import.py archive  inbox -o documents

Затем перенести в леджер: python tools/merge_extract.py out.beancount

То же самое доступно из браузера: fava умеет разбирать выписки теми же
импортёрами, см. fava_import_config.py.

Список импортёров живёт в finance/config.py — он общий для CLI и для fava.
"""

import beangulp

from finance.cli import ensure_utf8_mode
from finance.config import build_importers

ensure_utf8_mode()

if __name__ == "__main__":
    beangulp.Ingest(build_importers())()