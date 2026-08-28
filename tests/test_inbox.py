"""Тесты раскладки выписок по счетам.

Проверяется то, ради чего модуль и появился: файл должен попасть в inbox под
именем, которое опознаётся ровно одним импортёром, а спрашивать человека нужно
только там, где по содержимому счёт действительно не определить.
"""

from __future__ import annotations

import pytest

from finance.inbox import InboxError
from tests.conftest import account_named, copy
from tests.fixtures import ACBA_CARD_DIR, AMERIA_ACCOUNT_DIR, AMERIA_CARD_DIR

CARD = AMERIA_CARD_DIR / "card0001_statement.csv"
ACCOUNT = AMERIA_ACCOUNT_DIR / "usd_statement.csv"


# ──────────────────────────── кого нужно спросить ────────────────────────────


def test_named_card_belongs_to_one_account(inbox, tmp_path):
    """Метка в имени — счёт определён, спрашивать нечего."""
    verdict = inbox.verdict(copy(CARD, tmp_path, "card0001_2026-08.csv"))
    assert verdict.settled
    assert verdict.owners[0].name == "Assets:Ameria:Card0001"


def test_two_cards_in_one_currency_need_a_human(inbox, tmp_path):
    """Тот единственный случай, ради которого команда вообще спрашивает."""
    verdict = inbox.verdict(copy(CARD, tmp_path, "export_777.csv"))
    assert not verdict.owners
    assert [account.name for account in verdict.candidates] == [
        "Assets:Ameria:Card0001",
        "Assets:Ameria:Card0002",
    ]


def test_account_number_settles_the_file_without_a_marker(inbox, tmp_path):
    """Хвост номера в описании процентов заменяет метку — вопроса не будет."""
    verdict = inbox.verdict(copy(ACCOUNT, tmp_path, "statement_march.csv"))
    assert verdict.settled
    assert verdict.owners[0].name == "Assets:Ameria:Usd"


def test_foreign_file_is_not_offered_to_anybody(inbox, tmp_path):
    """Не выписка — не предлагаем счета: переименование тут не поможет."""
    garbage = tmp_path / "notes.csv"
    garbage.write_text("не то и не в том формате\n", encoding="utf-8")
    assert inbox.verdict(garbage).unknown


def test_content_identified_account_ignores_the_file_name(inbox, tmp_path):
    """Номер счёта ACBA лежит внутри файла: как файл назван, тому счёту всё равно."""
    verdict = inbox.verdict(copy(ACBA_CARD_DIR / "card.xls", tmp_path, "unknown.xls"))
    assert verdict.settled
    assert verdict.owners[0].name == "Assets:Acba:Amd"


def test_accounts_without_markers_are_never_proposed(inbox, tmp_path):
    """Переименование помогает только счетам с меткой, поэтому в кандидаты
    попадают лишь они — даже когда файл на самом деле чужой."""
    alien = copy(ACBA_CARD_DIR / "card.xls", tmp_path, "unknown.xls")
    assert inbox.candidates(alien) == []


def test_file_claimed_by_two_accounts_is_disputed(inbox, tmp_path):
    """В имени сразу две метки — на таком падает и обычный импорт."""
    lines = ACCOUNT.read_text(encoding="utf-8").splitlines()
    without = [lines[0]] + [line for line in lines[1:] if "ըստ" not in line]
    path = tmp_path / "usd_eur_2026-08.csv"
    path.write_text("\n".join(without) + "\n", encoding="utf-8")

    verdict = inbox.verdict(path)
    assert verdict.disputed
    assert len(verdict.owners) == 2


# ─────────────────────────────── раскладка ───────────────────────────────


def test_place_puts_the_marker_into_the_name(inbox, tmp_path):
    source = copy(CARD, tmp_path, "export_777.csv")
    target, owner = inbox.place(source, account_named(inbox, "Assets:Ameria:Card0002"))

    assert target.name == "card0002_export_777.csv"
    assert target.parent == inbox.directory
    assert owner.name == "Assets:Ameria:Card0002"
    assert not source.exists(), "по умолчанию файл перемещается, а не копируется"


def test_place_keeps_the_name_of_an_already_settled_file(inbox, tmp_path):
    source = copy(ACCOUNT, tmp_path, "statement_march.csv")
    target, owner = inbox.place(source)

    assert target.name == "statement_march.csv"
    assert owner.name == "Assets:Ameria:Usd"


def test_place_drops_the_original_name_when_it_carries_a_foreign_marker(inbox, tmp_path):
    """`usd_...csv`, отданный счёту EUR, стал бы `eur_usd_...csv` — и достался
    бы обоим счетам сразу. Имя проверяется делом, поэтому такое не проедет."""
    lines = ACCOUNT.read_text(encoding="utf-8").splitlines()
    without = [lines[0]] + [line for line in lines[1:] if "ըստ" not in line]
    source = tmp_path / "usd_2026-08.csv"
    source.write_text("\n".join(without) + "\n", encoding="utf-8")

    target, owner = inbox.place(source, account_named(inbox, "Assets:Ameria:Eur"))

    assert target.name == "eur.csv", "исходное имя пришлось выбросить целиком"
    assert owner.name == "Assets:Ameria:Eur"
    assert len(inbox.owners(target)) == 1


def test_place_does_not_overwrite_what_is_already_there(inbox, tmp_path):
    first = inbox.place(copy(CARD, tmp_path, "card0001_2026-08.csv"))[0]
    second = inbox.place(copy(CARD, tmp_path, "card0001_2026-08.csv"))[0]

    assert first.name == "card0001_2026-08.csv"
    assert second.name == "card0001_2026-08-2.csv"
    assert first.exists() and second.exists()


def test_place_can_leave_the_source_alone(inbox, tmp_path):
    source = copy(CARD, tmp_path, "card0001_2026-08.csv")
    inbox.place(source, move=False)
    assert source.exists()


def test_place_refuses_a_file_it_cannot_make_unambiguous(inbox, tmp_path):
    """Обещание модуля: либо файл лежит однозначно, либо ошибка. Молча
    положить спорный файл нельзя — импорт на нём упадёт позже и непонятнее."""
    garbage = copy(CARD, tmp_path, "export_777.csv")
    garbage.write_text("не выписка\n", encoding="utf-8")

    with pytest.raises(InboxError, match="однозначным"):
        inbox.place(garbage, account_named(inbox, "Assets:Ameria:Card0001"))
