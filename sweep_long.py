"""
5-year LONG-edge sweep — finds best ADX/body/vol params for BTC LONG trades.
Mirrors the SHORT sweep methodology exactly.
Run: python3 sweep_long.py
"""
import pandas as pd
import numpy as np
from itertools import product
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).parent / 'data'
WARMUP   = 250

# ── Indicator helpers ────────────────────────────────────────────────────

def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def _atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def _adx(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    up = h - h.shift(1); dn = l.shift(1) - l
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=h.index)
    atr_s = tr.ewm(alpha=1/n, adjust=False).mean()
    pdi_s = pdm.ewm(alpha=1/n, adjust=False).mean()
    ndi_s = ndm.ewm(alpha=1/n, adjust=False).mean()
    pdi = 100 * pdi_s / atr_s
    ndi = 100 * ndi_s / atr_s
    dx = (100 * (pdi-ndi).abs() / (pdi+ndi).replace(0, np.nan)).fillna(0)
    return dx.ewm(alpha=1/n, adjust=False).mean()


# ── Data ─────────────────────────────────────────────────────────────────

def load_data():
    f1h = DATA_DIR / 'btc_1h_5y.csv'
    f4h = DATA_DIR / 'btc_4h_5y.csv'
    if not f1h.exists() or not f4h.exists():
        raise FileNotFoundError(f'Missing CSV data in {DATA_DIR}. Run fetch_data.py first.')

    df1h = pd.read_csv(f1h, parse_dates=['timestamp']).set_index('timestamp')
    df4h = pd.read_csv(f4h, parse_dates=['timestamp']).set_index('timestamp')

    ef = _ema(df4h['close'], 50)
    es = _ema(df4h['close'], 200)
    df4h['direction'] = np.where(ef > es*1.001, 'LONG',
                         np.where(ef < es*0.999, 'SHORT', 'NONE'))

    df1h = df1h.sort_index(); df4h = df4h.sort_index()
    df1h['direction'] = pd.merge_asof(
        df1h.reset_index(), df4h[['direction']].reset_index(),
        on='timestamp', direction='backward'
    )['direction'].values

    df1h['atr']        = _atr(df1h['high'], df1h['low'], df1h['close'], 14)
    df1h['adx']        = _adx(df1h['high'], df1h['low'], df1h['close'], 14)
    df1h['adx_up']     = df1h['adx'] > df1h['adx'].shift(5)
    df1h['donch_hi']   = df1h['high'].shift(1).rolling(20).max()
    df1h['donch_lo']   = df1h['low'].shift(1).rolling(20).min()
    body               = (df1h['close'] - df1h['open']).abs()
    df1h['body_atr']   = body / df1h['atr']
    df1h['bull_candle'] = df1h['close'] > df1h['open']
    df1h['vol_ma20']   = df1h['volume'].shift(1).rolling(20).mean()
    df1h['vol_ratio']  = df1h['volume'] / df1h['vol_ma20']
    df1h['high_20']    = df1h['high'].rolling(20).max()
    df1h['year']       = df1h.index.year
    df1h = df1h.dropna().copy()
    return df1h


# ── Backtest LONG ─────────────────────────────────────────────────────────

def backtest_long(df: pd.DataFrame, params: dict) -> dict:
    adx_min  = params['LONG_ADX_MIN'];  adx_max = params['LONG_ADX_MAX']
    body_min = params['LONG_BODY_ATR_MIN']
    vol_mult = params['LONG_VOL_MULT']
    sl_m     = params['ATR_SL_MULT'];   tp_m = params['ATR_TP_MULT']

    FEE_RATE  = 0.0004
    RISK_PCT  = 0.02
    MAX_POS   = 0.30
    INITIAL   = 10_000.0
    SCALE_OUT_AT_ATR    = 1.0
    SCALE_OUT_FRACTION  = 0.5
    CHANDELIER_ATR_MULT = 3.0
    CHANDELIER_ACTIVATE = 1.5
    TRAIL_BE_AT_ATR     = 1.0

    entry_ok = (
        (df['direction'] == 'LONG').values
        & (df['close'].values > df['donch_hi'].values)
        & df['bull_candle'].values
        & (df['adx'].values >= adx_min) & (df['adx'].values < adx_max)
        & df['adx_up'].values
        & (df['body_atr'].values >= body_min)
        & (df['vol_ratio'].values >= vol_mult)
    )

    closes = df['close'].values; highs = df['high'].values; lows = df['low'].values
    atrs   = df['atr'].values;   high20 = df['high_20'].values; years = df['year'].values

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

        equity = cash + qty * (price - entry_p) if in_pos else cash
        peak = max(peak, equity)
        eq_curve.append(equity)

        if in_pos:
            profit_atr_max = (h - entry_p) / a
            profit_atr_cls = (price - entry_p) / a

            # Scale-out at 1×ATR
            if not scaled and profit_atr_max >= SCALE_OUT_AT_ATR:
                sp = entry_p + SCALE_OUT_AT_ATR * a
                sq = qty * SCALE_OUT_FRACTION
                pnl_s = (sp - entry_p) * sq - FEE_RATE * sp * sq - entry_fee * SCALE_OUT_FRACTION
                cash += sq * sp; qty -= sq
                scaled = True; scale_pnl = pnl_s; sl = entry_p  # BE after scale

            # Chandelier stop
            if profit_atr_cls >= CHANDELIER_ACTIVATE:
                chand = high20[i] - CHANDELIER_ATR_MULT * a
                sl = max(sl, chand) if sl > 0 else chand

            # Break-even trail
            if (h - entry_p) >= TRAIL_BE_AT_ATR * a:
                sl = max(sl, entry_p)

            exit_p = reason = None
            if l <= sl:   exit_p, reason = sl, 'SL'
            elif h >= tp: exit_p, reason = tp, 'TP'

            if exit_p is not None:
                rem_fee = entry_fee * (1-SCALE_OUT_FRACTION) if scaled else entry_fee
                pnl_g = (exit_p - entry_p) * qty
                fee_e = FEE_RATE * exit_p * qty
                pnl_n = pnl_g - fee_e - rem_fee + scale_pnl
                cash += qty * exit_p
                trades.append({'year': years[i], 'pnl': pnl_n, 'reason': reason})
                in_pos = False; qty = 0.0; entry_p = sl = tp = 0.0
                entry_fee = 0.0; scaled = False; scale_pnl = 0.0
            continue

        if not entry_ok[i] or equity <= 100: continue
        sl_dist = sl_m * a
        if sl_dist <= 0: continue
        new_qty = round(min((equity*RISK_PCT)/sl_dist, (equity*MAX_POS)/price), 3)
        if new_qty < 0.001: continue
        cost = new_qty * price; fee = FEE_RATE * cost
        if cost + fee > cash: continue

        cash -= cost + fee; in_pos = True
        entry_p = price; qty = new_qty
        sl = price - sl_m * a; tp = price + tp_m * a
        entry_fee = fee; scaled = False; scale_pnl = 0.0

    if in_pos:
        last_p = closes[-1]
        pnl_g = (last_p - entry_p) * qty
        fee_e = FEE_RATE * last_p * qty
        rem_fee = entry_fee * (1-SCALE_OUT_FRACTION) if scaled else entry_fee
        pnl_n = pnl_g - fee_e - rem_fee + scale_pnl
        cash += qty * last_p
        trades.append({'year': years[-1], 'pnl': pnl_n, 'reason': 'EOD'})

    n_tr = len(trades)
    if n_tr == 0:
        return {'n': 0, 'ret': 0, 'pf': 0, 'wr': 0, 'dd': 0, 'years_profitable': 0}

    wins   = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    gw = sum(t['pnl'] for t in wins); gl = abs(sum(t['pnl'] for t in losses))
    pf = gw/gl if gl > 0 else (99.0 if gw > 0 else 0.0)
    eq = np.array(eq_curve)
    dd = (eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)
    mdd = abs(dd.min()*100) if len(dd) else 0

    # Year-by-year profitability
    years_set = sorted(set(t['year'] for t in trades))
    yp = sum(1 for y in years_set if sum(t['pnl'] for t in trades if t['year']==y) > 0)

    return {
        'n': n_tr, 'ret': (cash/INITIAL-1)*100, 'pf': pf,
        'wr': len(wins)/n_tr*100, 'dd': mdd,
        'years_profitable': yp, 'years_total': len(years_set),
    }


# ── Combined SHORT+LONG backtest (for frequency calc) ──────────────────

def backtest_combined(df: pd.DataFrame, short_params: dict, long_params: dict) -> dict:
    """Count total signals from both edges combined."""
    s_entry_ok = (
        (df['direction'] == 'SHORT').values
        & (df['close'].values < df['donch_lo'].values)
        & ~df['bull_candle'].values
        & (df['adx'].values >= short_params['SHORT_ADX_MIN'])
        & (df['adx'].values < short_params['SHORT_ADX_MAX'])
        & df['adx_up'].values
        & (df['body_atr'].values >= short_params['SHORT_BODY_ATR_MIN'])
        & (df['vol_ratio'].values >= short_params['SHORT_VOL_MULT'])
    )
    l_entry_ok = (
        (df['direction'] == 'LONG').values
        & (df['close'].values > df['donch_hi'].values)
        & df['bull_candle'].values
        & (df['adx'].values >= long_params['LONG_ADX_MIN'])
        & (df['adx'].values < long_params['LONG_ADX_MAX'])
        & df['adx_up'].values
        & (df['body_atr'].values >= long_params['LONG_BODY_ATR_MIN'])
        & (df['vol_ratio'].values >= long_params['LONG_VOL_MULT'])
    )
    df_bt = df.iloc[WARMUP:]
    n_short = int(s_entry_ok[WARMUP:].sum())
    n_long  = int(l_entry_ok[WARMUP:].sum())
    n_years = (df_bt.index[-1] - df_bt.index[0]).days / 365.25
    return {'n_short': n_short, 'n_long': n_long,
            'total': n_short+n_long, 'per_year': round((n_short+n_long)/n_years, 1),
            'years': round(n_years, 1)}


# ── Sweep grid ────────────────────────────────────────────────────────────

LONG_GRID = {
    'LONG_ADX_MIN':      [25, 30, 35, 40],
    'LONG_ADX_MAX':      [50, 55, 60, 65],
    'LONG_BODY_ATR_MIN': [0.5, 0.8, 1.0, 1.5],
    'LONG_VOL_MULT':     [1.0, 1.2, 1.5],
    'ATR_SL_MULT':       [1.5, 2.0, 2.5],
    'ATR_TP_MULT':       [3.0, 4.0, 6.0],
}

# Current SHORT anchors (for combined frequency calc)
SHORT_ANCHOR = {
    'SHORT_ADX_MIN': 50, 'SHORT_ADX_MAX': 65,
    'SHORT_BODY_ATR_MIN': 0.8, 'SHORT_VOL_MULT': 1.2,
}
SHORT_RELAXED = {
    'SHORT_ADX_MIN': 45, 'SHORT_ADX_MAX': 70,
    'SHORT_BODY_ATR_MIN': 0.8, 'SHORT_VOL_MULT': 1.2,
}


if __name__ == '__main__':
    print('Loading 5y data...')
    df = load_data()
    n_years = (df.index[-1] - df.index[0]).days / 365.25
    print(f'  {len(df)} bars, {n_years:.1f} years ({df.index[0].date()} → {df.index[-1].date()})')

    keys = list(LONG_GRID.keys())
    combos = [c for c in product(*[LONG_GRID[k] for k in keys]) if c[0] < c[1]]
    print(f'  Running {len(combos)} LONG configs...')

    results = []
    for vals in combos:
        params = dict(zip(keys, vals))
        r = backtest_long(df, params)
        r.update(params)
        results.append(r)

    # Sort by: years_profitable DESC, then PF DESC
    results.sort(key=lambda r: (r['years_profitable'], r['pf']), reverse=True)

    print(f'\n=== TOP 20 LONG CONFIGS (by years profitable then PF) ===')
    print(f'{"ADX_MIN":>7} {"ADX_MAX":>7} {"BODY":>6} {"VOL":>5} {"SL":>4} {"TP":>4} '
          f'{"N":>4} {"WR%":>5} {"PF":>5} {"Ret%":>6} {"DD%":>5} {"Yrs":>4}')
    print('-'*76)

    shown = 0
    for r in results:
        if r['n'] < 5 or r['pf'] < 1.0 or r['dd'] > 35: continue
        print(f'{r["LONG_ADX_MIN"]:>7} {r["LONG_ADX_MAX"]:>7} '
              f'{r["LONG_BODY_ATR_MIN"]:>6.1f} {r["LONG_VOL_MULT"]:>5.1f} '
              f'{r["ATR_SL_MULT"]:>4.1f} {r["ATR_TP_MULT"]:>4.1f} '
              f'{r["n"]:>4} {r["wr"]:>5.1f} {r["pf"]:>5.2f} '
              f'{r["ret"]:>+6.1f} {r["dd"]:>5.1f} '
              f'{r["years_profitable"]}/{r["years_total"]}')
        shown += 1
        if shown >= 20: break

    if shown == 0:
        print('  [no configs passed n≥5, PF≥1.0, DD≤35%]')
        # Show top 10 by years_profitable regardless
        print('\n=== TOP 10 (any quality) ===')
        for r in results[:10]:
            if r['n'] == 0: continue
            print(f'  ADX[{r["LONG_ADX_MIN"]}-{r["LONG_ADX_MAX"]}) body≥{r["LONG_BODY_ATR_MIN"]} '
                  f'vol≥{r["LONG_VOL_MULT"]} SL×{r["ATR_SL_MULT"]} TP×{r["ATR_TP_MULT"]} | '
                  f'n={r["n"]} WR={r["wr"]:.0f}% PF={r["pf"]:.2f} ret={r["ret"]:+.1f}% '
                  f'DD={r["dd"]:.1f}% years={r["years_profitable"]}/{r["years_total"]}')

    # Pick best
    quality = [r for r in results if r['n'] >= 5 and r['pf'] >= 1.0 and r['dd'] <= 35]
    if quality:
        best = quality[0]
        print(f'\n=== BEST LONG CONFIG ===')
        print(f'  ADX [{best["LONG_ADX_MIN"]}-{best["LONG_ADX_MAX"]})')
        print(f'  BODY_ATR_MIN = {best["LONG_BODY_ATR_MIN"]}')
        print(f'  VOL_MULT     = {best["LONG_VOL_MULT"]}')
        print(f'  SL_MULT      = {best["ATR_SL_MULT"]}  TP_MULT = {best["ATR_TP_MULT"]}')
        print(f'  Performance  : n={best["n"]}, WR={best["wr"]:.1f}%, PF={best["pf"]:.2f}, '
              f'ret={best["ret"]:+.1f}%, DD={best["dd"]:.1f}%')
        print(f'  Profitable   : {best["years_profitable"]}/{best["years_total"]} years')

        # Frequency with best LONG + current SHORT anchors
        long_p = {k: best[k] for k in LONG_GRID.keys()}
        freq = backtest_combined(df, SHORT_ANCHOR, long_p)
        print(f'\n  Combined frequency (SHORT anchor + this LONG):')
        print(f'    SHORT: {freq["n_short"]} trades in {freq["years"]:.1f}y = {freq["n_short"]/freq["years"]:.1f}/yr')
        print(f'    LONG:  {freq["n_long"]}  trades                     = {freq["n_long"]/freq["years"]:.1f}/yr')
        print(f'    TOTAL: {freq["total"]} trades                       = {freq["per_year"]}/yr')

        freq2 = backtest_combined(df, SHORT_RELAXED, long_p)
        print(f'\n  Combined frequency (SHORT relaxed ADX[45-70) + this LONG):')
        print(f'    SHORT: {freq2["n_short"]} trades = {freq2["n_short"]/freq2["years"]:.1f}/yr')
        print(f'    LONG:  {freq2["n_long"]}  trades = {freq2["n_long"]/freq2["years"]:.1f}/yr')
        print(f'    TOTAL: {freq2["total"]} trades   = {freq2["per_year"]}/yr')

    # Also run SHORT with relaxed ADX to show standalone improvement
    from adapt import _backtest_short, load_window_df
    print(f'\n=== SHORT edge with relaxed ADX[45-70) ===')
    short_relaxed_params = {
        'SHORT_ADX_MIN': 45, 'SHORT_ADX_MAX': 70,
        'SHORT_BODY_ATR_MIN': 0.8, 'SHORT_VOL_MULT': 1.2,
        'ATR_SL_MULT': 2.0, 'ATR_TP_MULT': 4.0,
        'use_scale_out': True, 'use_chandelier': True,
    }
    # Load full 5y window
    try:
        df_full = load_window_df(60)  # 60 months = full 5y
        r_short = _backtest_short(df_full, short_relaxed_params)
        print(f'  ADX[45-70) body≥0.8 vol≥1.2: '
              f'n={r_short["n"]}, WR={r_short["wr"]:.1f}%, PF={r_short["pf"]:.2f}, '
              f'ret={r_short["ret"]:+.1f}%, DD={r_short["dd"]:.1f}%')
    except Exception as e:
        print(f'  [SHORT relaxed backtest failed: {e}]')

    print(f'\nDone.')
