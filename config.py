"""BTC Futures Bot — 1H DUAL EDGE (v6, 6m adaptive re-tune 2026-05-17)

5y validated anchors (SHORT):
  ADX [45-70) + body ≥ 0.6×ATR + vol ≥ 1.2×MA20 | 45t, WR 86.7%, PF 1.97

6m adaptive blend (20% weight, 8 trades, WR 100%):
  6m winner: ADX [40-60) body≥0.8 vol≥1.0 SL×1.5 TP×4.0
  Blended:   ADX [44-68) body≥0.64 vol≥1.16 SL×1.9 TP×4.0

Validated LONG edge — 5y backtest:
  ADX [40-55) body ≥ 1.5×ATR | 20t, WR 80%, PF 2.13, 4/4 years

Combined frequency: ~13 trades/yr
Adapt window: 6 months (weekly re-tune)
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
LEVERAGE     = int(os.getenv('BOT_LEVERAGE', '2'))   # default 2× for live safety
MARGIN_TYPE  = 'ISOLATED'

# ── Safety guards ────────────────────────────────────────────────────────
# Hard USD cap on a single position — overrides MAX_POSITION_PCT
MAX_POSITION_USD = float(os.getenv('BOT_MAX_POS_USD', '500'))

# Minimum balance required to trade
MIN_BALANCE_USD  = float(os.getenv('BOT_MIN_BALANCE', '100'))

# Kill switch — if this file exists, bot closes positions and stops
KILL_SWITCH_FILE = '.kill_switch'

# ── Trend filter (HTF = 4h) ──────────────────────────────────────────────
HTF_EMA_FAST = 50
HTF_EMA_SLOW = 200

# ── Breakout entry (1h) — FINAL OPTIMIZED ────────────────────────────────
BREAKOUT_PERIOD = 20
ADX_PERIOD      = 14
ATR_PERIOD      = 14
RSI_PERIOD      = 14

# LONG v5 edge — validated 2026-05-17 freq sweep on 5y 1h+4h data:
#   20 trades, WR 80.0%, PF 2.13, Return +2.3%, profitable 4/4 years
LONG_ADX_MIN        = 40    # validated optimal
LONG_ADX_MAX        = 55    # raised from 50 — 4 more trades, still 4/4 years
LONG_BODY_ATR_MIN   = 1.5   # decisive candle required — lower hurts quality fast
LONG_VOL_MULT       = 1.2

# SHORT v6 — 5y anchor blended with 6m adaptive re-tune (2026-05-17):
#   5y: ADX[45-70) body≥0.6 vol≥1.2 | 6m winner: ADX[40-60) body≥0.8 vol≥1.0 SL×1.5
#   Blended at 20% window / 80% anchor (8 trades, low confidence)
SHORT_ADX_MIN       = 44    # blended: 45×0.8 + 40×0.2
SHORT_ADX_MAX       = 68    # blended: 70×0.8 + 60×0.2
SHORT_BODY_ATR_MIN  = 0.64  # blended: 0.6×0.8 + 0.8×0.2
SHORT_VOL_MULT      = 1.16  # blended: 1.2×0.8 + 1.0×0.2

SHORTS_ONLY         = False  # Dual-edge enabled — LONG edge validated 2026-05-17

# ── Exits — side-specific SL/TP multipliers ──────────────────────────────
# SHORT: tight TP captures breakout momentum fast (validated on 5y backtest)
ATR_SL_MULT           = 1.9     # SHORT SL — blended: 2.0×0.8 + 1.5×0.2
ATR_TP_MULT           = 4.0     # SHORT TP — unchanged
# LONG: wider TP captures extended trending moves (validated on 5y backtest)
LONG_ATR_SL_MULT      = 2.0     # LONG stop loss
LONG_ATR_TP_MULT      = 6.0     # LONG take profit — wider R:R validated
CHANDELIER_PERIOD     = 20
CHANDELIER_ATR_MULT   = 3.0
TRAIL_BE_AT_ATR       = 1.0
CHANDELIER_ACTIVATE_AT_ATR = 1.5

# ── Liquidity sweep edge ─────────────────────────────────────────────────
# Detects stop-hunt candles: price wicks beyond N-bar swing high/low then rejects.
# SHORT sweep: wick above swing high + bearish close below it = reversal short.
# LONG  sweep: wick below swing low  + bullish close above it = reversal long.
SWEEP_LOOKBACK      = 20    # bars to look back for swing high/low
SWEEP_WICK_ATR_MIN  = 0.25  # wick must pierce at least 0.25×ATR beyond swing level

# ── Scale-out ────────────────────────────────────────────────────────────
SCALE_OUT_ENABLED     = True
SCALE_OUT_AT_ATR      = 1.0
SCALE_OUT_FRACTION    = 0.5

# ── Risk management ──────────────────────────────────────────────────────
RISK_PCT            = float(os.getenv('BOT_RISK_PCT', '0.005'))   # 0.5% default for live safety
MAX_POSITION_PCT    = 0.30
DAILY_LOSS_HALT_PCT = 0.03
MAX_DD_HALT_PCT     = 0.10     # tightened from 0.15
LOW_LIQ_UTC_HOURS   = {3, 4, 5, 6}  # UTC 6 added — Asian session fully blocked (confirmed bad signals)

# ── Fees / loop ─────────────────────────────────────────────────────────
FEE_RATE      = 0.0004
LOOP_INTERVAL = 120

# ── Parameter learning ────────────────────────────────────────────────────
ADAPT_ENABLED          = True
ADAPT_WINDOW_MONTHS    = 6    # 6-month rolling window — re-tunes weekly
ADAPT_MIN_TRADES       = 5    # skip adaptation if window produces < N trades
ADAPT_INTERVAL_DAYS    = 7    # re-run adaptation every N days

# ── Multi-symbol config ───────────────────────────────────────────────────
SYMBOLS = {
    'BTCUSDT': {
        'enabled': True,
        'shorts_only': False,
        # SHORT (refined 2026-05-28 after current-market study): ADX[40-70) body>=0.6.
        # 5y validated: 53 trades, WR 83%, PF 1.84, +5.2% return, 4/6 profitable yrs —
        # the optimal ADX band; [43-70) gave PF 1.55 / +3.1%. Current regime is LONG-
        # dominant with ADX p90~41, so the [40,43) zone matters for catching the
        # marginal SHORT setups when regime shifts back.
        'short_adx_min': 40,   'short_adx_max': 70,
        'short_body_atr_min': 0.6, 'short_vol_mult': 1.16,
        'short_sl_mult': 1.9,  'short_tp_mult': 4.0,
        'long_adx_min': 40,    'long_adx_max': 55,
        'long_body_atr_min': 1.5, 'long_vol_mult': 1.2,
        'long_sl_mult': 2.0,   'long_tp_mult': 6.0,
    },
    'ETHUSDT': {
        'enabled': True,
        'shorts_only': True,
        'short_adx_min': 45,   'short_adx_max': 55,
        'short_body_atr_min': 0.5, 'short_vol_mult': 1.0,
        'short_sl_mult': 1.5,  'short_tp_mult': 3.0,
    },
    'SOLUSDT': {
        'enabled': True,
        'shorts_only': True,
        'short_adx_min': 50,   'short_adx_max': 55,
        'short_body_atr_min': 0.8, 'short_vol_mult': 1.0,
        'short_sl_mult': 2.0,  'short_tp_mult': 4.0,
    },
}
MAX_CONCURRENT_POSITIONS = 2
