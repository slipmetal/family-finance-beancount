"""Тесты настроек бота и коротких ключей для кнопок.

Настройки проверяются потому, что от них зависит, поднимется ли контейнер:
бот дополнителен, и опечатка в его переменных не должна оставить семью без
леджера. Ключи — потому, что на них держится вся работа кнопок.
"""

from __future__ import annotations

import pytest

from finance.bot import keys
from finance.bot.settings import Settings, SettingsError

TOKEN = "42:TEST"

#: Секрет вебхука бот заводит себе сам и пишет на диск. Пусть пишет во
#: временную папку: настоящая на сервере лежит на томе, а тестам туда не надо.
pytestmark = pytest.mark.usefixtures("finance_env")


def load(**env: str) -> Settings | None:
    """Настройки из окружения, собранного прямо здесь."""
    return Settings.load({"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_PUBLIC_URL": "https://x", **env})


# ──────────────────────────── бот необязателен ────────────────────────────


def test_no_token_means_no_bot():
    """Главное обещание: без токена контейнер поднимается как раньше."""
    assert Settings.load({}) is None


def test_blank_token_counts_as_no_token():
    assert Settings.load({"TELEGRAM_TOKEN": "   "}) is None


# ──────────────────────────── список допущенных ────────────────────────────


def test_allowlist_is_read_as_numbers():
    assert load(TELEGRAM_ALLOWED="111,222").allowed == frozenset({111, 222})


def test_allowlist_tolerates_spaces_and_a_trailing_comma():
    assert load(TELEGRAM_ALLOWED=" 111 , 222 ,").allowed == frozenset({111, 222})


def test_group_chats_have_negative_ids():
    assert load(TELEGRAM_ALLOWED="-100500").allowed == frozenset({-100500})


def test_empty_allowlist_is_allowed():
    """Так узнают свой chat id — бот с пустым списком за этим и запускается."""
    assert load().allowed == frozenset()


def test_junk_in_the_allowlist_is_refused():
    with pytest.raises(SettingsError, match="TELEGRAM_ALLOWED"):
        load(TELEGRAM_ALLOWED="я")


# ──────────────────────────── адрес вебхука ────────────────────────────


def test_public_url_comes_from_the_fly_app_name():
    settings = Settings.load({"TELEGRAM_TOKEN": TOKEN, "FLY_APP_NAME": "family-debit"})
    assert settings.public_url == "https://family-debit.fly.dev"


def test_given_url_wins_over_the_fly_one():
    settings = Settings.load(
        {"TELEGRAM_TOKEN": TOKEN, "FLY_APP_NAME": "a", "TELEGRAM_PUBLIC_URL": "https://b/"}
    )
    assert settings.public_url == "https://b"


def test_webhook_without_an_address_is_refused():
    """Иначе бот молча слушал бы порт, до которого никто не достучится."""
    with pytest.raises(SettingsError, match="адрес"):
        Settings.load({"TELEGRAM_TOKEN": TOKEN})


def test_polling_needs_no_address():
    """Режим для работы над разговором: публичного адреса там нет и не надо."""
    settings = Settings.load({"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_MODE": "polling"})
    assert settings.polling


def test_webhook_path_is_derived_from_the_token():
    """Отдельный секрет заводить не за что, а со сменой токена адрес меняется
    сам — старый перестаёт работать."""
    first = load().path
    second = Settings.load(
        {"TELEGRAM_TOKEN": "43:OTHER", "TELEGRAM_PUBLIC_URL": "https://x"}
    ).path

    assert first != second
    assert first == load().path, "тот же токен — тот же адрес"


def test_url_puts_the_path_under_the_proxied_prefix():
    """Префикс /tg/ проксирует Caddy; секрет за ним знает только бот."""
    assert load().url.startswith("https://x/tg/")


def test_a_bad_webhook_path_is_refused():
    with pytest.raises(SettingsError, match="TELEGRAM_WEBHOOK_PATH"):
        load(TELEGRAM_WEBHOOK_PATH="с пробелом")


def test_a_bad_webhook_secret_is_refused():
    with pytest.raises(SettingsError, match="TELEGRAM_WEBHOOK_SECRET"):
        load(TELEGRAM_WEBHOOK_SECRET="коротко")


def test_the_secret_is_not_the_path():
    """Адрес оседает в журналах доступа, заголовок — нигде. Совпади они,
    первый же включённый для отладки лог опубликовал бы и заголовок."""
    settings = load(TELEGRAM_WEBHOOK_SECRET="secret-secret-1")
    assert settings.secret != settings.path


# ──────────────────────────── ключи для кнопок ────────────────────────────


def test_the_same_name_always_gives_the_same_key():
    """На этом всё и держится: словаря нет, ключ считается заново."""
    assert keys.of("card0001_statement.csv") == keys.of("card0001_statement.csv")


def test_different_names_give_different_keys():
    assert keys.of("Assets:Ameria:Card0001") != keys.of("Assets:Ameria:Card0002")


def test_find_returns_the_only_match():
    names = ["один.csv", "два.csv"]
    assert keys.find(names, str, keys.of("два.csv")) == "два.csv"


def test_find_gives_up_when_nothing_matches():
    """Файл унесли, пока сообщение висело в чате, — угадывать тут нечего."""
    assert keys.find(["один.csv"], str, keys.of("нет-такого.csv")) is None


def test_find_gives_up_on_the_skip_button():
    """У «пропустить» нет предмета, и искать его не нужно."""
    assert keys.find(["один.csv"], str, keys.NOTHING) is None
