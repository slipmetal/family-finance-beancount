"""Настройки бота: всё, что он берёт из окружения, в одном месте.

Проверяются они на старте и разом. Бот дополнителен — леджер живёт и без
него, — поэтому нехватка настроек не должна ронять контейнер: `load()`
возвращает None и объясняет, чего не хватило, а `deploy/entrypoint.py` просто
не поднимает третий процесс.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field

from finance import config

#: Сколько байт готовы принять. Столько же отдаёт `getFile` у самого Telegram,
#: так что больший файл всё равно не скачать.
MAX_BYTES = 20 * 1024 * 1024

#: Что импортёры вообще умеют читать. Прочее незачем и класть в inbox: там
#: оно только мозолило бы глаза в списке неопознанных.
SUFFIXES = (".csv", ".xls", ".xlsx", ".pdf")

#: Логин допускает только это; тот же алфавит у `secrets.token_urlsafe`.
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{8,256}")


class SettingsError(Exception):
    """Настройки заданы, но неправильно. Сообщение говорит, что именно."""


@dataclass(frozen=True)
class Settings:
    """Что нужно боту, чтобы работать."""

    token: str
    #: Кого бот слушает. Пусто — не слушает никого, но называет собеседнику
    #: его chat id: иначе взять этот id негде.
    allowed: frozenset[int] = field(default_factory=frozenset)
    #: Секрет в адресе вебхука и секрет в заголовке — РАЗНЫЕ значения.
    #: Адрес едет в строке запроса и оседает в любом журнале доступа, который
    #: однажды включат; заголовок не оседает нигде. Совпади они — первый же
    #: включённый для отладки лог опубликовал бы и заголовок.
    path: str = ""
    secret: str = ""
    #: Публичный адрес, по которому Telegram достучится. Пусто — вебхук не
    #: ставим, но порт всё равно слушаем: так бот поднимается за туннелем.
    public_url: str = ""
    host: str = "127.0.0.1"
    port: int = 5001
    #: polling — для работы над разговором без публичного адреса. У бота
    #: ровно один вебхук, поэтому режимы несовместимы, и для локальной
    #: работы нужен ОТДЕЛЬНЫЙ бот от @BotFather.
    polling: bool = False

    @property
    def url(self) -> str:
        """Полный адрес вебхука — тот, что уходит в setWebhook."""
        return f"{self.public_url}{self.route}"

    @property
    def route(self) -> str:
        """Путь вебхука. Caddy проксирует префикс, секрет знает только бот."""
        return f"/tg/{self.path}"

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> Settings | None:
        """Настройки — или None, если бота не просили.

        None это не ошибка: без TELEGRAM_TOKEN контейнер обязан подниматься
        ровно как раньше, с одной только fava.
        """
        env = env if env is not None else dict(os.environ)
        token = env.get("TELEGRAM_TOKEN", "").strip()
        if not token:
            return None

        polling = env.get("TELEGRAM_MODE", "webhook").strip() == "polling"
        public_url = _public_url(env)
        if not polling and not public_url:
            raise SettingsError(
                "не знаю своего публичного адреса: задайте TELEGRAM_PUBLIC_URL "
                "или запустите с TELEGRAM_MODE=polling"
            )

        return cls(
            token=token,
            allowed=_allowed(env),
            path=_path(env, token),
            secret=_secret(env),
            public_url=public_url,
            port=int(env.get("TELEGRAM_PORT") or 5001),
            polling=polling,
        )


def _allowed(env: dict[str, str]) -> frozenset[int]:
    """Список chat id. Пустой — законное состояние, см. Settings.allowed."""
    chats = [chat.strip() for chat in env.get("TELEGRAM_ALLOWED", "").split(",")]
    wanted = [chat for chat in chats if chat]
    # Отрицательные id бывают у групп — их тоже разрешаем записать.
    if any(not re.fullmatch(r"-?[0-9]+", chat) for chat in wanted):
        raise SettingsError(
            "TELEGRAM_ALLOWED — числовые chat id через запятую; "
            f"получено: {env.get('TELEGRAM_ALLOWED', '')!r}"
        )
    return frozenset(int(chat) for chat in wanted)


def _public_url(env: dict[str, str]) -> str:
    """Адрес снаружи. На fly он выводится из имени приложения."""
    given = env.get("TELEGRAM_PUBLIC_URL", "").strip().rstrip("/")
    if given:
        return given
    app = env.get("FLY_APP_NAME", "").strip()
    return f"https://{app}.fly.dev" if app else ""


def _path(env: dict[str, str], token: str) -> str:
    """Секретная часть адреса вебхука.

    По умолчанию выводится из токена: заводить ради неё отдельный секрет не за
    что, а при смене токена адрес меняется сам — старый перестаёт работать.
    """
    given = env.get("TELEGRAM_WEBHOOK_PATH", "").strip()
    if not given:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    if not SECRET_RE.fullmatch(given):
        raise SettingsError("TELEGRAM_WEBHOOK_PATH — 8+ символов из A-Za-z0-9_-")
    return given


def _secret(env: dict[str, str]) -> str:
    """Значение заголовка X-Telegram-Bot-Api-Secret-Token.

    Задан руками — берём его; иначе бот заводит секрет сам и хранит на томе,
    по образцу ключа подписи сессий в deploy/entrypoint.py. Человеку в этой
    паре делать нечего: бот и ставит секрет, и проверяет его.
    """
    given = env.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if given and not SECRET_RE.fullmatch(given):
        raise SettingsError(
            "TELEGRAM_WEBHOOK_SECRET — 8–256 символов из A-Za-z0-9_-; "
            "столько же допускает и сам Telegram"
        )
    if given:
        return given

    # config.RUN читается в момент вызова, а не при импорте: так его можно
    # подменить в тестах, и секрет не окажется на настоящем диске.
    kept = config.RUN / "webhook-secret.txt"
    if not kept.exists():
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        kept.chmod(0o600)
    return kept.read_text(encoding="utf-8").strip()
