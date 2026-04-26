from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import math
from typing import Any

import lighter

from .parser import Signal, pick_tp3_tp4


def _norm_symbol(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


@dataclass(frozen=True)
class MarketMeta:
    symbol: str
    market_id: int
    supported_size_decimals: int
    supported_price_decimals: int
    min_base_amount: float
    min_quote_amount: float


class LighterExecutor:
    def __init__(self, *, base_url: str, api_key_config_path: Path, account_index: int, margin_mode: str):
        self._base_url = base_url
        self._api_key_config_path = api_key_config_path
        self._account_index = account_index
        self._margin_mode = margin_mode

        self._api_client: lighter.ApiClient | None = None
        self._signer: lighter.SignerClient | None = None
        self._markets_by_norm: dict[str, MarketMeta] = {}

    async def open(self) -> None:
        cfg = json.loads(self._api_key_config_path.read_text(encoding="utf-8"))
        api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self._base_url))
        signer = lighter.SignerClient(
            url=cfg.get("baseUrl", self._base_url),
            account_index=int(cfg.get("accountIndex", self._account_index)),
            api_private_keys={int(k): v for k, v in cfg["privateKeys"].items()},
        )

        err = signer.check_client()
        if err is not None:
            raise RuntimeError(str(err))

        self._api_client = api_client
        self._signer = signer

        await self._refresh_markets()

    async def close(self) -> None:
        if self._signer is not None:
            await self._signer.close()
        if self._api_client is not None:
            await self._api_client.close()

    async def _refresh_markets(self) -> None:
        assert self._api_client is not None
        order_api = lighter.OrderApi(self._api_client)
        ob = await order_api.order_books()
        markets: dict[str, MarketMeta] = {}
        for m in ob.order_books or []:
            meta = MarketMeta(
                symbol=m.symbol,
                market_id=int(m.market_id),
                supported_size_decimals=int(m.supported_size_decimals),
                supported_price_decimals=int(m.supported_price_decimals),
                min_base_amount=float(m.min_base_amount),
                min_quote_amount=float(m.min_quote_amount),
            )
            markets[_norm_symbol(m.symbol)] = meta
        self._markets_by_norm = markets

    def _resolve_market(self, signal_symbol: str) -> MarketMeta | None:
        ns = _norm_symbol(signal_symbol)
        if ns in self._markets_by_norm:
            return self._markets_by_norm[ns]

        # Try common quote suffix substitutions and base-only fallback.
        # Some Lighter markets are listed as base-only symbols (e.g. "STRK").
        candidates: list[str] = []
        quote_suffixes = ("USDT", "USDC", "USD", "PERP")
        for q in quote_suffixes:
            if ns.endswith(q) and len(ns) > len(q):
                base = ns[: -len(q)]
                candidates.extend([base, base + "USDC", base + "USDT", base + "USD"])

        # If nothing matched above, still try stripping a trailing stable suffix from markets side:
        # e.g. signal "ETHUSDT" while market might be "ETH".
        candidates.extend([ns.replace("USDT", "USDC"), ns.replace("USDC", "USDT")])

        for cand in candidates:
            if cand in self._markets_by_norm:
                return self._markets_by_norm[cand]
        return None

    async def ensure_leverage(self, market_id: int, requested: int) -> int:
        assert self._signer is not None
        mode = self._signer.CROSS_MARGIN_MODE if self._margin_mode == "CROSS" else self._signer.ISOLATED_MARGIN_MODE

        candidates = []
        if requested >= 10:
            candidates = [requested, 10, 5, 3, 2, 1]
        else:
            candidates = [requested, 3, 2, 1]

        last_err: Exception | None = None
        for lev in candidates:
            try:
                tx, tx_hash, err = await self._signer.update_leverage(
                    market_index=market_id, leverage=int(lev), margin_mode=mode
                )
                if err is None:
                    return int(lev)
                last_err = RuntimeError(str(err))
            except Exception as e:
                last_err = e

        raise RuntimeError(f"Failed to set leverage, last error: {last_err}")

    async def _get_current_market_price(self, market_id: int) -> float | None:
        """
        Returns the latest trade price for the market.
        """
        assert self._api_client is not None
        order_api = lighter.OrderApi(self._api_client)
        trades = await order_api.recent_trades(market_id, 1)
        if not trades or not trades.trades:
            return None
        latest = trades.trades[0]
        if latest.price is None:
            return None
        return float(latest.price)

    async def _get_available_capital_usd(self) -> float | None:
        """
        Returns available USD-equivalent account capital.
        """
        assert self._api_client is not None
        account_api = lighter.AccountApi(self._api_client)
        account = await account_api.account(by="index", value=str(self._account_index))
        if account is None:
            return None

        # The SDK response is a wrapper that usually contains `accounts: [...]`.
        # "Available to Trade" maps to available_balance on the first account object.
        account_obj: Any = account
        accounts = getattr(account, "accounts", None)
        if accounts:
            account_obj = accounts[0]

        for field in ("available_balance", "collateral", "cross_asset_value", "total_asset_value"):
            raw = getattr(account_obj, field, None)
            if raw is None:
                continue
            try:
                value = float(raw)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
        return None

    async def execute_signal(
        self,
        signal: Signal,
        *,
        capital_allocation_pct: float,
        fallback_notional_usd: float,
        dry_run: bool,
    ) -> dict[str, Any]:
        """
        Places:
        - entry limit order only when live market price is inside entry range
        - 2 reduce-only TP limit orders (75% TP3, 25% TP4, with fallbacks)
        - 1 reduce-only SL trigger order when possible
        """
        assert self._signer is not None

        market = self._resolve_market(signal.symbol)
        if market is None:
            return {"status": "skipped", "reason": "symbol_not_available", "symbol": signal.symbol}

        accuracy = signal.accuracy_pct or 0.0
        requested_leverage = 10 if accuracy >= 95.0 else 3

        current_price = await self._get_current_market_price(market.market_id)
        if current_price is None:
            return {"status": "skipped", "reason": "no_market_price", "market": market.symbol}

        if not (signal.entry_low <= current_price <= signal.entry_high):
            return {
                "status": "skipped",
                "reason": "market_price_out_of_entry_range",
                "market": market.symbol,
                "market_price": current_price,
                "entry_low": signal.entry_low,
                "entry_high": signal.entry_high,
            }

        entry_price = current_price
        if entry_price <= 0:
            return {"status": "skipped", "reason": "bad_entry_price"}

        available_capital_usd = await self._get_available_capital_usd()
        if available_capital_usd is not None:
            notional_usd = available_capital_usd * max(0.0, capital_allocation_pct)
        else:
            notional_usd = fallback_notional_usd

        if notional_usd <= 0:
            return {"status": "skipped", "reason": "notional_not_positive"}

        base_size = (notional_usd * requested_leverage) / entry_price
        min_base_from_quote = market.min_quote_amount / entry_price if entry_price > 0 else 0.0
        required_min_base = max(market.min_base_amount, min_base_from_quote)
        was_bumped = False
        if base_size < required_min_base:
            base_size = required_min_base
            was_bumped = True
            # Keep notional aligned with bumped trade size.
            notional_usd = (base_size * entry_price) / requested_leverage

        base_amount_int = int(math.floor(base_size * (10 ** market.supported_size_decimals)))
        if base_amount_int <= 0:
            return {"status": "skipped", "reason": "size_too_small"}

        price_int = int(round(entry_price * (10 ** market.supported_price_decimals)))

        is_ask = signal.side.lower() == "short"

        tp3, tp4 = pick_tp3_tp4(signal.tps)
        tp3_int = int(round(tp3 * (10 ** market.supported_price_decimals)))
        tp4_int = int(round(tp4 * (10 ** market.supported_price_decimals)))
        sl_int = int(round(signal.stop_loss * (10 ** market.supported_price_decimals)))

        # Split sizes
        tp3_size = int(math.floor(base_amount_int * 0.75))
        tp4_size = base_amount_int - tp3_size
        if tp3_size <= 0 or tp4_size <= 0:
            return {"status": "skipped", "reason": "tp_split_too_small"}

        if dry_run:
            return {
                "status": "dry_run",
                "market": market.symbol,
                "market_id": market.market_id,
                "requested_leverage": requested_leverage,
                "available_capital_usd": available_capital_usd,
                "capital_allocation_pct": capital_allocation_pct,
                "notional_usd": notional_usd,
                "size_bumped_to_market_min": was_bumped,
                "min_base_amount": market.min_base_amount,
                "min_quote_amount": market.min_quote_amount,
                "market_price": current_price,
                "entry_price": entry_price,
                "base_amount_int": base_amount_int,
                "price_int": price_int,
                "tp3": tp3,
                "tp4": tp4,
                "sl": signal.stop_loss,
            }

        applied_leverage = await self.ensure_leverage(market.market_id, requested_leverage)

        # Entry order
        api_key_index, nonce = self._signer.nonce_manager.next_nonce()
        _, entry_hash, entry_err = await self._signer.create_order(
            market_index=market.market_id,
            client_order_index=int(nonce) % 2_000_000_000,
            base_amount=base_amount_int,
            price=price_int,
            is_ask=is_ask,
            order_type=self._signer.ORDER_TYPE_LIMIT,
            time_in_force=self._signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=False,
            trigger_price=0,
            nonce=nonce,
            api_key_index=api_key_index,
        )
        if entry_err is not None:
            raise RuntimeError(str(entry_err))

        # Take profits (reduce-only, opposite side)
        api_key_index, nonce = self._signer.nonce_manager.next_nonce(api_key_index)
        _, tp3_hash, tp3_err = await self._signer.create_order(
            market_index=market.market_id,
            client_order_index=(int(nonce) % 2_000_000_000),
            base_amount=tp3_size,
            price=tp3_int,
            is_ask=not is_ask,
            order_type=self._signer.ORDER_TYPE_LIMIT,
            time_in_force=self._signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=True,
            trigger_price=0,
            nonce=nonce,
            api_key_index=api_key_index,
        )
        if tp3_err is not None:
            raise RuntimeError(str(tp3_err))

        api_key_index, nonce = self._signer.nonce_manager.next_nonce(api_key_index)
        _, tp4_hash, tp4_err = await self._signer.create_order(
            market_index=market.market_id,
            client_order_index=(int(nonce) % 2_000_000_000),
            base_amount=tp4_size,
            price=tp4_int,
            is_ask=not is_ask,
            order_type=self._signer.ORDER_TYPE_LIMIT,
            time_in_force=self._signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=True,
            trigger_price=0,
            nonce=nonce,
            api_key_index=api_key_index,
        )
        if tp4_err is not None:
            raise RuntimeError(str(tp4_err))

        # Stop loss: best-effort. Some markets/accounts may not support trigger orders; if it errors,
        # we surface it but keep entry/TPs.
        sl_hash = None
        sl_error = None
        try:
            api_key_index, nonce = self._signer.nonce_manager.next_nonce(api_key_index)
            _, sl_hash, sl_err = await self._signer.create_order(
                market_index=market.market_id,
                client_order_index=(int(nonce) % 2_000_000_000),
                base_amount=base_amount_int,
                price=sl_int,
                is_ask=not is_ask,  # close position
                order_type=getattr(self._signer, "ORDER_TYPE_STOP_LOSS", self._signer.ORDER_TYPE_LIMIT),
                time_in_force=self._signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=True,
                trigger_price=sl_int,
                nonce=nonce,
                api_key_index=api_key_index,
            )
            if sl_err is not None:
                sl_error = str(sl_err)
        except Exception as e:
            sl_error = str(e)

        return {
            "status": "placed",
            "market": market.symbol,
            "market_id": market.market_id,
            "applied_leverage": applied_leverage,
            "entry_tx_hash": str(entry_hash),
            "tp3_tx_hash": str(tp3_hash),
            "tp4_tx_hash": str(tp4_hash),
            "sl_tx_hash": str(sl_hash) if sl_hash else None,
            "sl_error": sl_error,
        }

