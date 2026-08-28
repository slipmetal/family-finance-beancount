#!/usr/bin/env python3
"""Regression-тесты импортёров на эталонных файлах.

    python tests/golden.py list                    # какие эталоны есть
    python tests/golden.py ameria-card test        # сверить с эталоном
    python tests/golden.py ameria-card generate    # перегенерировать эталон

Эталон — это `<фикстура>.beancount` рядом с входным файлом. Он проверяет не
только разбор строк, но и `account()`, `date()` и `filename()`.

Папку с фикстурами указывать не нужно: у каждого эталона она своя и записана
здесь. Раньше её передавали руками, и ошибиться было нечем — beangulp считает
ошибкой файл, который проверяемый импортёр не опознаёт, так что папки у карт
и счетов одного банка всё равно разные. Передать её всё-таки можно, следующим
аргументом: `generate --force` и прочие ключи beangulp работают как работали.

Правила берутся тестовые (tests/rules.yaml), а не боевые: боевые лежат рядом
с леджером, в приватном репозитории. Правка тестовых правил меняет эталон — и
это хорошо: в диффе видно, что именно новое правило сделало с проводками.
Поменяли tests/rules.yaml → `generate`, глазами просмотрели дифф, закоммитили.

Файл называется не `*_test.py`: под таким именем его собрал бы pytest, а он
не набор тестов, а скрипт. Импорт его модуля перезапускает интерпретатор
ради UTF-8 — под сборщиком тестов это кончилось бы плохо. Сами тесты зовут
его подпроцессом, см. tests/conftest.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from beangulp.testing import main

ROOT = Path(__file__).resolve().parents[1]
# Скрипт запускают файлом, поэтому в sys.path попадает tests/, а не корень.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finance.categorize import Rules  # noqa: E402
from finance.cli import ensure_utf8_mode  # noqa: E402
from finance.importers import acba, ameria, sber, tbank  # noqa: E402
from tests import fixtures  # noqa: E402

ensure_utf8_mode()


@dataclass(frozen=True)
class Golden:
    """Импортёр вместе с папкой, эталон в которой он держит."""

    directory: Path
    build: Callable[[Rules], Any]


#: Имя эталона → чем и по чему его проверять. Имена те же, что у банков
#: в accounts.yaml, только карты и счета названы врозь: у них разные форматы
#: выписки, а значит разные импортёры и разные папки с фикстурами.
GOLDEN: dict[str, Golden] = {
    "ameria-card": Golden(
        fixtures.AMERIA_CARD_DIR,
        lambda rules: ameria.CardImporter(
            fixtures.AMERIA_ACCOUNT, "AMD", rules, marker=fixtures.AMERIA_MARKER
        ),
    ),
    "ameria-account": Golden(
        fixtures.AMERIA_ACCOUNT_DIR,
        lambda rules: ameria.AccountImporter(
            fixtures.AMERIA_SAVINGS_ACCOUNT,
            "USD",
            rules,
            marker=fixtures.AMERIA_SAVINGS_MARKER,
            number=fixtures.AMERIA_SAVINGS_NUMBER,
        ),
    ),
    "acba-card": Golden(
        fixtures.ACBA_CARD_DIR,
        lambda rules: acba.CardImporter(
            "Assets:Acba:AmdCard", "AMD", fixtures.ACBA_CARD_NUMBER, rules
        ),
    ),
    "acba-account": Golden(
        fixtures.ACBA_ACCOUNT_DIR,
        lambda rules: acba.AccountImporter(
            "Assets:Acba:Amd", "AMD", fixtures.ACBA_ACCOUNT_NUMBER, rules
        ),
    ),
    "tbank": Golden(
        fixtures.TBANK_DIR,
        lambda rules: tbank.Importer(
            fixtures.TBANK_ACCOUNT, "RUB", fixtures.TBANK_NUMBER, rules
        ),
    ),
    "sber": Golden(
        fixtures.SBER_DIR,
        lambda rules: sber.Importer(
            fixtures.SBER_ACCOUNT, "RUB", fixtures.SBER_NUMBER, rules
        ),
    ),
}


def usage(problem: str = "") -> int:
    if problem:
        print(problem, file=sys.stderr)
    print("Эталоны: " + ", ".join(GOLDEN))
    print("Запуск:  python tests/golden.py <эталон> test|generate [ключи]")
    return 2 if problem else 0


def run(argv: list[str]) -> int:
    """Разобрать своё первое слово и отдать остальное click-команде beangulp."""
    if not argv or argv[0] in {"list", "-h", "--help"}:
        return usage()

    name, rest = argv[0], argv[1:]
    if name not in GOLDEN:
        return usage(f"Нет такого эталона: {name}")
    if not rest:
        return usage(f"{name}: не сказано, что делать — test или generate")

    golden = GOLDEN[name]
    # Папка по умолчанию своя у каждого эталона. Явно переданная — побеждает.
    if not [argument for argument in rest[1:] if not argument.startswith("-")]:
        rest = [rest[0], str(golden.directory), *rest[1:]]

    # beangulp.testing.main — click-команда, аргументы она берёт из sys.argv.
    sys.argv = [f"{Path(sys.argv[0]).name} {name}", *rest]
    main(golden.build(Rules.load(fixtures.RULES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))