"""Общее для всех точек входа проекта."""

from __future__ import annotations

import os
import subprocess
import sys

#: Метка перезапуска: страхует от бесконечной петли, если UTF-8 так и не включился.
_RELAUNCHED = "FINANCE_UTF8_RELAUNCHED"


def ensure_utf8_mode() -> None:
    """Гарантировать, что интерпретатор работает в режиме UTF-8.

    beangulp пишет и вывод `extract`, и golden-файлы обычным `open()` без явной
    кодировки, то есть в кодировке локали. На Windows это cp1251, куда армянский
    текст выписки не помещается, и всё падает на UnicodeEncodeError.

    Режим UTF-8 включается только переменной окружения PYTHONUTF8 и только до
    старта интерпретатора, поэтому единственный способ починить это изнутри —
    перезапустить себя. Дешевле, чем требовать от пользователя каждый раз
    выставлять переменную вручную.
    """
    if sys.flags.utf8_mode:
        return

    if os.environ.get(_RELAUNCHED):
        print(
            "Внимание: не удалось включить режим UTF-8. Текст в армянской "
            "и русской раскладке может записаться с ошибками.",
            file=sys.stderr,
        )
        return

    env = {**os.environ, "PYTHONUTF8": "1", _RELAUNCHED: "1"}
    completed = subprocess.run([sys.executable, *sys.argv], env=env)  # noqa: S603
    sys.exit(completed.returncode)
