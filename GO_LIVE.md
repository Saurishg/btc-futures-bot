# Go-Live Procedure

**STOP.** Read this whole document before flipping to live. This is real money.

## Pre-flight checklist

- [ ] Bot has run on **testnet for ≥14 days** without unhandled errors
- [ ] You've witnessed at least **3 complete trade cycles** (open → SL or TP → close) on testnet
- [ ] Funding rate history checked — has shorting been net positive or negative in the last 30 days?
- [ ] You have **production Binance Futures API keys** with **only trading + read** permissions (NOT withdraw)
- [ ] API keys IP-whitelisted to this server's IP
- [ ] You have a tested kill-switch workflow: `touch .kill_switch` → confirm bot closes flat
- [ ] You understand: bot risks **0.5% of equity per trade**, capped at **$500 notional**, **2× leverage**

## How the safety rails work

| Setting | Default | Override via |
|---|---|---|
| Risk per trade | 0.5% of equity | `BOT_RISK_PCT=0.005` |
| Max position USD | $500 notional | `BOT_MAX_POS_USD=500` |
| Min balance to trade | $100 | `BOT_MIN_BALANCE=100` |
| Leverage | 2× | `BOT_LEVERAGE=2` |
| Daily loss halt | 3% (closes positions) | `cfg.DAILY_LOSS_HALT_PCT` |
| Max drawdown halt | 10% from peak | `cfg.MAX_DD_HALT_PCT` |
| Kill switch | `touch .kill_switch` | (no override — manual only) |

## Go-Live (Stage B — Tiny Live)

For the first **10 live trades**, hold these settings:

```bash
# In /home/work/btc_futures_15m/.env
BINANCE_FUT_ENV=live
BINANCE_FUT_API_KEY=...   # Production key from Binance
BINANCE_FUT_API_SECRET=...
BOT_LEVERAGE=2
BOT_RISK_PCT=0.005
BOT_MAX_POS_USD=500
BOT_MIN_BALANCE=100
```

Then:
```bash
pm2 restart btc-fut-15m
pm2 logs btc-fut-15m   # watch the startup banner — confirm ENV=LIVE
```

The startup banner will warn: `🚨 LIVE MODE — REAL MONEY 🚨`

## Emergency stop

```bash
touch /home/work/btc_futures_15m/.kill_switch
# bot's next cycle (within 2 min) closes any open position and halts
# To resume:
rm /home/work/btc_futures_15m/.kill_switch
```

## Stage C — Production Scale-Up

Only after Stage B produces **≥10 trades with PF ≥ 1.5**:

```bash
# Raise gradually, never above:
BOT_RISK_PCT=0.01      # 1%
BOT_MAX_POS_USD=2000   # $2k
BOT_LEVERAGE=3
```

## What I'm NOT doing

- **NOT auto-flipping to live.** That's the user's explicit decision.
- **NOT raising leverage or risk.** Conservative defaults until live data validates the backtest.
- **NOT skipping testnet validation.** 25 backtest trades is too few to bet real money on without paper-trading verification.

## Honest expectation

Based on 5y backtest: ~5 trades/year, ~$1.30 expected profit per $100 of equity per year. **This is a low-frequency, low-yield strategy.** It's positive-edge but it won't make you rich. If real-world execution (slippage, funding) eats 30% of the edge, you're at ~$0.91/year per $100. Plan accordingly.
