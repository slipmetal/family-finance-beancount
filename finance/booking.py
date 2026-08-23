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
    """
    match = rules.apply(
        counterparty=counterparty,
        details=details,
        txn_type=txn_type,
        amount=amount,
        correspondent=correspondent,
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
