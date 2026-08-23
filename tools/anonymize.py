"""Замена персональных данных при сборке тестовых фикстур.

Карта замен лежит в `tools/anonymize.yaml` и в репозиторий НЕ попадает: в левой
её колонке настоящие имена, номера счетов и маски карт. Образец формата —
`tools/anonymize.example.yaml`.

Кроме карты замен работает страховка: `LEAK_RE` ищет всё, что похоже на
идентификатор — восемь и более цифр подряд или маску карты, — и `check()`
падает, если что-то осталось незаменённым. Так забытая строчка в карте замен
не превращается в утечку молча: фикстура просто не соберётся.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "tools" / "anonymize.yaml"

#: Всё, что после замен обязано исчезнуть: длинные числовые идентификаторы и
#: маски карт. Суммы и даты под это не попадают — в них цифры идут группами
#: по три-четыре, разделённые запятой или точкой.
LEAK_RE = re.compile(r"\d{4}[*X]{4,}\d{4}|\d{4}\*+\d{4}|\d{8,}")

#: Сквозные идентификаторы, которые перечислять поимённо бессмысленно: их
#: много и они одноразовые. Заменяются по образцу, с сохранением подписи.
PATTERNS = [
    (re.compile(r"(մոբայլ բանկինգ\s*)\d+"), r"\g<1>100000000"),
    (re.compile(r"(ARCA-ի քաղվ\.\()\d+"), r"\g<1>100000000000"),
    (re.compile(r"(Reversal\s+)\d+"), r"\g<1>1000000"),
    (re.compile(r"(STATEMENT\s*[№#]?\s*)\d+", re.IGNORECASE), r"\g<1>10000000"),
]

#: Заглушки, которые подставляет PATTERNS: сами они на идентификаторы похожи,
#: и check() не должен считать их утечкой.
PATTERN_PLACEHOLDERS = frozenset({"100000000", "100000000000", "1000000", "10000000"})


class LeakError(Exception):
    """В фикстуре остались настоящие данные."""


class Anonymizer:
    """Карта замен: настоящее значение → заглушка."""

    def __init__(self, subs: dict[str, str]):
        # Длинные строки заменяются первыми: иначе замена номера счёта могла бы
        # разрезать номер, который является частью более длинной строки.
        self._subs = sorted(subs.items(), key=lambda kv: -len(kv[0]))

    @classmethod
    def load(cls, path: Path | None = None) -> Anonymizer:
        path = path if path is not None else MAP_PATH
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise LeakError(
                f"{path}: карта замен не найдена, без неё фикстуры собирать нельзя. "
                f"Заполните её по образцу: cp tools/anonymize.example.yaml {path}"
            ) from None

        subs: dict[str, str] = {}
        for section in ("names", "accounts", "cards", "numbers"):
            for old, new in (raw.get(section) or {}).items():
                subs[str(old)] = str(new)
        if not subs:
            raise LeakError(f"{path}: карта замен пуста")
        return cls(subs)

    def scrub(self, text: str) -> str:
        # ACBA местами разделяет слова в названии контрагента неразрывным
        # пробелом (\xa0). Глазами его не отличить от обычного, и замена по
        # карте молча промахивалась бы мимо имени.
        text = text.replace("\xa0", " ")
        for old, new in self._subs:
            text = text.replace(old, new)
        for pattern, replacement in PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def check(self, text: str, where: str) -> None:
        """Убедиться, что в тексте не осталось похожего на идентификатор.

        Заглушки из карты замен под LEAK_RE тоже попадают — поэтому сверяемся
        со списком уже подставленных значений, а не просто с регуляркой.
        """
        allowed = {new for _, new in self._subs} | PATTERN_PLACEHOLDERS
        for found in LEAK_RE.findall(text):
            if any(found in value for value in allowed):
                continue
            raise LeakError(
                f"{where}: {found!r} похоже на настоящий идентификатор. "
                f"Добавьте его в {MAP_PATH.name} или расширьте PATTERNS."
            )
        # Имена под LEAK_RE не попадают, поэтому отдельно убеждаемся, что
        # замена сработала: описанное в карте пережить её не должно.
        for old, _ in self._subs:
            if old in text:
                raise LeakError(
                    f"{where}: {old!r} описано в {MAP_PATH.name}, но осталось "
                    f"в тексте — замена не сработала"
                )
