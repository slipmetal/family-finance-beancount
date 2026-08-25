#!/usr/bin/env python3
"""Regression-тест импортёра выписок по счетам Ameriabank (.csv).

    python ameria_account_test.py test tests/ameria/account
    python ameria_account_test.py generate tests/ameria/account

Формат отличается от карточного: раздельные Credit/Debit, номер счёта
контрагента и строка оборотов в конце — отсюда отдельный импортёр и своя
папка с фикстурами. beangulp считает ошибкой файл, который проверяемый
импортёр не опознаёт, поэтому смешивать их в одной папке нельзя.

Правила берутся тестовые (tests/rules.yaml), а не боевые.
"""

from beangulp.testing import main

from finance.categorize import Rules
from finance.cli import ensure_utf8_mode
from finance.importers.ameria import AccountImporter
from tests.fixtures import (
    AMERIA_SAVINGS_ACCOUNT,
    AMERIA_SAVINGS_MARKER,
    AMERIA_SAVINGS_NUMBER,
    RULES,
)

ensure_utf8_mode()

if __name__ == "__main__":
    main(
        AccountImporter(
            AMERIA_SAVINGS_ACCOUNT,
            "USD",
            Rules.load(RULES),
            marker=AMERIA_SAVINGS_MARKER,
            number=AMERIA_SAVINGS_NUMBER,
        )
    )