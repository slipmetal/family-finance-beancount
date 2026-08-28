"""Парсеры выписок: по одному модулю на банк."""


def importer_name(prefix: str, account: str) -> str:
    """Уникальное имя импортёра: префикс банка плюс путь счёта под банком.

    Имена обязаны быть различными — fava отвергает конфиг импорта с повторами,
    а экземпляров каждого класса столько, сколько счетов в этом банке.

    Берётся ВЕСЬ путь ниже банка, а не последний сегмент. Раньше был последний,
    и это работало ровно до первого вложенного счёта: `Assets:Acba:Usd` и
    `Assets:Acba:SoleProprietor:Usd` оба давали `acba.usd`, и fava падала на
    «Duplicate importer name found». Для плоских счетов результат не изменился:

        Assets:Acba:Usd                 → acba.usd
        Assets:Acba:AmdCard             → acba.amdcard
        Assets:Acba:SoleProprietor:Usd  → acba.soleproprietor.usd
    """
    return f"{prefix}." + ".".join(account.split(":")[2:]).lower()
