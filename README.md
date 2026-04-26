# ggshot → Lighter bot

Python bot that:
- Listens to messages in a Telegram channel (via Telethon)
- Parses trading signals (both formatted and “free-form”)
- Places Lighter orders using the official SDK: `elliottech/lighter-python`

## Setup

1) Install deps

```bash
python -m pip install -r requirements.txt
```

2) Create `.env`

Copy `.env.example` → `.env` and fill:
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`
- `TELEGRAM_CHANNEL`
- Lighter config path `LIGHTER_API_KEY_CONFIG`

3) Create `api_key_config.json`

The official SDK examples expect a json like:

```json
{
  "baseUrl": "https://mainnet.zklighter.elliot.ai",
  "accountIndex": 1,
  "privateKeys": {
    "0": "YOUR_API_PRIVATE_KEY_FOR_INDEX_0"
  }
}
```

4) Run

```bash
python -m ggshot_lighter_bot
```

## Trading rules implemented

- **Leverage**: default 3x, or 10x if accuracy ≥ 95%. If the requested leverage is rejected by the API, the bot retries with lower leverages.
- **Symbol not on Lighter**: skipped (based on `/api/v1/orderBooks` metadata).
- **Position size**: per signal notional is `available_balance * CAPITAL_ALLOCATION_PCT` (default 10%). If balance cannot be fetched, fallback to `TRADE_NOTIONAL_USD`.
- **Take profit**:
  - 75% at TP3, 25% at TP4
  - If TP4 missing → use TP3; if TP3 missing → use TP2; if TP2 missing → use TP1
  - TP1 and TP2 are otherwise ignored

## Notes

This project places:
- 1 entry limit order only when current market price is inside the entry range
- 2 reduce-only take-profit limit orders
- 1 reduce-only stop-loss trigger order (when supported by the SDK for the market)

Start with `DRY_RUN=true` to validate parsing and symbol matching before enabling live orders.
