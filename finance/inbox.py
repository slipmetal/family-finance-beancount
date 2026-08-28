"""Раскладка выгруженных выписок по счетам: кто заберёт файл и как его назвать.

Задача одна: положить файл в `inbox/` так, чтобы его забрал ровно один
импортёр. Обычно для этого ничего не нужно — счёт написан внутри самой выписки.
Исключение — Ameriabank: номера счёта в его файлах нет, и счёт, неотличимый по
содержимому от соседнего, приходится называть меткой в имени файла
(см. finance/importers/ameria.py).

Модуль ничего не спрашивает и ничего не печатает: он только отвечает, кто из
счетов возьмёт файл и кто взял бы его после переименования. Спрашивает
вызывающий — у CLI это вопрос в терминале, у бота были бы кнопки в чате.

Про какой файл кому достанется, здесь не рассуждают, а спрашивают у самих
импортёров тем же вопросом, который задаст импорт: копия файла под подходящим
именем предъявляется `identify()`. Поэтому в модуле нет знания ни про один
банк — и не появится, когда добавится следующий.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from finance import config
from finance.categorize import Rules


class InboxError(Exception):
    """Файл не удалось положить так, чтобы его забрал нужный счёт."""


@dataclass(frozen=True)
class Account:
    """Счёт вместе с импортёром, который разбирает его выписки."""

    importer: Any
    name: str

    @property
    def marker(self) -> str:
        """Метка счёта в имени файла; пустая, если счёт опознаётся по содержимому.

        Метка есть только у импортёров Ameriabank. Спрашиваем через getattr,
        а не по классу: так модуль не знает про банки, а новый импортёр
        с метками подхватится сам.
        """
        return getattr(self.importer, "marker", "")


@dataclass(frozen=True)
class Verdict:
    """Что известно про файл до того, как его куда-то класть."""

    path: Path
    #: Счета, которые забирают файл прямо сейчас, как он назван.
    owners: list[Account]
    #: Счета, которые забрали бы его после переименования. Считаются только
    #: тогда, когда владельца нет: спрашивать «а кто ещё?» незачем.
    candidates: list[Account]

    @property
    def settled(self) -> bool:
        """Файл уже назван так, что его заберёт ровно один счёт."""
        return len(self.owners) == 1

    @property
    def disputed(self) -> bool:
        """Файл забирают двое — beangulp на таком падает, и не зря."""
        return len(self.owners) > 1

    @property
    def unknown(self) -> bool:
        """Файл не берёт никто и не возьмёт даже после переименования."""
        return not self.owners and not self.candidates


class Inbox:
    """Папка, из которой идёт импорт, и правила именования в ней."""

    def __init__(self, accounts: list[Account], directory: Path) -> None:
        self.accounts = accounts
        self.directory = directory

    @classmethod
    def build(cls, rules: Rules | None = None, directory: Path | None = None) -> Inbox:
        """Собрать по accounts.yaml — тому же файлу, что и весь остальной импорт."""
        specs = config.load_accounts()
        importers = config.build_importers(rules, specs)
        accounts = [
            Account(importer, spec["account"])
            for importer, spec in zip(importers, specs, strict=True)
        ]
        return cls(accounts, directory or config.INBOX)

    def owners(self, path: Path) -> list[Account]:
        """Счета, забирающие файл с таким именем и таким содержимым."""
        return [account for account in self.accounts if account.importer.identify(str(path))]

    def candidates(self, path: Path) -> list[Account]:
        """Счета, которым файл достанется, если назвать его их меткой.

        Проверяется делом: копия файла под именем-меткой предъявляется
        импортёру. Счёт без метки пропускается — он опознаётся по содержимому,
        и раз файл не взял, то не возьмёт его ни под каким именем.
        """
        found = []
        with tempfile.TemporaryDirectory(prefix="finance-inbox-") as tmp:
            probe_dir = Path(tmp)
            for account in self.accounts:
                if not account.marker:
                    continue
                probe = probe_dir / f"{account.marker}{path.suffix}"
                shutil.copyfile(path, probe)
                if account.importer.identify(str(probe)):
                    found.append(account)
                probe.unlink()
        return found

    def verdict(self, path: Path) -> Verdict:
        owners = self.owners(path)
        return Verdict(path, owners, [] if owners else self.candidates(path))

    def place(
        self, source: Path, account: Account | None = None, *, move: bool = True
    ) -> tuple[Path, Account]:
        """Положить файл в inbox под именем, которое опознаётся однозначно.

        Имя тоже проверяется делом: файл кладётся и предъявляется всем
        импортёрам сразу. Забрал не тот счёт или сразу двое — имя меняется
        на следующее, а неудачная копия убирается.

        Ловится этим вот что: в исходном имени уже сидит чужая метка.
        `usd_2026-08.csv`, отданный счёту EUR, стал бы `eur_usd_2026-08.csv` —
        и достался бы обоим счетам сразу. Второе имя-кандидат исходное
        отбрасывает целиком.

        `account=None` — файл уже опознан там, где лежит; имя не меняем,
        но однозначность всё равно перепроверяем: в inbox могло лежать что-то,
        отчего файл стал спорным.

        Возвращается не только путь, но и счёт, которому файл в итоге достался:
        вызывающему обычно есть что про это сказать человеку.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        tried = []
        for stem in self._stems(source, account.marker if account else ""):
            target = self._free_path(stem, source.suffix)
            shutil.copyfile(source, target)
            owners = self.owners(target)
            if len(owners) == 1 and (account is None or owners[0].name == account.name):
                if move:
                    source.unlink()
                return target, owners[0]
            target.unlink()
            tried.append((target.name, [owner.name for owner in owners]))

        raise InboxError(
            f"{source.name}: ни одно имя не сделало файл однозначным. "
            + "; ".join(
                f"{name} — {', '.join(owners) if owners else 'не опознан никем'}"
                for name, owners in tried
            )
        )

    def keep(self, source: Path, *, move: bool = True) -> Path:
        """Положить файл в inbox как есть, не разбираясь, кому он достанется.

        Нужно ровно одному сценарию: файл загрузили через браузер, счёт по
        содержимому не определить, а переспросить уже поздно — форма отправлена,
        и отвергнуть файл значило бы заставить выбирать его заново. Он ложится
        под свободным именем и ждёт, пока счёт назовут отдельно.

        Штатный путь — `place()`: он либо кладёт однозначно, либо отказывается.
        Здесь такого обещания нет, и полагаться на него нельзя: файл, положенный
        так, импорт не увидит, пока его не переименуют.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._free_path(source.stem, source.suffix)
        shutil.copyfile(source, target)
        if move:
            source.unlink()
        return target

    @staticmethod
    def _stems(source: Path, marker: str) -> Iterator[str]:
        """Имена-кандидаты, от самого понятного к самому надёжному.

        Сначала исходное имя с меткой впереди: в нём обычно записан период,
        и терять его жалко. Потом — одна метка: исходное имя отбрасывается
        целиком, если именно оно и мешает.
        """
        if not marker:
            yield source.stem
            return
        yield f"{marker}_{source.stem}"
        yield marker

    def _free_path(self, stem: str, suffix: str) -> Path:
        """Первое незанятое имя: класть поверх чужой выписки нельзя."""
        target = self.directory / f"{stem}{suffix}"
        number = 2
        while target.exists():
            target = self.directory / f"{stem}-{number}{suffix}"
            number += 1
        return target
