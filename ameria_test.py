#!/usr/bin/env python3
"""Regression-тест импортёра Ameriabank на эталонных файлах.

    python ameria_test.py test tests/ameria/card          # сверить с эталоном
    python ameria_test.py generate tests/ameria/card      # перегенерировать эталон

Эталон — это `tests/ameria/*.csv.beancount` рядом с входным CSV. Он проверяет
не только разбор строк, но и account(), date() и filename().

Правила берутся тестовые (tests/rules.yaml), а не боевые: боевые лежат рядом
с леджером, в приватном репозитории. Правка тестовых правил меняет эталон — и
это хорошо: в диффе видно, что именно новое правило сделало с проводками.
Поменяли tests/rules.yaml → `generate`, глазами просмотрели дифф, закоммитили.
"""

from beangulp.testing import main

from finance.categorize import Rules
from finance.cli import ensure_utf8_mode
from finance.importers.ameria import CardImporter
from tests.fixtures import AMERIA_ACCOUNT, AMERIA_MARKER, RULES

ensure_utf8_mode()

if __name__ == "__main__":
    main(CardImporter(AMERIA_ACCOUNT, "AMD", Rules.load(RULES), marker=AMERIA_MARKER))