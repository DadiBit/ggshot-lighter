from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv
from telethon import TelegramClient, events

from .config import Config
from .lighter_exec import LighterExecutor
from .parser import parse_signal


log = logging.getLogger("ggshot_lighter_bot")


async def main() -> None:
    load_dotenv()
    cfg = Config.from_env()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    if cfg.telegram_session_string:
        tg = TelegramClient(
            session=cfg.telegram_session_string,
            api_id=cfg.telegram_api_id,
            api_hash=cfg.telegram_api_hash,
        )
    else:
        tg = TelegramClient(
            session=cfg.telegram_session_file,
            api_id=cfg.telegram_api_id,
            api_hash=cfg.telegram_api_hash,
        )

    exec_ = LighterExecutor(
        base_url=cfg.lighter_base_url,
        api_key_config_path=cfg.lighter_api_key_config,
        account_index=cfg.lighter_account_index,
        margin_mode=cfg.margin_mode,
    )

    await exec_.open()

    @tg.on(events.NewMessage(chats=cfg.telegram_channel, incoming=True))
    async def handler(event: events.NewMessage.Event) -> None:
        text = event.raw_text or ""
        sig = parse_signal(text)
        if sig is None:
            return

        log.info("Parsed signal symbol=%s side=%s entry=%s-%s", sig.symbol, sig.side, sig.entry_low, sig.entry_high)
        try:
            res = await exec_.execute_signal(
                sig,
                capital_allocation_pct=cfg.capital_allocation_pct,
                fallback_notional_usd=cfg.trade_notional_usd,
                dry_run=cfg.dry_run,
            )
            log.info("Execution result: %s", res)
        except Exception:
            log.exception("Failed executing signal for %s", sig.symbol)

    async with tg:
        log.info("Listening to Telegram channel=%s (dry_run=%s)", cfg.telegram_channel, cfg.dry_run)
        await tg.run_until_disconnected()

    await exec_.close()


if __name__ == "__main__":
    asyncio.run(main())

