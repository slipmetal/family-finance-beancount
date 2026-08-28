"""Мостик от fava к расширениям, которые живут в кодовом репозитории.

Расширения fava ищет только рядом с main.beancount, то есть здесь, в приватном
репозитории с леджером. Кода тут быть не должно — он в соседнем репозитории,
поэтому здесь только три строки: положить его корень в sys.path и втащить сам
класс расширения. Найдёт его fava сама — она ищет в модуле любой подкласс
FavaExtensionBase, неважно, объявлен он тут или импортирован.

Этот файл — исходник. Работать он должен рядом с main.beancount, то есть в
репозитории леджера, и копия туда кладётся при установке:

    copy fava_ext.py ledger\\

Подключается из main.beancount:

    2026-01-01 custom "fava-extension" "fava_ext"

Почему нельзя понадеяться на fava_import_config.py, который тоже правит
sys.path: расширения грузятся раньше конфига импорта.

FINANCE_APP нужен на сервере, где код лежит в /app, а леджер в /data/ledger.
Локально клон леджера лежит внутри кодового репозитория, и корень — уровнем выше.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("FINANCE_APP") or str(Path(__file__).resolve().parents[1]))

from finance.fava_upload import UploadStatements  # noqa: E402,F401  pylint: disable=wrong-import-position,unused-import
