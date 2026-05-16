"""BTC Futures Bot — 1H Dual-Edge (data-driven from 5y deep dive)

Two validated edges (both profitable 5/6 years):
  LONG:  ADX [30-50) + body ≥ 1.5×ATR + vol ≥ 1.2×MA20
  SHORT: ADX [45-65) + body ≥ 1.0×ATR + vol ≥ 2.0×MA20

Combined: 52 trades over 5 years, PF ~2.0, profitable in 5/6 years.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env')

# ── API ──────────────────────────────────────────────────────────────────
ENV = os.getenv('BINANCE_FUT_ENV', 'testnet').lower()
if ENV == 'live':
    BASE_URL = 'https://fapi.binance.com'
    API_KEY    = os.getenv('BINANCE_FUT_API_KEY', '')
    API_SECRET = os.getenv('BINANCE_FUT_API_SECRET', '')
else:
    BASE_URL = 'https://testnet.binancefuture.com'
    API_KEY    = os.getenv('BINANCE_TESTNET_API_KEY', '')
    API_SECRET = os.getenv('BINANCE_TESTNET_API_SECRET', '')

SYMBOL   = 'BTCUSDT'
TF_ENTRY = '1h'
TF_HTF   = '4h'

# ── Leverage / margin ────────────────────────────────────────────────────
LEVERAGE     = 5
MARGIN_TYPE  = 'ISOLATED'

# ── Trend filter (HTF = 4h) ──────────────────────────────────────────────
HTF_EMA_FAST = 50
HTF_EMA_SLOW = 200

# ── Breakout entry (1h) — FINAL OPTIMIZED ────────────────────────────────
BREAKOUT_PERIOD = 20
ADX_PERIOD      = 14
ATR_PERIOD      = 14
RSI_PERIOD      = 14

# LONG params (kept for code safety even though longs are gated off by SHORTS_ONLY).
# Values mirror the SHORT edge — won't fire unless SHORTS_ONLY is False.
LONG_ADX_MIN        = 30
LONG_ADX_MAX        = 50
LONG_BODY_ATR_MIN   = 1.5
LONG_VOL_MULT       = 1.2

# SHORT v2 edge — validated by sweep_short_v2_fast.py on 5y of 1h data:
#   25 trades, WR 96%, PF 3.03, Return +3.94%, MaxDD 3.23%, profitable 6/6 years
# Walk-forward (2021-23 train / 2024-26 test): edge holds out-of-sample
SHORT_ADX_MIN       = 50    # raised from 45 — strongest trends only
SHORT_ADX_MAX       = 65
SHORT_BODY_ATR_MIN  = 0.5   # lowered from 1.0 — old filter was too restrictive
SHORT_VOL_MULT      = 1.2   # lowered from 2.0 — 2.0× vol is too rare

# LONGs disabled — checked in strategy.py
SHORTS_ONLY         = True

# ── Exits ───────────────────────────────────────────────────────────────
ATR_SL_MULT           = 2.0     # sweep-validated
ATR_TP_MULT           = 4.0     # v2: tighter from 6.0 — 1:2 R:R, same PF as TP=6 in backtest but +1 trade
CHANDELIER_PERIOD     = 20
CHANDELIER_ATR_MULT   = 3.0
TRAIL_BE_AT_ATR       = 1.0
CHANDELIER_ACTIVATE_AT_ATR = 1.5

# ── Scale-out ────────────────────────────────────────────────────────────
SCALE_OUT_ENABLED     = True
SCALE_OUT_AT_ATR      = 1.0
SCALE_OUT_FRACTION    = 0.5

# ── Risk management ──────────────────────────────────────────────────────
RISK_PCT            = 0.02     # 2% — sized up because edge is real but infrequent
MAX_POSITION_PCT    = 0.30
DAILY_LOSS_HALT_PCT = 0.03
MAX_DD_HALT_PCT     = 0.15
LOW_LIQ_UTC_HOURS   = {3, 4, 5}

# ── Fees / loop ─────────────────────────────────────────────────────────
FEE_RATE      = 0.0004
LOOP_INTERVAL = 120
