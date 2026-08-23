"""Конфиг импорта для fava: те же импортёры, что у CLI.

Подключается опцией `import-config` в ledger/main.beancount. Fava выполняет
этот файл через runpy и ждёт от него `CONFIG` — список объектов beangulp
Importer. Имена импортёров обязаны быть уникальными, поэтому у каждого
переопределено свойство `name`.

Благодаря этому выписку можно загрузить прямо из браузера: fava кладёт файл
в первую папку из `import-dirs`, опознаёт импортёром, показывает разобранные
проводки и даёт их сохранить в леджер.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Fava запускает файл через runpy, sys.path при этом не трогает.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finance.config import build_importers  # noqa: E402

CONFIG = build_importers()