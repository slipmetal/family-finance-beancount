"""Что общего у тестов и у скриптов эталонных проверок в корне репозитория.

Номера счетов здесь выдуманные — те же заглушки, что и в самих фикстурах.
Настоящие номера лежат в accounts.yaml рядом с леджером, в приватном
репозитории, и в тестах не участвуют.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Правила для тестов. Боевые лежат рядом с леджером и эталоны не ломают.
RULES = ROOT / "tests" / "rules.yaml"

AMERIA_CARD_DIR = ROOT / "tests" / "ameria" / "card"
#: Выписка по счёту приходит в другом формате, поэтому и папка своя:
#: beangulp generate считает ошибкой файл, который проверяемый импортёр
#: не опознаёт.
AMERIA_ACCOUNT_DIR = ROOT / "tests" / "ameria" / "account"
AMERIA_SAVINGS_ACCOUNT = "Assets:Ameria:Usd"
AMERIA_SAVINGS_MARKER = "usd"
#: Номер сберегательного счёта: заглушка на 1000, как у остальных своих.
AMERIA_SAVINGS_NUMBER = "1000053294282901"
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

TBANK_DIR = ROOT / "tests" / "tbank"
TBANK_ACCOUNT = "Assets:Tbank:Rub"
#: Российский лицевой счёт — двадцать цифр, а не пятнадцать, как в Армении.
TBANK_NUMBER = "10000000000000000001"
TBANK_NUMBER_OTHER = "10000000000000000002"

SBER_DIR = ROOT / "tests" / "sber"
SBER_ACCOUNT = "Assets:Sber:Rub"
#: В выписке Сбербанка номер напечатан группами через пробел («10000 000 0
#: 0000 0000003»), а импортёр сравнивает его вот с этим — только цифры.
#: Заглушка на 1000, как и у остальных своих счетов.
SBER_NUMBER = "10000000000000000003"
SBER_NUMBER_OTHER = "10000000000000000004"
