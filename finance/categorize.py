"""Движок правил категоризации.

Читает `rules.yaml` и по полям транзакции подбирает счёт второй ноги проводки.
Не знает ничего про конкретные банки: импортёр приводит свою строку выписки
к четырём полям (контрагент, описание, тип, сумма) и зовёт `Rules.apply`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

#: Текстовые поля транзакции, по которым можно матчить регулярным выражением.
#: `text` — служебное поле: склейка всех остальных. Нужно потому, что банки
#: кладут название мерчанта в разные колонки (Ameriabank — в описание, ACBA —
#: в «место операции»), а правило на мерчанта должно быть одно на оба банка.
TEXT_FIELDS = ("counterparty", "details", "type", "correspondent", "text")

#: Все ключи, допустимые внутри `match`.
MATCH_KEYS = (*TEXT_FIELDS, "sign", "amount_min", "amount_max")

#: Ключи верхнего уровня правила.
RULE_KEYS = ("name", "match", "account", "payee", "narration", "tags")

#: Имя счёта beancount: один из пяти корней плюс хотя бы один компонент,
#: каждый компонент начинается с заглавной буквы или цифры.
ACCOUNT_RE = re.compile(r"^(?:Assets|Liabilities|Equity|Income|Expenses)(?::[A-Z0-9][\w-]*)+$")

#: Тег beancount: без пробелов и без ведущей решётки (её добавит beancount сам).
TAG_RE = re.compile(r"^[\w-]+$")


class RulesError(Exception):
    """Ошибка в rules.yaml. Поднимается при загрузке, а не во время импорта."""


@dataclass(frozen=True)
class Match:
    """Результат подбора правила для одной транзакции."""

    account: str
    payee: str | None
    narration: str | None
    tags: frozenset[str]
    #: Имя сработавшего правила; None — не подошло ни одно, взят default_account.
    rule: str | None

    @property
    def matched(self) -> bool:
        return self.rule is not None


@dataclass(frozen=True)
class Rule:
    """Одно правило из rules.yaml."""

    name: str
    account: str
    patterns: dict[str, re.Pattern[str]]
    sign: str | None
    amount_min: Decimal | None
    amount_max: Decimal | None
    payee: str | None
    narration: str | None
    tags: frozenset[str]

    def matches(self, fields: dict[str, str], amount: Decimal) -> bool:
        """Все условия правила должны выполниться одновременно (AND)."""
        for field, pattern in self.patterns.items():
            if not pattern.search(fields[field]):
                return False
        if self.sign is not None:
            # Ноль не относим ни к плюсу, ни к минусу.
            actual = "+" if amount > 0 else "-" if amount < 0 else "0"
            if actual != self.sign:
                return False
        # Пороги сравниваем по модулю: знак задаётся отдельным ключом `sign`.
        magnitude = abs(amount)
        if self.amount_min is not None and magnitude < self.amount_min:
            return False
        if self.amount_max is not None and magnitude > self.amount_max:
            return False
        return True


class Rules:
    """Упорядоченный набор правил. Побеждает первое подошедшее."""

    def __init__(self, rules: list[Rule], default_account: str) -> None:
        self.rules = rules
        self.default_account = default_account

    @classmethod
    def load(cls, path: str | Path) -> Rules:
        """Прочитать и провалидировать rules.yaml.

        Все ошибки (кривая регулярка, несуществующий ключ, плохое имя счёта)
        всплывают здесь — чтобы не выяснять это на середине импорта.
        """
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise RulesError(f"{path}: не разбирается как YAML: {exc}") from exc
        if raw is None:
            raise RulesError(f"{path}: файл пуст")
        if not isinstance(raw, dict):
            raise RulesError(f"{path}: ожидался словарь на верхнем уровне, получен {type(raw).__name__}")

        unknown = set(raw) - {"default_account", "rules"}
        if unknown:
            raise RulesError(f"{path}: неизвестные ключи верхнего уровня: {', '.join(sorted(unknown))}")

        default_account = raw.get("default_account", "Expenses:Uncategorized")
        _check_account(default_account, f"{path}: default_account")

        raw_rules = raw.get("rules") or []
        if not isinstance(raw_rules, list):
            raise RulesError(f"{path}: `rules` должен быть списком")

        rules, seen = [], set()
        for index, item in enumerate(raw_rules):
            rule = _parse_rule(item, where=f"{path}: правило #{index + 1}")
            if rule.name in seen:
                raise RulesError(f"{path}: правило #{index + 1}: имя {rule.name!r} уже занято")
            seen.add(rule.name)
            rules.append(rule)

        return cls(rules, default_account)

    def apply(
        self,
        *,
        counterparty: str,
        details: str,
        txn_type: str,
        amount: Decimal,
        correspondent: str = "",
    ) -> Match:
        """Подобрать счёт для транзакции. Ни одно правило не подошло — default_account."""
        fields = {
            "counterparty": counterparty,
            "details": details,
            "type": txn_type,
            # Номер счёта контрагента: есть не у всех банков. Намеренно НЕ входит
            # в `text`, чтобы номера не совпадали случайно с суммами в описании.
            "correspondent": correspondent,
        }
        # Переводом строк, чтобы регулярка не склеила конец одного поля с началом другого.
        fields["text"] = "\n".join((counterparty, details, txn_type))
        for rule in self.rules:
            if rule.matches(fields, amount):
                return Match(
                    account=rule.account,
                    payee=rule.payee,
                    narration=rule.narration,
                    tags=rule.tags,
                    rule=rule.name,
                )
        return Match(
            account=self.default_account,
            payee=None,
            narration=None,
            tags=frozenset(),
            rule=None,
        )


def _parse_rule(item: Any, *, where: str) -> Rule:
    if not isinstance(item, dict):
        raise RulesError(f"{where}: ожидался словарь, получен {type(item).__name__}")

    unknown = set(item) - set(RULE_KEYS)
    if unknown:
        raise RulesError(f"{where}: неизвестные ключи: {', '.join(sorted(unknown))}")

    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RulesError(f"{where}: обязателен непустой `name`")

    account = item.get("account")
    if account is None:
        raise RulesError(f"{where} ({name}): обязателен `account`")
    _check_account(account, f"{where} ({name}): account")

    match = item.get("match")
    if not isinstance(match, dict) or not match:
        raise RulesError(f"{where} ({name}): обязателен непустой `match`")
    unknown = set(match) - set(MATCH_KEYS)
    if unknown:
        raise RulesError(
            f"{where} ({name}): неизвестные ключи в `match`: {', '.join(sorted(unknown))}; "
            f"допустимы: {', '.join(MATCH_KEYS)}"
        )

    patterns = {}
    for field in TEXT_FIELDS:
        if field not in match:
            continue
        value = match[field]
        if not isinstance(value, str):
            raise RulesError(f"{where} ({name}): `match.{field}` должен быть строкой")
        try:
            # `text` — склейка полей через перевод строки, поэтому MULTILINE:
            # `^` и `$` в нём привязываются к границам поля, а не всей склейки.
            patterns[field] = re.compile(value, re.MULTILINE if field == "text" else 0)
        except re.error as exc:
            raise RulesError(f"{where} ({name}): `match.{field}` — невалидная регулярка: {exc}") from exc

    sign = match.get("sign")
    if sign is not None and sign not in ("+", "-"):
        raise RulesError(f"{where} ({name}): `match.sign` должен быть '+' или '-', получено {sign!r}")

    amount_min = _decimal(match.get("amount_min"), where=f"{where} ({name}): match.amount_min")
    amount_max = _decimal(match.get("amount_max"), where=f"{where} ({name}): match.amount_max")
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise RulesError(f"{where} ({name}): amount_min больше amount_max")

    payee = item.get("payee")
    if payee is not None and not isinstance(payee, str):
        raise RulesError(f"{where} ({name}): `payee` должен быть строкой")

    narration = item.get("narration")
    if narration is not None and not isinstance(narration, str):
        raise RulesError(f"{where} ({name}): `narration` должен быть строкой")

    raw_tags = item.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    if not isinstance(raw_tags, list):
        raise RulesError(f"{where} ({name}): `tags` должен быть списком строк")
    for tag in raw_tags:
        if not isinstance(tag, str) or not TAG_RE.match(tag):
            raise RulesError(f"{where} ({name}): недопустимый тег {tag!r} (без пробелов и без '#')")

    return Rule(
        name=name,
        account=account,
        patterns=patterns,
        sign=sign,
        amount_min=amount_min,
        amount_max=amount_max,
        payee=payee,
        narration=narration,
        tags=frozenset(raw_tags),
    )


def _check_account(value: Any, where: str) -> None:
    if not isinstance(value, str) or not ACCOUNT_RE.match(value):
        raise RulesError(
            f"{where}: {value!r} не похоже на имя счёта beancount "
            f"(например, Expenses:Food:Groceries)"
        )


def _decimal(value: Any, *, where: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise RulesError(f"{where}: {value!r} не число") from exc