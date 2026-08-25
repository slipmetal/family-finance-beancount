#!/usr/bin/env python3
"""Regression-тест импортёра выписок Сбербанка (.pdf).

    python sber_test.py test tests/sber
    python sber_test.py generate tests/sber

Фикстура рисуется с нуля: tools/make_sber_fixture.py. Настоящая выписка
в ней не участвует, поэтому анонимизировать нечего — см. докстринг генератора.

Правила берутся тестовые (tests/rules.yaml), а не боевые: боевые лежат рядом
с леджером, в приватном репозитории, и правка правил не должна ломать эталон.
"""

from beangulp.testing import main

from finance.categorize import Rules
from finance.cli import ensure_utf8_mode
from finance.importers.sber import Importer
from tests.fixtures import RULES, SBER_ACCOUNT, SBER_NUMBER

ensure_utf8_mode()

if __name__ == "__main__":
    main(Importer(SBER_ACCOUNT, "RUB", SBER_NUMBER, Rules.load(RULES)))