"""Страховка от попадания личных данных в кодовый репозиторий.

Проверка нарочно устроена не по списку настоящих номеров — такой список сам был
бы утечкой, — а по форме: номер счёта это 15–16 цифр подряд, маска карты это
четыре цифры, звёздочки и ещё четыре. Всё, что выглядит так и не начинается с
условленных префиксов-заглушек, считается настоящим и роняет тест.

Список файлов берётся у самого git: `ls-files --cached --others
--exclude-standard` — это ровно то, что уйдёт в коммит. Обход каталогов со
списком исключений тут не годится: он привязан к именам, и второй клон леджера
под любым другим именем (`ledger-backup`, `ledger.tmp`) в него не попал бы,
а в коммит — попал.

Ловится ровно тот случай, ради которого всё и затевалось: настоящий номер,
скопированный в правило, в тест или в README «на минутку».
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: С чего начинаются заглушки: свои счета, чужие счета, карты.
PLACEHOLDER_PREFIXES = ("1000", "2000", "4000", "5555", "4111", "0000")

ACCOUNT_RE = re.compile(r"(?<!\d)\d{15,16}(?!\d)")
CARD_RE = re.compile(r"(?<!\d)\d{4}[*X]{4,}\d{4}(?!\d)")


def repo_files() -> list[Path]:
    """Файлы, которые git закоммитит: отслеживаемые плюс не игнорируемые."""
    result = subprocess.run(  # noqa: S603
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.fail(
            "не удалось спросить у git список файлов, а без него проверка на "
            f"утечки бессмысленна: {result.stderr.decode(errors='replace')[:200]}"
        )
    names = result.stdout.decode("utf-8", errors="replace").split("\0")
    return [ROOT / name for name in names if name and (ROOT / name).is_file()]


def test_repository_is_not_empty():
    """Если список файлов не соберётся, проверка ниже станет зелёной впустую."""
    assert len(repo_files()) > 20


@pytest.mark.parametrize("pattern", [ACCOUNT_RE, CARD_RE], ids=["номер счёта", "маска карты"])
def test_no_real_identifiers_in_repository(pattern):
    found: list[str] = []
    for path in repo_files():
        # Фикстура .xls двоичная, поэтому читаем как текст с заменой: номера
        # в BIFF лежат обычными строками и так тоже находятся.
        text = path.read_text(encoding="utf-8", errors="replace")
        found += [
            f"{path.relative_to(ROOT).as_posix()}: {match}"
            for match in pattern.findall(text)
            if not match.startswith(PLACEHOLDER_PREFIXES)
        ]
    assert not found, (
        "похоже на настоящие номера — им место в приватном репозитории:\n" + "\n".join(found)
    )