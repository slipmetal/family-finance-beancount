"""Расширение fava: страница «Выписки» — загрузить файл и назвать счёт.

Закрывает единственное, чего не умеет штатная вкладка Import: сказать, какому
счёту принадлежит файл. Про выписку Ameriabank этого не знает никто, кроме
владельца, — номера счёта в ней нет (см. finance/importers/ameria.py), — а
спросить вкладке Import негде: она либо опознала файл, либо молчит.

Роль у страницы намеренно узкая: она доводит файл до состояния «лежит в inbox
под именем, по которому его заберёт ровно один импортёр». Дальше — штатная
вкладка Import: разбор и сохранение делает она.

Разбирать и сохранять здесь же было бы удобнее ровно один раз, а потом
неприятно: вкладка Import пишет проводки в transactions/manual.beancount
(через `insert-entry`), а tools/merge_extract.py — в transactions/<год>.beancount.
Разделение сделано намеренно, и заводить третий путь записи ради формы не стоит.

Вся работа лежит в finance/inbox.py — том же движке, что у `import.py add`.
Здесь только веб-обвязка: у CLI на этом месте вопрос в терминале.

Подключается из main.beancount, рядом с настройками импорта:

    2026-01-01 custom "fava-extension" "fava_ext"

`fava_ext` — трёхстрочный модуль рядом с самим main.beancount: fava ищет
расширения только там (fava/core/extensions.py), а код живёт отдельно. Он же
кладёт корень кода в sys.path. Понадеяться на то, что это уже сделал
fava_import_config.py, нельзя: расширения грузятся раньше конфига импорта.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from fava.ext import FavaExtensionBase, extension_endpoint
from flask import redirect, request, url_for
from werkzeug.wrappers import Response

# Row и describe лежат в finance/inbox.py, а не здесь: тот же пересказ нужен
# боту в Telegram, а тащить ради него fava с flask в чат незачем. Отсюда они
# по-прежнему видны — и шаблону, и тестам.
from finance.inbox import Account, Inbox, InboxError, Row, describe, plain_name

#: Значение выпадающего списка «разобраться по содержимому файла».
AUTO = ""


class UploadStatements(FavaExtensionBase):
    """Страница «Выписки»: что лежит в inbox и чего ему не хватает."""

    report_title = "Выписки"

    # ─────────────────────────── для шаблона ───────────────────────────

    def rows(self) -> list[Row]:
        """Что лежит в inbox и кому достанется.

        Строится на каждый запрос: accounts.yaml и rules.yaml правятся при
        живой fava, да и файлы в inbox появляются мимо этой страницы — через
        штатную загрузку или прямо на диске.
        """
        inbox = self._inbox()
        if not inbox.directory.exists():
            return []
        return [
            describe(inbox, path)
            for path in sorted(inbox.directory.iterdir())
            # `.gitkeep` и прочие точечные файлы — служебные, не выписки.
            if path.is_file() and not path.name.startswith(".")
        ]

    def accounts(self) -> list[str]:
        """Все счета — для выпадающего списка в форме загрузки."""
        return sorted(account.name for account in self._inbox().accounts)

    def message(self) -> str:
        """Итог прошлого действия. Через адрес, а не через flash: сессий у fava
        нет — секретного ключа приложению никто не задаёт."""
        return request.args.get("message", "")

    def action(self, endpoint: str) -> str:
        """Адрес своего эндпоинта — для `action` у формы.

        Собирается из адреса самой страницы, а не через `url_for` по имени
        маршрута: у `url_for` первый параметр тоже называется `endpoint`, и
        одноимённый кусок маршрута fava с ним сталкивается — вызов падает на
        «got multiple values for argument 'endpoint'». Адрес страницы всегда
        оканчивается косой чертой: так объявлен маршрут.
        """
        return self._page() + endpoint

    # ──────────────────────────── действия ────────────────────────────

    @extension_endpoint("upload", ["POST"])
    def upload(self) -> Response:
        """Принять файл из формы и положить его в inbox."""
        upload = request.files.get("statement")
        if upload is None or not upload.filename:
            return self._back("Файл не выбран.")

        inbox = self._inbox()
        try:
            account = self._chosen(inbox)
        except LookupError as error:
            return self._back(str(error))

        with TemporaryDirectory(prefix="fava-upload-") as tmp:
            source = Path(tmp) / plain_name(upload.filename)
            upload.save(source)
            if account is None and not inbox.verdict(source).settled:
                # Счёт не выбран и по содержимому не определился. Отвергнуть
                # файл значило бы заставить выбирать его в форме заново, так
                # что кладём как есть — назвать счёт можно строкой в таблице.
                kept = inbox.keep(source)
                return self._back(f"{kept.name}: счёт не определён, выберите его в списке.")
            return self._place(inbox, source, account)

    @extension_endpoint("assign", ["POST"])
    def assign(self) -> Response:
        """Назначить счёт файлу, который уже лежит в inbox."""
        inbox = self._inbox()
        # Имя приходит из формы, поэтому от него берётся только имя: подняться
        # из inbox по «..» и переименовать что-нибудь чужое не выйдет.
        name = Path(request.form.get("file", "")).name
        source = inbox.directory / name
        if not name or not source.is_file():
            return self._back(f"{name or 'файл'}: в inbox такого нет.")

        try:
            account = self._chosen(inbox)
        except LookupError as error:
            return self._back(str(error))
        if account is None:
            return self._back(f"{name}: счёт не выбран.")
        return self._place(inbox, source, account)

    # ──────────────────────────── внутреннее ────────────────────────────

    @staticmethod
    def _inbox() -> Inbox:
        return Inbox.build()

    def _chosen(self, inbox: Inbox) -> Account | None:
        """Счёт, выбранный в форме. None — «разобраться по содержимому»."""
        name = request.form.get("account", AUTO)
        if name == AUTO:
            return None
        for account in inbox.accounts:
            if account.name == name:
                return account
        raise LookupError(f"{name}: такого счёта нет в accounts.yaml.")

    def _place(self, inbox: Inbox, source: Path, account: Account | None) -> Response:
        try:
            target, owner = inbox.place(source, account)
        except InboxError as error:
            return self._back(str(error))
        return self._back(f"{target.name} → {owner.name}")

    def _page(self, **params: str) -> str:
        return url_for("extension_report", extension_name=self.name, **params)

    def _back(self, message: str) -> Response:
        """Вернуться на страницу и сказать, чем всё кончилось."""
        return redirect(self._page(message=message))
