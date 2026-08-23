#!/usr/bin/env python3
"""Regression-тест импортёра выписок обычных счетов ACBA (.xml).

    python acba_account_test.py test tests/acba/account
    python acba_account_test.py generate tests/acba/account

Фикстура собирается из настоящей выписки: tools/make_acba_fixtures.py
Правила берутся тестовые (tests/rules.yaml), а не боевые: боевые лежат рядом
с леджером, в приватном репозитории, и правка правил не должна ломать эталон.
"""

from beangulp.testing import main

from finance.categorize import Rules
from finance.cli import ensure_utf8_mode
from finance.importers.acba import AccountImporter
from tests.fixtures import ACBA_ACCOUNT_NUMBER, RULES

ensure_utf8_mode()

if __name__ == "__main__":
    main(AccountImporter("Assets:Acba:Amd", "AMD", ACBA_ACCOUNT_NUMBER, Rules.load(RULES)))