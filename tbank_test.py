#!/usr/bin/env python3
"""Regression-тест импортёра выписок Т-Банка (.pdf).

    python tbank_test.py test tests/tbank
    python tbank_test.py generate tests/tbank

Фикстура рисуется с нуля: tools/make_tbank_fixture.py. Настоящая выписка
в ней не участвует, поэтому анонимизировать нечего — см. докстринг генератора.

Правила берутся тестовые (tests/rules.yaml), а не боевые: боевые лежат рядом
с леджером, в приватном репозитории, и правка правил не должна ломать эталон.
"""

from beangulp.testing import main

from finance.categorize import Rules
from finance.cli import ensure_utf8_mode
from finance.importers.tbank import Importer
from tests.fixtures import RULES, TBANK_ACCOUNT, TBANK_NUMBER

ensure_utf8_mode()

if __name__ == "__main__":
    main(Importer(TBANK_ACCOUNT, "RUB", TBANK_NUMBER, Rules.load(RULES)))