from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_file: str
    telegram_session_string: str | None
    telegram_channel: str

    lighter_base_url: str
    lighter_api_key_config: Path
    lighter_account_index: int

    trade_notional_usd: float
    capital_allocation_pct: float
    entry_mode: str
    margin_mode: str
    dry_run: bool

    @staticmethod
    def from_env() -> "Config":
        api_id = os.getenv("TELEGRAM_API_ID", "").strip()
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        channel = os.getenv("TELEGRAM_CHANNEL", "").strip().lstrip("@")
        if not api_id or not api_hash or not channel:
            raise ValueError(
                "Missing TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_CHANNEL in environment."
            )

        session_file = os.getenv("TELEGRAM_SESSION_FILE", "ggshot.session").strip()
        session_string = os.getenv("TELEGRAM_SESSION_STRING", "").strip() or None

        base_url = os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai").strip()
        api_key_cfg = os.getenv("LIGHTER_API_KEY_CONFIG", "api_key_config.json").strip()
        account_index = int(os.getenv("LIGHTER_ACCOUNT_INDEX", "1").strip())

        trade_notional = float(os.getenv("TRADE_NOTIONAL_USD", "100").strip())
        capital_allocation_pct = float(os.getenv("CAPITAL_ALLOCATION_PCT", "0.10").strip())
        entry_mode = os.getenv("ENTRY_MODE", "mid").strip().lower()
        margin_mode = os.getenv("MARGIN_MODE", "CROSS").strip().upper()
        dry_run = _env_bool("DRY_RUN", True)

        return Config(
            telegram_api_id=int(api_id),
            telegram_api_hash=api_hash,
            telegram_session_file=session_file,
            telegram_session_string=session_string,
            telegram_channel=channel,
            lighter_base_url=base_url,
            lighter_api_key_config=Path(api_key_cfg),
            lighter_account_index=account_index,
            trade_notional_usd=trade_notional,
            capital_allocation_pct=capital_allocation_pct,
            entry_mode=entry_mode,
            margin_mode=margin_mode,
            dry_run=dry_run,
        )

