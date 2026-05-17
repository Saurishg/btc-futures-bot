"""
Dual-Edge Breakout + Liquidity Sweep Strategy — 1H BTCUSDT Futures.

Breakout edges (5y validated):
  LONG:  4H bullish + ADX [30-50) + body ≥ 1.5×ATR + vol ≥ 1.2×MA20 + Donchian break
  SHORT: 4H bearish + ADX [45-65) + body ≥ 1.0×ATR + vol ≥ 2.0×MA20 + Donchian break

Liquidity sweep edges:
  SHORT: 4H bearish + wick pierces N-bar swing high + closes below it (stop hunt reversal)
  LONG:  4H bullish + wick pierces N-bar swing low  + closes above it (stop hunt reversal)
"""
from indicators import ema, rsi, atr, adx
import config as cfg


def htf_direction(htf_closes: list) -> str:
    if len(htf_closes) < cfg.HTF_EMA_SLOW: return 'NONE'
    ef = ema(htf_closes[-cfg.HTF_EMA_FAST*3:], cfg.HTF_EMA_FAST)
    es = ema(htf_closes, cfg.HTF_EMA_SLOW)
    if ef > es * 1.001: return 'LONG'
    if ef < es * 0.999: return 'SHORT'
    return 'NONE'


def _adx_rising(highs, lows, closes, n=14, lookback=5) -> bool:
    cur = adx(highs, lows, closes, n)
    if len(closes) < lookback + 30: return False
    prev = adx(highs[:-lookback], lows[:-lookback], closes[:-lookback], n)
    return cur > prev


def compute_signal(klines: list, htf_klines: list) -> dict:
    closes = [float(k[4]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    opens  = [float(k[1]) for k in klines]
    vols   = [float(k[5]) for k in klines]

    htf_closes = [float(k[4]) for k in htf_klines]
    direction  = htf_direction(htf_closes)

    price   = closes[-1]
    atr_val = atr(highs, lows, closes, cfg.ATR_PERIOD)
    adx_val = adx(highs, lows, closes, cfg.ADX_PERIOD)
    adx_up  = _adx_rising(highs, lows, closes, cfg.ADX_PERIOD, 5)
    rsi_val = rsi(closes, cfg.RSI_PERIOD)

    # Donchian channel (excl current bar)
    donchian_high = max(highs[-cfg.BREAKOUT_PERIOD-1:-1])
    donchian_low  = min(lows[-cfg.BREAKOUT_PERIOD-1:-1])

    # Candle body in ATR units
    body = abs(closes[-1] - opens[-1])
    body_atr = body / atr_val if atr_val > 0 else 0
    bull_candle = closes[-1] > opens[-1]
    bear_candle = closes[-1] < opens[-1]

    # Volume ratio (20-bar MA of historical volumes, excluding current)
    if len(vols) >= 21:
        vol_ma = sum(vols[-21:-1]) / 20
    else:
        vol_ma = sum(vols[:-1]) / max(1, len(vols) - 1) if len(vols) > 1 else vols[-1]
    vol_ratio = vols[-1] / vol_ma if vol_ma > 0 else 1

    # ── LONG edge: moderate ADX + huge candle ────────────────────────
    # Hard-gated off by SHORTS_ONLY flag — backtest showed longs unprofitable
    long_signal = (
        not getattr(cfg, 'SHORTS_ONLY', False)
        and direction == 'LONG'
        and closes[-1] > donchian_high
        and bull_candle
        and cfg.LONG_ADX_MIN <= adx_val < cfg.LONG_ADX_MAX
        and adx_up
        and body_atr >= cfg.LONG_BODY_ATR_MIN
        and vol_ratio >= cfg.LONG_VOL_MULT
    )

    # ── SHORT edge: strong ADX + high volume ─────────────────────────
    short_signal = (
        direction == 'SHORT'
        and closes[-1] < donchian_low
        and bear_candle
        and cfg.SHORT_ADX_MIN <= adx_val < cfg.SHORT_ADX_MAX
        and adx_up
        and body_atr >= cfg.SHORT_BODY_ATR_MIN
        and vol_ratio >= cfg.SHORT_VOL_MULT
    )

    return {
        'price': price, 'atr': atr_val,
        'rsi': rsi_val, 'adx': adx_val, 'adx_rising': adx_up,
        'direction': direction,
        'donchian_high': donchian_high, 'donchian_low': donchian_low,
        'body_atr': body_atr, 'vol_ratio': vol_ratio,
        'long_signal': long_signal, 'short_signal': short_signal,
    }


def compute_signal_sym(klines: list, htf_klines: list, sym_params: dict) -> dict:
    """Per-symbol variant of compute_signal using sym_params for filter thresholds."""
    closes = [float(k[4]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    opens  = [float(k[1]) for k in klines]
    vols   = [float(k[5]) for k in klines]

    htf_closes = [float(k[4]) for k in htf_klines]
    direction  = htf_direction(htf_closes)

    price   = closes[-1]
    atr_val = atr(highs, lows, closes, cfg.ATR_PERIOD)
    adx_val = adx(highs, lows, closes, cfg.ADX_PERIOD)
    adx_up  = _adx_rising(highs, lows, closes, cfg.ADX_PERIOD, 5)
    rsi_val = rsi(closes, cfg.RSI_PERIOD)

    donchian_high = max(highs[-cfg.BREAKOUT_PERIOD-1:-1])
    donchian_low  = min(lows[-cfg.BREAKOUT_PERIOD-1:-1])

    body = abs(closes[-1] - opens[-1])
    body_atr = body / atr_val if atr_val > 0 else 0
    bull_candle = closes[-1] > opens[-1]
    bear_candle = closes[-1] < opens[-1]

    if len(vols) >= 21:
        vol_ma = sum(vols[-21:-1]) / 20
    else:
        vol_ma = sum(vols[:-1]) / max(1, len(vols) - 1) if len(vols) > 1 else vols[-1]
    vol_ratio = vols[-1] / vol_ma if vol_ma > 0 else 1

    shorts_only = sym_params.get('shorts_only', True)

    long_signal = (
        not shorts_only
        and direction == 'LONG'
        and closes[-1] > donchian_high
        and bull_candle
        and sym_params['long_adx_min'] <= adx_val < sym_params['long_adx_max']
        and adx_up
        and body_atr >= sym_params['long_body_atr_min']
        and vol_ratio >= sym_params['long_vol_mult']
    )

    short_signal = (
        direction == 'SHORT'
        and closes[-1] < donchian_low
        and bear_candle
        and sym_params['short_adx_min'] <= adx_val < sym_params['short_adx_max']
        and adx_up
        and body_atr >= sym_params['short_body_atr_min']
        and vol_ratio >= sym_params['short_vol_mult']
    )

    return {
        'price': price, 'atr': atr_val,
        'rsi': rsi_val, 'adx': adx_val, 'adx_rising': adx_up,
        'direction': direction,
        'donchian_high': donchian_high, 'donchian_low': donchian_low,
        'body_atr': body_atr, 'vol_ratio': vol_ratio,
        'long_signal': long_signal, 'short_signal': short_signal,
    }


def liquidity_sweep(
    highs: list, lows: list, closes: list, opens: list,
    atr_val: float, direction: str, params: dict,
) -> tuple:
    """
    Detect a stop-hunt candle: price wicks beyond a swing level then rejects.

    Bearish sweep (SHORT): wick pierces N-bar swing high, candle closes below it.
    Bullish sweep (LONG):  wick pierces N-bar swing low,  candle closes above it.

    Returns (sweep_long, sweep_short, swing_high_used, swing_low_used).
    SL for a sweep entry sits just beyond the wick (tighter than fixed ATR SL).
    """
    n        = params.get('sweep_lookback', 20)
    wick_min = params.get('sweep_wick_atr_min', 0.25)

    if len(highs) < n + 2 or atr_val <= 0:
        return False, False, 0.0, 0.0

    swing_high = max(highs[-n - 1:-1])
    swing_low  = min(lows[-n - 1:-1])

    cur_high  = highs[-1]
    cur_low   = lows[-1]
    cur_close = closes[-1]
    cur_open  = opens[-1]

    # How far the wick pierced beyond the swing level
    above_wick = cur_high - swing_high  # positive → wicked above swing high
    below_wick = swing_low - cur_low    # positive → wicked below swing low

    sweep_short = (
        direction == 'SHORT'
        and above_wick >= wick_min * atr_val   # meaningful wick above swing high
        and cur_close < swing_high              # rejection: closed back below level
        and cur_close < cur_open                # bearish body confirms reversal
    )

    sweep_long = (
        direction == 'LONG'
        and below_wick >= wick_min * atr_val   # meaningful wick below swing low
        and cur_close > swing_low              # rejection: closed back above level
        and cur_close > cur_open               # bullish body confirms reversal
    )

    return sweep_long, sweep_short, swing_high, swing_low


def chandelier_stop(highs: list, lows: list, atr_val: float, side: str) -> float:
    n = cfg.CHANDELIER_PERIOD
    if side == 'LONG':
        return max(highs[-n:]) - cfg.CHANDELIER_ATR_MULT * atr_val
    return min(lows[-n:]) + cfg.CHANDELIER_ATR_MULT * atr_val


def compute_qty(equity: float, price: float, atr_val: float, sl_mult: float = None) -> float:
    if sl_mult is None:
        sl_mult = cfg.ATR_SL_MULT
    sl_distance = sl_mult * atr_val
    if sl_distance <= 0: return 0.0
    qty_risk = (equity * cfg.RISK_PCT) / sl_distance
    qty_cap  = (equity * cfg.MAX_POSITION_PCT) / price
    return round(min(qty_risk, qty_cap), 3)
