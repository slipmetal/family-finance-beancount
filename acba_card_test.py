#!/usr/bin/env python3
"""Regression-тест импортёра карточных выписок ACBA (.xls).

    python acba_card_test.py test tests/acba/card
    python acba_card_test.py generate tests/acba/card

Фикстура собирается из настоящей выписки: tools/make_acba_fixtures.py
Правила берутся тестовые (tests/rules.yaml), а не боевые: боевые лежат рядом
с леджером, в приватном репозитории, и правка правил не должна ломать эталон.
"""

from beangulp.testing import main

from finance.categorize import Rules
from finance.cli import ensure_utf8_mode
from finance.importers.acba import CardImporter
from tests.fixtures import ACBA_CARD_NUMBER, RULES

ensure_utf8_mode()

if __name__ == "__main__":
    main(CardImporter("Assets:Acba:AmdCard", "AMD", ACBA_CARD_NUMBER, Rules.load(RULES)))