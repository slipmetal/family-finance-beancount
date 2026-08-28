"""Тесты страницы «Выписки» — расширения fava из finance/fava_upload.py.

Проверяется сквозь настоящее приложение fava: расширение это в первую очередь
маршруты и шаблон, и подделать их значило бы проверять не то. Леджер для этого
собирается временный — настоящий лежит в приватном репозитории.

Инбокс, список счетов и правила подменяются в `finance.config` фикстурой
`finance_env` из conftest: и раскладка, и импорт читают их оттуда в момент
вызова, а не при импорте модуля.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from finance.fava_upload import UploadStatements, describe
from tests.conftest import copy
from tests.fixtures import AMERIA_ACCOUNT_DIR, AMERIA_CARD_DIR

CARD = AMERIA_CARD_DIR / "card0001_statement.csv"
ACCOUNT = AMERIA_ACCOUNT_DIR / "usd_statement.csv"

LEDGER = """\
option "title" "Тест"
option "operating_currency" "AMD"
2026-01-01 custom "fava-extension" "fava_ext"
"""


@pytest.fixture
def page(tmp_path, finance_env):
    """Клиент fava, у которого страница расширения смотрит во временный inbox."""
    inbox = finance_env

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "main.beancount").write_text(LEDGER, encoding="utf-8")
    # Тот самый мостик, который кладётся рядом с настоящим main.beancount: fava
    # ищет расширения только рядом с ним. Проверяется заодно и он.
    shim = Path(__file__).resolve().parents[1] / "fava_ext.py"
    (ledger_dir / "fava_ext.py").write_text(shim.read_text(encoding="utf-8"), "utf-8")

    from fava.application import create_app  # noqa: PLC0415

    app = create_app([str(ledger_dir / "main.beancount")])
    client = app.test_client()
    slug = client.get("/").headers["Location"].split("/")[1]
    return Page(client, f"/{slug}/extension/{UploadStatements.__name__}/", inbox)


class Page:
    """Страница расширения: открыть, загрузить, назначить."""

    def __init__(self, client, url: str, inbox: Path) -> None:
        """Клиент fava, адрес страницы и папка, в которую она кладёт."""
        self.client = client
        self.url = url
        self.inbox = inbox

    def open(self) -> str:
        """Открыть страницу и вернуть её разметку."""
        response = self.client.get(self.url)
        assert response.status_code == 200
        return response.get_data(as_text=True)

    def upload(self, source: Path, name: str, account: str = "") -> str:
        """Отправить файл формой загрузки. Возвращает адрес, куда увели.

        Имя передаётся отдельно от содержимого: именно оно тут и проверяется,
        а фикстур с нужными именами под каждый случай не напасёшься.
        """
        response = self.client.post(
            self.url + "upload",
            data={
                "account": account,
                "statement": (BytesIO(source.read_bytes()), name),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 302
        return response.headers["Location"]

    def assign(self, name: str, account: str) -> str:
        """Назначить счёт файлу, который уже лежит в inbox."""
        response = self.client.post(
            self.url + "assign", data={"file": name, "account": account}
        )
        assert response.status_code == 302
        return response.headers["Location"]

    def names(self) -> list[str]:
        """Что лежит в inbox — по именам: имя тут и есть предмет проверки."""
        return sorted(path.name for path in self.inbox.iterdir())


# ─────────────────────────── что показано на странице ───────────────────────────


def test_page_opens_with_the_upload_form(page):
    text = page.open()
    assert "Загрузить выписку" in text
    assert "Assets:Ameria:Card0001" in text, "счета должны быть в выпадающем списке"


def test_settled_file_is_shown_with_its_account(page):
    copy(ACCOUNT, page.inbox, "statement_march.csv")
    assert "Assets:Ameria:Usd" in page.open()


def test_ambiguous_file_offers_a_choice(page):
    copy(CARD, page.inbox, "export_777.csv")
    text = page.open()
    assert "Назначить" in text, "у спорного файла должна быть форма выбора"
    assert "Assets:Ameria:Card0002" in text


def test_service_files_are_not_listed(page):
    (page.inbox / ".gitkeep").write_text("", encoding="utf-8")
    assert ".gitkeep" not in page.open()


# ──────────────────────────────── загрузка ────────────────────────────────


def test_upload_places_a_settled_file_as_is(page):
    page.upload(ACCOUNT, "statement_march.csv")
    assert page.names() == ["statement_march.csv"]


def test_upload_with_a_chosen_account_renames_the_file(page):
    location = page.upload(CARD, "export_777.csv", account="Assets:Ameria:Card0002")
    assert page.names() == ["card0002_export_777.csv"]
    assert "Card0002" in location, "в адрес возвращается, куда файл попал"


def test_upload_of_an_ambiguous_file_keeps_it_for_later(page):
    """Счёт не выбран и по содержимому не определился. Файл всё равно
    сохраняется: заставлять выбирать его в форме заново — плохая мена."""
    location = page.upload(CARD, "export_777.csv")
    assert page.names() == ["export_777.csv"]
    assert "%D1%81%D1%87%D1%91%D1%82" in location or "счёт" in location


def test_upload_strips_directories_from_the_name(page):
    """Имя приходит от браузера, и подниматься по нему из inbox нельзя."""
    page.upload(ACCOUNT, "../../statement_march.csv")
    assert page.names() == [".. .. statement_march.csv"]


def test_upload_without_a_file_is_reported(page):
    response = page.client.post(
        page.url + "upload", data={"account": ""}, content_type="multipart/form-data"
    )
    assert response.status_code == 302
    assert page.names() == []


# ─────────────────────────── назначение счёта ───────────────────────────


def test_assign_renames_a_file_already_in_inbox(page):
    copy(CARD, page.inbox, "export_777.csv")
    page.assign("export_777.csv", "Assets:Ameria:Card0001")
    assert page.names() == ["card0001_export_777.csv"]


def test_assign_refuses_a_file_outside_inbox(page, tmp_path):
    """Имя файла приходит из формы — по нему нельзя выйти за пределы inbox."""
    outside = tmp_path / "secret.csv"
    outside.write_text("не трогать\n", encoding="utf-8")

    page.assign("../secret.csv", "Assets:Ameria:Card0001")

    assert outside.exists()
    assert page.names() == []


def test_assign_rejects_an_unknown_account(page):
    copy(CARD, page.inbox, "export_777.csv")
    page.assign("export_777.csv", "Assets:Нет:Такого")
    assert page.names() == ["export_777.csv"], "файл не тронут"


# ──────────────────────────── формулировки ────────────────────────────


def test_unknown_file_is_described_without_alarm(inbox, tmp_path):
    """ACBA отдаёт по счёту два файла, и один импортёру не нужен. Он ляжет
    в inbox неопознанным, и это нормально — сообщение не должно пугать."""
    alien = tmp_path / "notes.csv"
    alien.write_text("не то и не в том формате\n", encoding="utf-8")

    row = describe(inbox, alien)

    assert not row.settled
    assert not row.choices
    assert "лишний файл" in row.problem
