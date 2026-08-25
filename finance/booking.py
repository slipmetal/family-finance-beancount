"""Сборка проводки по правилам категоризации — общая для всех банков.

Импортёр разбирает свой формат и отдаёт сюда заготовку с одной ногой (счёт
банка) плюс четыре текстовых поля. Здесь решается, что станет payee, что
описанием, какой счёт пойдёт второй ногой и какой у проводки будет флаг.
"""

from __future__ import annotations

from decimal import Decimal

from beancount.core import data, flags

from finance.categorize import Rules


def categorize(
    txn: data.Transaction,
    rules: Rules,
    *,
    counterparty: str,
    details: str,
    txn_type: str,
    amount: Decimal,
    correspondent: str = "",
    narration: str | None = None,
    ok_flag: str = flags.FLAG_OKAY,
) -> data.Transaction:
    """Достроить проводку: вторая нога, payee, описание, флаг.

    Вторая нога добавляется без суммы — beancount выведет её сам, поэтому
    арифметика проводки не может разойтись.

    Флаг `!` ставится, когда не подошло ни одно правило: такие строки видно
    в fava, и по ним понятно, что дописать в rules.yaml.

    Всё, что вытеснено подстановкой из правила, уезжает в метаданные —
    из выписки не теряется ничего.

    Правилу доступен и сам счёт — через `match.account`. Он берётся из первой
    ноги заготовки, поэтому порядок ног у импортёра значим: счёт банка идёт
    первым. Нужно это для случаев, когда одно и то же описание значит разное
    на разных счетах одного банка.
    """
    match = rules.apply(
        counterparty=counterparty,
        details=details,
        txn_type=txn_type,
        amount=amount,
        correspondent=correspondent,
        # Счёт берём из первой ноги заготовки: по контракту этой функции там
        # стоит счёт банка, чью выписку разбирают. Отдельным аргументом его не
        # передаём намеренно — иначе каждый импортёр дублировал бы то, что уже
        # положил в проводку, и они могли бы разъехаться.
        account=txn.postings[0].account if txn.postings else "",
    )

    meta = dict(txn.meta)

    payee = match.payee or counterparty or None
    if counterparty and payee != counterparty:
        meta["counterparty"] = counterparty

    text = match.narration or narration or details
    if details and text != details:
        meta["details"] = details

    return txn._replace(
        meta=meta,
        flag=ok_flag if match.matched else flags.FLAG_WARNING,
        payee=payee,
        narration=text,
        tags=frozenset(txn.tags) | match.tags,
        postings=[*txn.postings, data.Posting(match.account, None, None, None, None, None)],
    )
