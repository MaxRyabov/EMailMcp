"""OAuth provider profiles: the protocol of each provider is data, not code (D1)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    device_code_url: str
    token_url: str
    revoke_url: str
    scope: str
    imap_host: str
    imap_port: int
    device_grant_type: str
    device_code_param: str
    verification_url_field: str
    # Codes that keep the poll going; the value is how many seconds to add to the interval.
    pending_errors: Mapping[str, int]
    # Codes that stop the poll, with the text shown to the user.
    fatal_errors: Mapping[str, str]
    client_secret_required: bool
    expiry_warning: dt.timedelta
    # Where the user revokes the app's access by hand.
    access_management_url: str
    # AUTHENTICATE XOAUTH2 in one line (SASL-IR) instead of imaplib's two steps (D17).
    xoauth2_inline: bool = False
    # Why IMAP rejects a token the provider has just issued (`imap-mcp auth`).
    new_token_rejection_reasons: tuple[str, ...] = field(default=())
    # Why IMAP rejects a stored token that has not expired yet (status reauth-required).
    valid_token_rejection_reasons: tuple[str, ...] = field(default=())


_CODE_EXPIRED = "код неверен или просрочен, запустите `imap-mcp auth` заново"

YANDEX = ProviderProfile(
    name="yandex",
    device_code_url="https://oauth.yandex.ru/device/code",
    token_url="https://oauth.yandex.ru/token",
    revoke_url="https://oauth.yandex.ru/revoke_token",
    scope="mail:imap_ro",
    imap_host="imap.yandex.com",
    imap_port=993,
    device_grant_type="device_code",
    device_code_param="code",
    verification_url_field="verification_url",
    # Read-only mappings: the profile is shared by every account.
    pending_errors=MappingProxyType({"authorization_pending": 0, "slow_down": 5}),
    fatal_errors=MappingProxyType(
        {
            "invalid_client": (
                "неверный client_id или client_secret: client_id задаётся в accounts.toml, "
                "client_secret хранится в системном хранилище (удалить — `imap-mcp forget`)"
            ),
            "unauthorized_client": "приложение на модерации или заблокировано в Яндекс OAuth",
            "invalid_scope": (
                "права приложения изменились: на oauth.yandex.ru у приложения должно быть "
                "выбрано только «Доступ на чтение писем в почтовом ящике»"
            ),
            "invalid_grant": _CODE_EXPIRED,
            "bad_verification_code": _CODE_EXPIRED,
            "expired_token": _CODE_EXPIRED,
            "invalid_request": "провайдер отклонил запрос как некорректный",
            "unsupported_grant_type": "провайдер не поддерживает вход по коду устройства",
            "access_denied": "доступ не предоставлен, токен не получен",
        }
    ),
    client_secret_required=False,
    expiry_warning=dt.timedelta(days=30),
    access_management_url="https://id.yandex.ru/personal/data-access",
    new_token_rejection_reasons=(
        "в настройках почты выключен «IMAP с OAuth-токенами» (Все настройки → Почтовые программы)",
        "для ящика Яндекс 360 администратор организации запретил IMAP или сторонние приложения",
    ),
    valid_token_rejection_reasons=(
        "доступ приложения отозван в Яндекс ID (https://id.yandex.ru/personal/data-access)",
        "выключен «IMAP с OAuth-токенами» (Все настройки → Почтовые программы) "
        "или его запретил администратор Яндекс 360",
    ),
)

PROVIDERS: dict[str, ProviderProfile] = {YANDEX.name: YANDEX}
