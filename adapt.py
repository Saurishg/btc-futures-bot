"""
Parameter adaptation — rolls a mini-sweep over the last N months of backtest
data and blends the winner with the 5-year-validated anchors to produce
learned_params.json. Applied to the live cfg module at startup and every
ADAPT_INTERVAL_DAYS (triggered by bot.run_loop, runs in a daemon thread).

Blending logic:
  - ≥15 backtest trades in window → 50% trust in window winner
  - 10-14 trades                  → 30% trust
  -  5-9  trades                  → 20% trust
  -  <5   trades                  → no adaptation, stay on anchors

ADX_MIN < ADX_MAX is always enforced before writing.
"""

import os, json, logging, threading, time
from datetime import datetime, timedelta
from pathlib import Path
from itertools import product

import pandas as pd
import numpy as np

import config as cfg

log = logging.getLogger('bot.adapt')

BASE_DIR            = Path(__file__).parent
DATA_DIR            = BASE_DIR / 'data'
LEARNED_PARAMS_FILE = BASE_DIR / 'learned_params.json'
PNL_FILE            = BASE_DIR / 'pnl.json'

_config_lock = threading.Lock()

ADAPTABLE_PARAMS = [
    'SHORT_ADX_MIN', 'SHORT_ADX_MAX',
    'SHORT_BODY_ATR_MIN', 'SHORT_VOL_MULT',
    'ATR_SL_MULT', 'ATR_TP_MULT',
]

# v6 anchors — 5y sweep blended with 6m adaptive re-tune (2026-05-17)
# SHORT: 5y[45-70)body≥0.6 × 0.8 + 6m[40-60)body≥0.8 × 0.2
ANCHOR_PARAMS = {
    'SHORT_ADX_MIN':      44,
    'SHORT_ADX_MAX':      68,
    'SHORT_BODY_ATR_MIN': 0.64,
    'SHORT_VOL_MULT':     1.16,
    'ATR_SL_MULT':        1.9,
    'ATR_TP_MULT':        4.0,
}

# LONG anchors — fixed (not adapted by the SHORT-only mini-sweep)
LONG_ANCHOR_PARAMS = {
    'LONG_ADX_MIN':      40,
    'LONG_ADX_MAX':      55,
    'LONG_BODY_ATR_MIN': 1.5,
    'LONG_VOL_MULT':     1.2,
    'LONG_ATR_SL_MULT':  2.0,
    'LONG_ATR_TP_MULT':  6.0,
}

# A change smaller than this threshold is not considered meaningful
_CHANGE_THRESHOLDS = {
    'SHORT_ADX_MIN':      1.0,
    'SHORT_ADX_MAX':      1.0,
    'SHORT_BODY_ATR_MIN': 0.05,
    'SHORT_VOL_MULT':     0.05,
    'ATR_SL_MULT':        0.10,
    'ATR_TP_MULT':        0.20,
}

# Mini-sweep grid — centred on v5 anchors (ADX[45-70), body≥0.6)
MINI_GRID = {
    'SHORT_ADX_MIN':      [40, 43, 45, 48, 50],
    'SHORT_ADX_MAX':      [60, 65, 70, 75, 80],
    'SHORT_BODY_ATR_MIN': [0.3, 0.5, 0.6, 0.8, 1.0],
    'SHORT_VOL_MULT':     [1.0, 1.2, 1.5, 2.0],
    'ATR_SL_MULT':        [1.5, 2.0, 2.5],
    'ATR_TP_MULT':        [3.0, 4.0, 6.0],
}
MINI_EXTRAS = [
    {'use_scale_out': True, 'use_chandelier': True},
    {'use_scale_out': True, 'use_chandelier': False},
]

WARMUP = 250  # bars burned before backtest counts — mirrors sweep_short_v2_fast.py

# ── Indicator helpers (mirrors sweep_short_v2_fast.py) ───────────────────

def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def _atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def _rsi(c, n=14):
    diff = c.diff()
    gain = diff.clip(lower=0); loss = -diff.clip(upper=0)
    ag = gain.rolling(n).mean(); al = loss.rolling(n).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)

def _adx(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    up = h - h.shift(1); dn = l.shift(1) - l
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=h.index)
    atr_s = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi_s = pdm.ewm(alpha=1 / n, adjust=False).mean()
    ndi_s = ndm.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pdi_s / atr_s
    ndi = 100 * ndi_s / atr_s
    dx = (100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)).fillna(0)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


# ── Data loading ─────────────────────────────────────────────────────────

def load_window_df(window_months: int) -> pd.DataFrame:
    """Load 1h CSV, merge 4h direction, compute indicators, slice to window."""
    f1h = DATA_DIR / 'btc_1h_5y.csv'
    f4h = DATA_DIR / 'btc_4h_5y.csv'
    if not f1h.exists() or not f4h.exists():
        raise FileNotFoundError(f'Missing CSV data in {DATA_DIR}. Run fetch_data.py first.')

    df1h = pd.read_csv(f1h, parse_dates=['timestamp']).set_index('timestamp')
    df4h = pd.read_csv(f4h, parse_dates=['timestamp']).set_index('timestamp')

    data_age_days = (datetime.now() - df1h.index.max().to_pydatetime().replace(tzinfo=None)).days
    if data_age_days > 2:
        log.warning(f'1h CSV is {data_age_days} days old — run fetch_data.py to refresh')

    # 4h trend direction
    ef = _ema(df4h['close'], 50)
    es = _ema(df4h['close'], 200)
    df4h['direction'] = np.where(ef > es * 1.001, 'LONG',
                          np.where(ef < es * 0.999, 'SHORT', 'NONE'))

    df1h = df1h.sort_index(); df4h = df4h.sort_index()
    df1h['direction'] = pd.merge_asof(
        df1h.reset_index(), df4h[['direction']].reset_index(),
        on='timestamp', direction='backward'
    )['direction'].values

    # 1h indicators
    df1h['atr']      = _atr(df1h['high'], df1h['low'], df1h['close'], 14)
    df1h['adx']      = _adx(df1h['high'], df1h['low'], df1h['close'], 14)
    df1h['adx_up']   = df1h['adx'] > df1h['adx'].shift(5)
    df1h['donch_hi'] = df1h['high'].shift(1).rolling(20).max()
    df1h['donch_lo'] = df1h['low'].shift(1).rolling(20).min()
    body             = (df1h['close'] - df1h['open']).abs()
    df1h['body_atr'] = body / df1h['atr']
    df1h['bear_candle'] = df1h['close'] < df1h['open']
    df1h['vol_ma20']    = df1h['volume'].shift(1).rolling(20).mean()
    df1h['vol_ratio']   = df1h['volume'] / df1h['vol_ma20']
    df1h['low_20']      = df1h['low'].rolling(20).min()
    df1h['high_20']     = df1h['high'].rolling(20).max()
    df1h['year']        = df1h.index.year
    df1h = df1h.dropna().copy()

    # Slice to rolling window (keep WARMUP extra bars before the cutoff)
    cutoff = df1h.index.max() - pd.DateOffset(months=window_months)
    cutoff_pos = int(df1h.index.searchsorted(cutoff))
    start_pos = max(0, cutoff_pos - WARMUP)
    return df1h.iloc[start_pos:].copy()


# ── Mini backtest (mirrors sweep_short_v2_fast.backtest_short) ───────────

def _backtest_short(df: pd.DataFrame, params: dict) -> dict:
    """Tight-loop backtest of short-only strategy on pre-computed indicator df."""
    from collections import defaultdict

    adx_min  = params['SHORT_ADX_MIN'];  adx_max = params['SHORT_ADX_MAX']
    body_min = params['SHORT_BODY_ATR_MIN']
    vol_mult = params['SHORT_VOL_MULT']
    sl_m     = params['ATR_SL_MULT'];    tp_m    = params['ATR_TP_MULT']
    use_scale = params.get('use_scale_out', True)
    use_chand = params.get('use_chandelier', True)

    FEE_RATE   = 0.0004
    RISK_PCT   = 0.02
    MAX_POS    = 0.30
    INITIAL    = 10_000.0
    SCALE_OUT_AT_ATR      = 1.0
    SCALE_OUT_FRACTION    = 0.5
    CHANDELIER_ATR_MULT   = 3.0
    CHANDELIER_ACTIVATE   = 1.5
    TRAIL_BE_AT_ATR       = 1.0

    entry_ok = (
        (df['direction'] == 'SHORT').values
        & (df['close'].values < df['donch_lo'].values)
        & df['bear_candle'].values
        & (df['adx'].values >= adx_min) & (df['adx'].values < adx_max)
        & df['adx_up'].values
        & (df['body_atr'].values >= body_min)
        & (df['vol_ratio'].values >= vol_mult)
    )

    closes = df['close'].values; highs = df['high'].values; lows = df['low'].values
    atrs   = df['atr'].values;   low20 = df['low_20'].values; years = df['year'].values

    cash = INITIAL; peak = INITIAL
    in_pos = False; entry_p = sl = tp = qty = 0.0
    entry_fee = 0.0; scaled = False; scale_pnl = 0.0
    trades = []; eq_curve = []
    n = len(df)

    for i in range(n):
        if i < WARMUP: continue
        price = closes[i]; h = highs[i]; l = lows[i]; a = atrs[i]
        if not np.isfinite(a) or a <= 0:
            eq_curve.append(cash); continue

        equity = cash + qty * (entry_p - price) if in_pos else cash
        peak = max(peak, equity)
        eq_curve.append(equity)

        if in_pos:
            profit_atr_max = (entry_p - l) / a
            profit_atr_cls = (entry_p - price) / a

            if use_scale and not scaled and profit_atr_max >= SCALE_OUT_AT_ATR:
                sp = entry_p - SCALE_OUT_AT_ATR * a
                sq = qty * SCALE_OUT_FRACTION
                pnl_s = (entry_p - sp) * sq - FEE_RATE * sp * sq - entry_fee * SCALE_OUT_FRACTION
                cash += sq * entry_p + pnl_s; qty -= sq
                scaled = True; scale_pnl = pnl_s; sl = entry_p

            if use_chand and profit_atr_cls >= CHANDELIER_ACTIVATE:
                chand = low20[i] + CHANDELIER_ATR_MULT * a
                sl = min(sl, chand) if sl > 0 else chand

            if (entry_p - l) >= TRAIL_BE_AT_ATR * a:
                sl = min(sl, entry_p) if sl > 0 else entry_p

            exit_p = reason = None
            if h >= sl:   exit_p, reason = sl, 'SL'
            elif l <= tp: exit_p, reason = tp, 'TP'

            if exit_p is not None:
                rem_fee = entry_fee * (1 - SCALE_OUT_FRACTION) if scaled else entry_fee
                pnl_g = (entry_p - exit_p) * qty
                fee_e = FEE_RATE * exit_p * qty
                pnl_n = pnl_g - fee_e - rem_fee + scale_pnl
                cash += qty * entry_p + (pnl_g - fee_e - rem_fee)
                trades.append({'year': years[i], 'pnl': pnl_n, 'reason': reason})
                in_pos = False; qty = 0.0; entry_p = sl = tp = 0.0
                entry_fee = 0.0; scaled = False; scale_pnl = 0.0
            continue

        if not entry_ok[i] or equity <= 100: continue
        sl_dist = sl_m * a
        if sl_dist <= 0: continue
        new_qty = round(min((equity * RISK_PCT) / sl_dist, (equity * MAX_POS) / price), 3)
        if new_qty < 0.001: continue
        cost = new_qty * price; fee = FEE_RATE * cost
        if cost + fee > cash: continue

        cash -= cost + fee; in_pos = True
        entry_p = price; qty = new_qty
        sl = price + sl_m * a; tp = price - tp_m * a
        entry_fee = fee; scaled = False; scale_pnl = 0.0

    if in_pos:
        last_p = closes[-1]
        pnl_g = (entry_p - last_p) * qty
        fee_e = FEE_RATE * last_p * qty
        rem_fee = entry_fee * (1 - SCALE_OUT_FRACTION) if scaled else entry_fee
        pnl_n = pnl_g - fee_e - rem_fee + scale_pnl
        cash += qty * entry_p + (pnl_g - fee_e - rem_fee)
        trades.append({'year': years[-1], 'pnl': pnl_n, 'reason': 'EOD'})

    n_tr = len(trades)
    if n_tr == 0:
        return {'n': 0, 'ret': 0, 'pf': 0, 'wr': 0, 'dd': 0}
    wins   = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    gw = sum(t['pnl'] for t in wins); gl = abs(sum(t['pnl'] for t in losses))
    pf = gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0)
    eq = np.array(eq_curve)
    dd = (eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)
    mdd = abs(dd.min() * 100) if len(dd) else 0
    return {
        'n':   n_tr,
        'ret': (cash / INITIAL - 1) * 100,
        'pf':  pf,
        'wr':  len(wins) / n_tr * 100,
        'dd':  mdd,
    }


# ── Sweep ─────────────────────────────────────────────────────────────────

def run_mini_sweep(df: pd.DataFrame) -> list:
    keys = list(MINI_GRID.keys())
    combos = [c for c in product(*[MINI_GRID[k] for k in keys]) if c[0] < c[1]]
    results = []
    for vals in combos:
        params = dict(zip(keys, vals))
        for ext in MINI_EXTRAS:
            full = {**params, **ext}
            r = _backtest_short(df, full)
            r.update(full)
            results.append(r)
    return results


# ── Quality gate ──────────────────────────────────────────────────────────

def quality_gate(r: dict, min_trades: int) -> tuple:
    if r['n'] < min_trades:
        return False, f'too_few_trades ({r["n"]} < {min_trades})'
    if r['pf'] < 1.0:
        return False, f'unprofitable (pf={r["pf"]:.2f})'
    # pf=99 sentinel only degenerate if we have fewer than min_trades — otherwise it's a real win streak
    if r['pf'] >= 99 and r['n'] < min_trades:
        return False, f'degenerate_pf (n={r["n"]}, pf={r["pf"]:.0f})'
    # 100% WR on very tiny sample is a red flag, but allow it for rolling windows (n can be low)
    if r['wr'] >= 99.0 and r['n'] < 4:
        return False, f'degenerate_wr (n={r["n"]}, wr={r["wr"]:.0f}%)'
    # 35% max DD — rolling 12m windows accumulate DD faster than 5y (fresh equity curve)
    if r['dd'] > 35.0:
        return False, f'excess_dd ({r["dd"]:.1f}%)'
    return True, 'ok'


# ── Param blending ────────────────────────────────────────────────────────

def compute_blend_weight(n_trades: int) -> float:
    if n_trades >= 15: return 0.50
    if n_trades >= 10: return 0.30
    if n_trades >=  5: return 0.20
    return 0.0


def validate_params(p: dict) -> tuple:
    if p['SHORT_ADX_MIN'] >= p['SHORT_ADX_MAX']:
        return False, f'ADX_MIN {p["SHORT_ADX_MIN"]} >= ADX_MAX {p["SHORT_ADX_MAX"]}'
    if any(p.get(k, 1) <= 0 for k in ADAPTABLE_PARAMS):
        return False, 'non-positive param value'
    return True, 'ok'


def blend_params(anchor: dict, window_best: dict, weight: float) -> dict:
    blended = {}
    for k in ADAPTABLE_PARAMS:
        raw = anchor[k] * (1.0 - weight) + window_best[k] * weight
        blended[k] = int(round(raw)) if k in ('SHORT_ADX_MIN', 'SHORT_ADX_MAX') else round(raw, 2)
    ok, err = validate_params(blended)
    if not ok:
        raise ValueError(f'Blended params failed validation: {err}')
    return blended


# ── Config application ────────────────────────────────────────────────────

def apply_to_config(params: dict) -> None:
    with _config_lock:
        for k, v in params.items():
            setattr(cfg, k, v)


def load_and_apply_learned_params() -> bool:
    if not LEARNED_PARAMS_FILE.exists():
        return False
    try:
        data = json.loads(LEARNED_PARAMS_FILE.read_text())
        blended = data['blended']
        ok, err = validate_params(blended)
        if not ok:
            log.warning(f'learned_params.json invalid ({err}) — ignoring, using config.py defaults')
            return False
        apply_to_config(blended)
        age_days = (datetime.now() - datetime.fromisoformat(data['adapted_at'])).days
        log.info(
            f'Loaded learned params (confidence={data["confidence"]}, '
            f'window={data["window_trades"]} bt-trades, adapted {age_days}d ago): '
            + ', '.join(f'{k}={blended[k]}' for k in ADAPTABLE_PARAMS)
        )
        return True
    except Exception as e:
        log.warning(f'Failed to load learned_params.json: {e} — using config.py defaults')
        return False


# ── Persistence ───────────────────────────────────────────────────────────

def _confidence_label(weight: float) -> str:
    return {0.50: 'high', 0.30: 'medium', 0.20: 'low'}.get(weight, 'none')


def save_learned_params(blended: dict, window_best: dict, weight: float,
                        n_trades: int, why: str) -> None:
    data = {
        'adapt_version': 2,
        'adapted_at':    datetime.now().isoformat(),
        'window_months': cfg.ADAPT_WINDOW_MONTHS,
        'window_trades': n_trades,
        'blend_weight':  weight,
        'confidence':    _confidence_label(weight),
        'anchors':       ANCHOR_PARAMS,
        'window_best':   {k: window_best[k] for k in ADAPTABLE_PARAMS},
        'blended':       blended,
        'why':           why,
    }
    tmp = LEARNED_PARAMS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, LEARNED_PARAMS_FILE)   # atomic on POSIX


# ── Main entry point ──────────────────────────────────────────────────────

def run_adaptation(force: bool = False) -> bool:
    """
    Run one adaptation cycle. Returns True if params were updated.
    Safe to call from a background thread.
    """
    log.info('=== Adaptation starting ===')

    # Load window data early so we can report its trade count
    log.info(f'Loading {cfg.ADAPT_WINDOW_MONTHS}m data window...')
    try:
        df_window = load_window_df(cfg.ADAPT_WINDOW_MONTHS)
    except FileNotFoundError as e:
        log.warning(f'Adaptation skipped: {e}')
        return False

    effective_start = df_window.index[WARMUP] if len(df_window) > WARMUP else df_window.index[0]
    log.info(f'Window: {len(df_window)} bars '
             f'({effective_start:%Y-%m-%d} → {df_window.index[-1]:%Y-%m-%d})')

    # Quick probe: what does the current config produce in this window?
    probe = _backtest_short(df_window, {**ANCHOR_PARAMS,
                                         'use_scale_out': True, 'use_chandelier': True})
    log.info(f'Current anchor params in window: n={probe["n"]}, PF={probe["pf"]:.2f}, '
             f'ret={probe["ret"]:+.2f}%, WR={probe["wr"]:.1f}%')

    if probe['n'] < cfg.ADAPT_MIN_TRADES and not force:
        log.info(f'Anchor produces only {probe["n"]} trades in window '
                 f'(< {cfg.ADAPT_MIN_TRADES}) — market regime may lack signals. '
                 f'Staying on 5y anchors.')
        return False

    # Mini-sweep
    log.info(f'Running mini-sweep...')
    t0 = time.time()
    results = run_mini_sweep(df_window)
    elapsed = time.time() - t0
    log.info(f'Mini-sweep: {len(results)} configs in {elapsed:.0f}s')

    # Quality filter
    quality = [r for r in results if quality_gate(r, cfg.ADAPT_MIN_TRADES)[0]]
    log.info(f'{len(quality)}/{len(results)} configs passed quality gate')

    if not quality:
        log.info('No quality configs found — staying on 5y anchors')
        return False

    # Pick best by PF, break ties by trade count
    quality.sort(key=lambda r: (r['pf'], r['n']), reverse=True)
    winner = quality[0]
    log.info(f'Window winner: ADX {winner["SHORT_ADX_MIN"]}-{winner["SHORT_ADX_MAX"]}, '
             f'body≥{winner["SHORT_BODY_ATR_MIN"]}, vol≥{winner["SHORT_VOL_MULT"]}, '
             f'SL×{winner["ATR_SL_MULT"]}, TP×{winner["ATR_TP_MULT"]} | '
             f'n={winner["n"]}, PF={winner["pf"]:.2f}, WR={winner["wr"]:.1f}%, '
             f'DD={winner["dd"]:.1f}%, ret={winner["ret"]:+.2f}%')

    # Blend weight
    weight = compute_blend_weight(winner['n'])
    log.info(f'Blend weight: {weight*100:.0f}% window / {(1-weight)*100:.0f}% anchor '
             f'(confidence: {_confidence_label(weight)}, n={winner["n"]})')

    # Blend
    try:
        blended = blend_params(ANCHOR_PARAMS, winner, weight)
    except ValueError as e:
        log.error(f'Blend failed: {e} — staying on anchors')
        return False

    # Check if change is meaningful
    changes = [
        f'{k}: {ANCHOR_PARAMS[k]} → {blended[k]}'
        for k in ADAPTABLE_PARAMS
        if abs(blended[k] - ANCHOR_PARAMS[k]) > _CHANGE_THRESHOLDS[k]
    ]
    if not changes and not force:
        log.info('Blended params within noise threshold of anchors — no update needed')
        return False

    why = (
        f'Window winner (n={winner["n"]}, PF={winner["pf"]:.2f}) blended at '
        f'{weight*100:.0f}% [{_confidence_label(weight)} confidence]. '
        + ('Changes: ' + '; '.join(changes) if changes else 'No meaningful change.')
    )

    # Final validation, apply, persist
    ok, err = validate_params(blended)
    if not ok:
        log.error(f'Final validation failed: {err}')
        return False

    apply_to_config(blended)
    save_learned_params(blended, winner, weight, winner['n'], why)

    log.info('=== Adaptation complete ===')
    log.info(f'  {why}')
    log.info('  New live params: ' + ', '.join(f'{k}={blended[k]}' for k in ADAPTABLE_PARAMS))
    return True
