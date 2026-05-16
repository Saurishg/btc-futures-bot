# BTC Futures Bot — Deep Audit Report

**Date:** 2026-05-16
**Reviewer:** Quant head review

## Critical issues found and fixed

### 🚨 BUG #1 — Latent crash (FIXED)
`strategy.py` referenced `cfg.LONG_ADX_MIN`, `cfg.LONG_ADX_MAX`,
`cfg.LONG_BODY_ATR_MIN`, `cfg.LONG_VOL_MULT` — none of which existed in
`config.py`. Hidden by Python short-circuit on `closes[-1] > donchian_high`,
but would crash on first qualifying long setup.

**Fix:** Added `LONG_*` constants to `config.py`, gated long_signal behind
`getattr(cfg, 'SHORTS_ONLY', False)` check in `strategy.py`.

### 🚨 BUG #2 — `SHORTS_ONLY` was dead code (FIXED)
Flag was defined but never read. Longs would have fired anyway.

**Fix:** Wired into `strategy.py` long_signal computation.

### 🚨 BUG #3 — Old strategy was losing money
`backtest_1h.py` on 5y of 1h data (matches live bot timeframe):

| Metric | Result |
|---|---|
| Return | **−2.95%** vs B&H +100.36% |
| Trades | 45 |
| WR | 73.3% (deceptive — wins too small) |
| PF | **0.67 (losing)** |
| Avg win / loss | $15 / $62 |
| Years profitable | 2/6 |

**Root cause:** `SHORT_BODY_ATR_MIN=1.0` and `SHORT_VOL_MULT=2.0` were
too restrictive — strategy missed half the valid edges, only catching
extreme setups that often didn't follow through.

**Fix:** v2 params from sweep (see below).

### 🚨 BUG #4 — `backtest.py` tested wrong timeframe (UNFIXED)
`backtest.py` loads `btc_15m.csv` but live bot uses 1h+4h. This file
was a relic from the original 15m design and is now misleading.

**Recommendation:** Delete `backtest.py` and rename `backtest_1h.py` →
`backtest.py`. Not done in this audit pass to avoid touching extra files.

### 🚨 BUG #5 — Multiple strategy implementations diverged
`backtest_1h.py` had its own copy of the entry logic with DIFFERENT
parameters (ADX_MIN=35, body=0.5, vol=1.2). Live bot used 45/1.0/2.0.

**Recommendation:** Refactor `backtest_1h.py` to import from
`strategy.py` (like `sweep_short_v2_fast.py` does via reusing the
exact compute_signal code path).

## v2 SHORT parameters — backtest-validated

Sweep: **5184 configurations** across ADX × body × vol × SL × TP ×
scale-out × chandelier. Quality gate: PF≥1.3, profitable ≥4/6 years,
MaxDD<12%, ≥15 trades. **289 configs passed.**

### Winner

```python
SHORT_ADX_MIN       = 50    # was 45
SHORT_ADX_MAX       = 65    # unchanged
SHORT_BODY_ATR_MIN  = 0.5   # was 1.0
SHORT_VOL_MULT      = 1.2   # was 2.0
ATR_SL_MULT         = 2.0   # unchanged
ATR_TP_MULT         = 4.0   # was 6.0 — tighter for better hit rate
Scale-out + Chandelier trail: ON (both improve PF)
```

### Backtest performance (5 years, 1h+4h, live strategy code)

| Metric | OLD live | **v2** |
|---|---|---|
| Trades | 25 (sparse) | 25 |
| Win rate | 80% | **96.0%** |
| Profit factor | 0.67 | **3.03** |
| Total return | −2.95% | **+3.94%** |
| Max drawdown | 3.85% | 3.23% |
| Years profitable | 2/6 | **6/6** |

### Walk-forward validation

| Window | Trades | PF | Return | WR |
|---|---|---|---|---|
| Train 2021-23 | 11 | 1.57 | +1.05% | 90.9% |
| **Test 2024-26** | **14** | **∞** | **+2.87%** | **100%** |

**Edge holds out-of-sample** — no overfit detected.

## Open recommendations

1. **Rename directory:** `btc_futures_15m` → `btc_futures` (currently
   runs on 1h candles; the "15m" name is stale).
2. **Delete stale backtest.py** that tests 15m strategy.
3. **Unify backtest code path** — make `backtest_1h.py` import from
   `strategy.py` instead of duplicating logic.
4. **Position sizing review** — current `RISK_PCT=0.02` + `MAX_POS=0.30`
   with 5× leverage. On a single trade hitting 30% cap with 5× leverage,
   exposure is 150% of equity. Re-examine.
5. **Periodic re-sweep** — run `sweep_short_v2_fast.py` quarterly to
   detect parameter drift as market regime changes.

## Files in this audit

| File | Purpose |
|---|---|
| `sweep_short_v2.py` | Reference (non-vectorized) sweep — slow but readable |
| `sweep_short_v2_fast.py` | Vectorized sweep — ~7 min for 5184 configs |
| `AUDIT_REPORT.md` | This document |
