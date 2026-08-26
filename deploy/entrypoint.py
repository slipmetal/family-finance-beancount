#!/usr/bin/env python3
"""Запуск связки Caddy + fava внутри контейнера.

Порядок такой:

1. подготовить том: леджер, папку для выписок, каталог секретов;
2. разложить конфигурацию аутентификации из переменных окружения;
3. поднять фоновую синхронизацию леджера с git-репозиторием;
4. запустить fava на петле и отдать управление Caddy.

fava слушает только 127.0.0.1: наружу смотрит исключительно Caddy, поэтому
обойти пароль и второй фактор, постучавшись прямо в fava, нельзя.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

APP = Path("/app")
DATA = Path(os.environ.get("FINANCE_DATA", "/data"))
LEDGER = DATA / "ledger"
INBOX = DATA / "inbox"
AUTH = DATA / "auth"

FAVA_PORT = "5000"
#: Как часто складывать изменения леджера в коммит.
SYNC_INTERVAL = int(os.environ.get("LEDGER_SYNC_SECONDS", "900"))
#: Сколько живёт сессия портала, секунды. Сутки: у входа появилась кнопка
#: выхода, поэтому короткий срок ради безопасности больше не нужен.
SESSION_SECONDS = os.environ.get("FAVA_SESSION_SECONDS", "86400")


def log(message: str) -> None:
    print(f"[entrypoint] {message}", flush=True)


def run(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


# ─────────────────────────────── том ───────────────────────────────


def prepare_dirs() -> None:
    """Создать каталоги на томе. Леджер и ссылки здесь НЕ трогаем."""
    for path in (DATA, INBOX, AUTH):
        path.mkdir(parents=True, exist_ok=True)


def link_import_config() -> None:
    """Связать том с конфигом импорта из образа.

    main.beancount ссылается на ../fava_import_config.py, то есть на файл рядом
    с каталогом ledger. Код живёт в образе, данные на томе — отсюда ссылка.

    Делается ПОСЛЕ git: это часть образа, а не данные, и в репозитории ей не
    место. Иначе на новом томе она оказывается неотслеживаемым файлом поверх
    отслеживаемого, и восстановление спотыкается об неё.
    """
    shim = DATA / "fava_import_config.py"
    if shim.exists() or shim.is_symlink():
        return
    try:
        shim.symlink_to(APP / "fava_import_config.py")
    except OSError:
        # Тома fly.io ссылки поддерживают, но подстрахуемся копией.
        shutil.copy(APP / "fava_import_config.py", shim)


#: Скелет на случай, когда репозиторий леджера пуст. Настоящий леджер живёт
#: в отдельном приватном репозитории, в образе его нет вовсе.
BOOTSTRAP_MAIN = """\
;; Скелет, созданный при первом запуске: репозиторий леджера был пуст.
;; Заведите счета в accounts.beancount и импортируйте выписки.

option "title" "Family ledger"
option "operating_currency" "AMD"

;; Разбор выписок прямо из браузера. Пути — относительно этого файла.
2026-01-01 custom "fava-option" "import-config" "../fava_import_config.py"
2026-01-01 custom "fava-option" "import-dirs" "../inbox"

include "accounts.beancount"
include "transactions/manual.beancount"
"""

BOOTSTRAP_MANUAL = """\
;; Сюда fava дописывает то, что добавлено через браузер: запись попадает
;; в файл, где стоит директива insert-entry.

2026-01-01 custom "fava-option" "insert-entry" ".*"
"""


def seed_ledger() -> None:
    """Создать недостающие части леджера.

    Вызывается ПОСЛЕ попытки забрать репозиторий. Порядок принципиален: иначе
    на новом томе — а это и есть восстановление после отказа диска — скелет
    перекрыл бы настоящий леджер из git.

    Правила и список счетов тоже живут рядом с леджером, в приватном
    репозитории: там номера счетов, фамилии контрагентов и список мерчантов,
    и в кодовом репозитории им не место. В образе лежат только образцы.
    Досеиваем их по отдельности — если репозиторий уже есть, но заполнен
    не до конца, fava упала бы на открытии вкладки импорта.
    """
    LEDGER.mkdir(parents=True, exist_ok=True)
    for name in ("rules.yaml", "accounts.yaml"):
        target = LEDGER / name
        if not target.exists():
            log(f"{name} в репозитории нет — кладу образец, его нужно поправить под себя")
            shutil.copy(APP / name.replace(".yaml", ".example.yaml"), target)

    if (LEDGER / "main.beancount").exists():
        return
    log("репозиторий леджера пуст — создаю скелет, счета нужно завести самим")
    (LEDGER / "transactions").mkdir(parents=True, exist_ok=True)
    (LEDGER / "main.beancount").write_text(BOOTSTRAP_MAIN, encoding="utf-8")
    (LEDGER / "accounts.beancount").write_text(
        ";; План счетов. Без него импортированные проводки не пройдут проверку.\n",
        encoding="utf-8",
    )
    (LEDGER / "transactions" / "manual.beancount").write_text(BOOTSTRAP_MANUAL, encoding="utf-8")


# ──────────────────────────── аутентификация ────────────────────────────


def _pairs(raw: str) -> list[tuple[str, str]]:
    """Разобрать `user:value,user:value`.

    Пару делит первое двоеточие, поэтому двоеточия внутри пароля переживают
    разбор. А вот запятая разделяет пользователей: пароль с запятой развалится
    на два куска, и второй уедет в ошибку про неразобранную пару.
    """
    out = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            sys.exit(f"не разобрать пару логин:значение — {chunk!r}")
        user, value = chunk.split(":", 1)
        out.append((user.strip(), value.strip()))
    return out


def _caddy_token(value: str) -> str:
    """Обернуть значение в кавычки так, как их понимает Caddyfile."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


#: Портал требует у каждого адрес почты, хотя писем никуда не шлёт. Домен
#: выдуманный, наружу не виден и ни на что не влияет.
EMAIL_DOMAIN = "family.local"

#: При пустой базе портал заводит учётную запись администратора сам, и отменить
#: это нельзя. Поэтому задаём ей имя и пароль явно: пароль случайный, лежит на
#: томе и не печатается в лог. Полагаться на то, каким его придумает плагин,
#: не хочется. До леджера учётка всё равно не дотягивается: политика пускает
#: только роль authp/user, а у неё authp/admin.
PORTAL_ADMIN = "webadmin"

#: Файлы прежней схемы входа: список для basic_auth, секреты TOTP и ключ
#: подписи её сессий. Формат сессий сменился, старый ключ всё равно ничего
#: не откроет, а второй фактор теперь лежит в базе портала. Держать это на
#: томе незачем — секреты, которые уже ничего не защищают, только мешают.
STALE_AUTH_FILES = ("basic-users.caddy", "2fa-secrets.json", "jwt-sign-key.txt")


def write_auth_config() -> None:
    """Сформировать список пользователей портала и ключ подписи сессий."""
    if not re.fullmatch(r"[1-9][0-9]*", SESSION_SECONDS):
        sys.exit(f"FAVA_SESSION_SECONDS — целое число секунд, а не {SESSION_SECONDS!r}")

    users = _pairs(os.environ.get("FAVA_USERS", ""))
    if not users:
        sys.exit(
            "FAVA_USERS не задана. Формат: 'artem:пароль,dariia:пароль'\n"
            "Пароль задаётся открытым текстом: bcrypt портал считает сам и "
            "хранит только хеш.\nЗапятая разделяет пользователей, поэтому "
            "в самом пароле её быть не должно."
        )

    for user, _ in users:
        if user == PORTAL_ADMIN:
            sys.exit(
                f"логин {PORTAL_ADMIN!r} занят служебной учётной записью "
                "портала — возьмите другой"
            )

    for user, password in users:
        # Портал держит свою политику логинов — 3–50 символов — и ругается на
        # нарушение уже при старте. Проверим заранее и понятными словами.
        if not re.fullmatch(r"[a-z0-9._-]{3,50}", user):
            sys.exit(
                f"логин {user!r} портал не примет: нужно 3–50 символов из "
                "строчных латинских букв, цифр, точки, дефиса и подчёркивания"
            )
        # Политика паролей портала — 8–128 символов.
        if not 8 <= len(password) <= 128:
            sys.exit(
                f"в пароле пользователя {user!r} {len(password)} символов, "
                "а портал требует от 8 до 128"
            )
        # Фигурные скобки Caddy раскрывает как подстановку даже внутри кавычек,
        # и пароль с ними молча превратился бы во что-то другое. Ровно та же
        # ловушка, что раньше была с `$` в bcrypt-хеше, только теперь её
        # достаточно предотвратить проверкой.
        if "{" in password or "}" in password:
            sys.exit(
                f"в пароле пользователя {user!r} есть фигурная скобка: "
                "Caddy принял бы её за подстановку. Уберите { и }."
            )

    # Caddyfile импортирует этот файл внутрь блока local identity store.
    # overwrite означает, что источник правды о пароле — переменная окружения:
    # пароль, изменённый через портал, при перезапуске вернётся к заданному.
    # Привязанных аутентификаторов это не касается, они живут в users.json.
    portal_users = AUTH / "portal-users.caddy"
    portal_users.write_text(
        "".join(
            f"user {user} {{\n"
            f"\tname {_caddy_token(user)}\n"
            f"\temail {_caddy_token(user + '@' + EMAIL_DOMAIN)}\n"
            f"\tpassword {_caddy_token(password)} overwrite\n"
            f"\troles authp/user\n"
            "}\n"
            for user, password in users
        ),
        encoding="utf-8",
    )
    log(f"вход разрешён: {', '.join(user for user, _ in users)}")

    # Ключ подписи сессий. Живёт на томе: пересоздание разлогинивает всех и
    # остаётся способом отозвать сразу все сессии. Отдельную сессию теперь
    # закрывает кнопка выхода, ради которой всё и затевалось.
    sign_key = AUTH / "portal-sign-key.txt"
    if not sign_key.exists():
        sign_key.write_text(secrets.token_hex(32), encoding="utf-8")
        log("создан новый ключ подписи сессий")
    # Caddy забирает ключ из окружения, а не из текста конфига: так он не виден
    # ни в Caddyfile, ни в выводе `caddy adapt`.
    os.environ["PORTAL_SIGN_KEY"] = sign_key.read_text(encoding="utf-8").strip()

    # А вот срок сессии через окружение не передать: плагину нужно число уже
    # при разборе конфига, и подстановку {env....} он в этих директивах не
    # разворачивает — спотыкается о саму строку. Пишем их файлом.
    portal_session = AUTH / "portal-session.caddy"
    portal_session.write_text(
        f"crypto default token lifetime {SESSION_SECONDS}\n"
        f"cookie lifetime {SESSION_SECONDS}\n",
        encoding="utf-8",
    )

    # Пароль служебной учётной записи — см. PORTAL_ADMIN. Передаётся окружением,
    # поэтому в конфиг и в лог не попадает.
    admin_secret = AUTH / "portal-admin-secret.txt"
    if not admin_secret.exists():
        admin_secret.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    os.environ["AUTHP_ADMIN_USER"] = PORTAL_ADMIN
    os.environ["AUTHP_ADMIN_EMAIL"] = f"{PORTAL_ADMIN}@{EMAIL_DOMAIN}"
    os.environ["AUTHP_ADMIN_SECRET"] = admin_secret.read_text(encoding="utf-8").strip()

    for name in STALE_AUTH_FILES:
        stale = AUTH / name
        if stale.exists():
            stale.unlink()
            log(f"удалён файл прежней схемы входа: {name}")

    if os.environ.get("FAVA_TOTP"):
        log(
            "FAVA_TOTP больше не используется: аутентификатор привязывается "
            "через портал. Переменную можно снять."
        )

    for path in (portal_users, sign_key, admin_secret, AUTH / "users.json"):
        if path.exists():
            path.chmod(0o600)


# ────────────────────────── синхронизация с git ──────────────────────────


def git_available() -> bool:
    return bool(os.environ.get("LEDGER_REMOTE")) and (LEDGER / ".git").exists()


def setup_git() -> None:
    """Подключить каталог леджера к приватному репозиторию, если он задан.

    Корень репозитория — именно `ledger/`, а не весь том. Это важно для
    безопасности: секреты (`auth/`) и оригиналы выписок (`inbox/`) лежат
    ВЫШЕ корня, поэтому попасть в коммит не могут в принципе — никакие
    правила исключений для этого не нужны и не могут быть перезаписаны
    чужим .gitignore из репозитория.
    """
    remote = os.environ.get("LEDGER_REMOTE")
    if not remote:
        log("LEDGER_REMOTE не задана — история правок вестись не будет")
        return

    LEDGER.mkdir(parents=True, exist_ok=True)
    fresh = not (LEDGER / ".git").exists()
    if fresh:
        log("подключаю каталог леджера к репозиторию")
        run("git", "init", "-q", "-b", "main", cwd=LEDGER)
        run("git", "remote", "add", "origin", remote, cwd=LEDGER)

    run("git", "config", "user.email", os.environ.get("GIT_EMAIL", "fava@localhost"), cwd=LEDGER)
    run("git", "config", "user.name", os.environ.get("GIT_NAME", "fava"), cwd=LEDGER)
    run("git", "remote", "set-url", "origin", remote, cwd=LEDGER)

    fetched = run("git", "fetch", "-q", "origin", "main", cwd=LEDGER, check=False)
    if fetched.returncode:
        log("в репозитории пока нет ветки main — это нормально при первом запуске")
    elif fresh:
        # Новый том с непустым репозиторием — это восстановление после отказа
        # диска. Забираем леджер до того, как его засеет копия из образа.
        # reset, а не checkout: checkout отказывается затирать неотслеживаемые
        # файлы, а на томе они к этому моменту уже могут быть.
        log("восстанавливаю леджер из репозитория")
        run("git", "reset", "-q", "--hard", "FETCH_HEAD", cwd=LEDGER)
    else:
        rebased = run("git", "pull", "-q", "--rebase", "origin", "main", cwd=LEDGER, check=False)
        if rebased.returncode:
            log(f"git pull не удался: {rebased.stderr.strip()[:200]}")


def sync_once() -> None:
    # Репозиторий укоренён в самом каталоге леджера, поэтому `add -A` не может
    # захватить ничего лишнего: секреты и выписки лежат выше по дереву.
    status = run("git", "status", "--porcelain", cwd=LEDGER, check=False)
    if status.returncode or not status.stdout.strip():
        return
    run("git", "add", "-A", cwd=LEDGER, check=False)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    run("git", "commit", "-q", "-m", f"Леджер: изменения из fava, {stamp}", cwd=LEDGER, check=False)
    pushed = run("git", "push", "-q", "origin", "HEAD:main", cwd=LEDGER, check=False)
    if pushed.returncode:
        log(f"git push не удался: {pushed.stderr.strip()[:200]}")
    else:
        log("изменения леджера отправлены в репозиторий")


def sync_loop() -> None:
    while True:
        time.sleep(SYNC_INTERVAL)
        try:
            sync_once()
        except Exception as error:  # noqa: BLE001 — фоновый цикл не должен ронять контейнер
            log(f"ошибка синхронизации: {error}")


# ─────────────────────────────── запуск ───────────────────────────────


def main() -> None:
    prepare_dirs()
    # Репозиторий раньше засева: иначе на новом томе копия из образа перекрыла
    # бы актуальный леджер, который лежит в git.
    setup_git()
    link_import_config()
    seed_ledger()
    write_auth_config()

    if git_available():
        # Первый коммит делаем сразу, а не через SYNC_INTERVAL: машина может
        # заснуть раньше, и засеянный леджер так и не попал бы в репозиторий.
        sync_once()
        threading.Thread(target=sync_loop, daemon=True).start()
        log(f"синхронизация с git каждые {SYNC_INTERVAL} с")

    fava = subprocess.Popen(
        [
            "fava",
            "--host", "127.0.0.1",
            "--port", FAVA_PORT,
            str(LEDGER / "main.beancount"),
        ],
        env={**os.environ, "FINANCE_INBOX": str(INBOX)},
    )
    log(f"fava поднята на 127.0.0.1:{FAVA_PORT}")

    caddy = subprocess.Popen(["caddy", "run", "--config", "/app/deploy/Caddyfile"])
    log("caddy принимает запросы на :8080")

    stopping = threading.Event()

    def shutdown(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        log(f"получен сигнал {signum}, останавливаюсь")
        # Досинхронизировать НАДО до остановки: fly усыпляет машину, когда
        # запросов нет, и это запросто случается сразу после того, как человек
        # сохранил импорт. Без этого правка ждала бы следующего тика.
        if git_available():
            sync_once()
        for process in (caddy, fava):
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Python остаётся главным процессом — иначе обработчики сигналов пропадут
    # вместе с ним, и досинхронизации при остановке не будет. Заодно падение
    # любого из двух процессов роняет контейнер, и fly поднимает его заново.
    while not stopping.is_set():
        for name, process in (("caddy", caddy), ("fava", fava)):
            code = process.poll()
            if code is not None:
                log(f"{name} завершился с кодом {code}, останавливаю остальное")
                shutdown(f"выход {name}", None)
                sys.exit(code or 1)
        time.sleep(1)

    for process in (caddy, fava):
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    sys.exit(0)


if __name__ == "__main__":
    main()
