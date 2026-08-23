"""Что общего у тестов и у скриптов эталонных проверок в корне репозитория.

Номера счетов здесь выдуманные — те же заглушки, что и в самих фикстурах.
Настоящие номера лежат в accounts.yaml рядом с леджером, в приватном
репозитории, и в тестах не участвуют.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Правила для тестов. Боевые лежат рядом с леджером и эталоны не ломают.
RULES = ROOT / "tests" / "rules.yaml"

AMERIA_DIR = ROOT / "tests" / "ameria"
AMERIA_ACCOUNT = "Assets:Ameria:Card0001"
AMERIA_MARKER = "card0001"
#: Вторая карта в той же валюте: различить их можно только по метке.
AMERIA_ACCOUNT_OTHER = "Assets:Ameria:Card0002"
AMERIA_MARKER_OTHER = "card0002"

ACBA_CARD_DIR = ROOT / "tests" / "acba" / "card"
ACBA_ACCOUNT_DIR = ROOT / "tests" / "acba" / "account"

#: Свои счета в фикстурах: карточные начинаются на 1000…1, обычные — на 1000…11.
ACBA_CARD_NUMBER = "100000000000001"
ACBA_CARD_NUMBER_USD = "100000000000002"
ACBA_ACCOUNT_NUMBER = "100000000000011"
ACBA_ACCOUNT_NUMBER_USD = "100000000000012"
ACBA_ACCOUNT_NUMBER_RUB = "100000000000013"