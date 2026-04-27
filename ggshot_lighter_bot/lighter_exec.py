from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import math
import re
from typing import Any

import lighter
from lighter.signer_client import CreateOrderTxReq

from .parser import Signal, pick_tp3_tp4


def _norm_symbol(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


def _extract_tx_hash(tx_response: Any) -> str | None:
    """
    Returns a compact tx hash from SDK response objects.
    """
    if tx_response is None:
        return None

    for field in ("tx_hash", "txHash"):
        value = getattr(tx_response, field, None)
        if value:
            return str(value)

    if isinstance(tx_response, dict):
        for field in ("tx_hash", "txHash"):
            value = tx_response.get(field)
            if value:
                return str(value)

    match = re.search(r"tx_hash='([^']+)'", str(tx_response))
    if match:
        return match.group(1)
    return str(tx_response)


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
        - grouped TP3/SL reduce-only exits linked to entry
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

        tp3, _ = pick_tp3_tp4(signal.tps)
        tp3_int = int(round(tp3 * (10 ** market.supported_price_decimals)))
        sl_int = int(round(signal.stop_loss * (10 ** market.supported_price_decimals)))

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
                "sl": signal.stop_loss,
            }

        applied_leverage = await self.ensure_leverage(market.market_id, requested_leverage)

        # Grouped order:
        # - Entry LIMIT IOC
        # - TP3 TAKE_PROFIT_LIMIT
        # - SL STOP_LOSS_LIMIT
        # The grouped request lets Lighter manage trigger lifecycle server-side.
        api_key_index, nonce = self._signer.nonce_manager.next_nonce()
        entry_order = CreateOrderTxReq(
            MarketIndex=market.market_id,
            ClientOrderIndex=int(nonce) % 2_000_000_000,
            BaseAmount=base_amount_int,
            Price=price_int,
            IsAsk=int(is_ask),
            Type=self._signer.ORDER_TYPE_LIMIT,
            TimeInForce=self._signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            ReduceOnly=0,
            TriggerPrice=0,
            OrderExpiry=0,
        )

        tp3_order = CreateOrderTxReq(
            MarketIndex=market.market_id,
            ClientOrderIndex=(int(nonce) + 1) % 2_000_000_000,
            BaseAmount=0,  # auto-size to executed entry amount
            Price=tp3_int,
            IsAsk=int(not is_ask),
            Type=self._signer.ORDER_TYPE_TAKE_PROFIT_LIMIT,
            TimeInForce=self._signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=1,
            TriggerPrice=tp3_int,
            OrderExpiry=-1,
        )

        sl_order = CreateOrderTxReq(
            MarketIndex=market.market_id,
            ClientOrderIndex=(int(nonce) + 2) % 2_000_000_000,
            BaseAmount=0,  # auto-size to executed entry amount
            Price=sl_int,
            IsAsk=int(not is_ask),
            Type=self._signer.ORDER_TYPE_STOP_LOSS_LIMIT,
            TimeInForce=self._signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=1,
            TriggerPrice=sl_int,
            OrderExpiry=-1,
        )

        _, grouped_hash, grouped_err = await self._signer.create_grouped_orders(
            grouping_type=self._signer.GROUPING_TYPE_ONE_TRIGGERS_A_ONE_CANCELS_THE_OTHER,
            orders=[entry_order, tp3_order, sl_order],
            nonce=nonce,
            api_key_index=api_key_index,
        )
        if grouped_err is not None:
            raise RuntimeError(str(grouped_err))

        return {
            "status": "placed",
            "market": market.symbol,
            "market_id": market.market_id,
            "applied_leverage": applied_leverage,
            "grouped_tx_hash": _extract_tx_hash(grouped_hash),
        }
