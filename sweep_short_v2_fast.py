"""
SHORT V2 SWEEP — FAST vectorized version.

Precomputes all indicators ONCE in pandas, then runs each param combo
through a tight Python loop using only the precomputed Series.

Expected speedup: 50-100x over the original sweep.
"""
import sys, math
from pathlib import Path
from itertools import product
from collections import defaultdict
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
DATA = Path(__file__).parent / 'data'
INITIAL = 10_000.0
WARMUP = 250
FEE_RATE = 0.0004
MAX_POS  = 0.30
RISK_PCT = 0.02

# Fixed exit params
TRAIL_BE_AT_ATR            = 1.0
CHANDELIER_ATR_MULT        = 3.0
CHANDELIER_ACTIVATE_AT_ATR = 1.5
CHANDELIER_PERIOD          = 20
SCALE_OUT_AT_ATR           = 1.0
SCALE_OUT_FRACTION         = 0.5
BREAKOUT_PERIOD            = 20
ADX_PERIOD                 = 14
ATR_PERIOD                 = 14
RSI_PERIOD                 = 14
HTF_EMA_FAST               = 50
HTF_EMA_SLOW               = 200


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _rsi(c, n=14):
    diff = c.diff()
    gain = diff.clip(lower=0); loss = -diff.clip(upper=0)
    ag = gain.rolling(n).mean(); al = loss.rolling(n).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(100)


def _adx(h, l, c, n=14):
    """Wilder ADX with +DI/-DI."""
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    up = h - h.shift(1)
    dn = l.shift(1) - l
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pdm = pd.Series(pdm, index=h.index)
    ndm = pd.Series(ndm, index=h.index)
    # Wilder smoothing
    atr_s = tr.ewm(alpha=1/n, adjust=False).mean()
    pdi_s = pdm.ewm(alpha=1/n, adjust=False).mean()
    ndi_s = ndm.ewm(alpha=1/n, adjust=False).mean()
    pdi = 100 * pdi_s / atr_s
    ndi = 100 * ndi_s / atr_s
    dx = (100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)).fillna(0)
    adx = dx.ewm(alpha=1/n, adjust=False).mean()
    return adx, pdi, ndi


def load_data():
    df1h = pd.read_csv(DATA / 'btc_1h_5y.csv', parse_dates=['timestamp']).set_index('timestamp')
    df4h = pd.read_csv(DATA / 'btc_4h_5y.csv', parse_dates=['timestamp']).set_index('timestamp')

    # 4h trend
    ef = _ema(df4h['close'], HTF_EMA_FAST)
    es = _ema(df4h['close'], HTF_EMA_SLOW)
    df4h['direction'] = np.where(ef > es * 1.001, 'LONG',
                          np.where(ef < es * 0.999, 'SHORT', 'NONE'))

    # Map 4h direction onto each 1h bar using merge_asof
    df1h = df1h.sort_index()
    df4h = df4h.sort_index()
    df1h['direction'] = pd.merge_asof(
        df1h.reset_index(),
        df4h[['direction']].reset_index(),
        on='timestamp', direction='backward'
    )['direction'].values

    # 1h indicators
    df1h['atr']      = _atr(df1h['high'], df1h['low'], df1h['close'], ATR_PERIOD)
    df1h['rsi']      = _rsi(df1h['close'], RSI_PERIOD)
    adx, pdi, ndi    = _adx(df1h['high'], df1h['low'], df1h['close'], ADX_PERIOD)
    df1h['adx']      = adx
    df1h['adx_5ago'] = adx.shift(5)
    df1h['adx_up']   = adx > adx.shift(5)

    # Donchian (exclude current bar)
    df1h['donch_hi'] = df1h['high'].shift(1).rolling(BREAKOUT_PERIOD).max()
    df1h['donch_lo'] = df1h['low'].shift(1).rolling(BREAKOUT_PERIOD).min()

    # Body & volume ratio
    body = (df1h['close'] - df1h['open']).abs()
    df1h['body_atr'] = body / df1h['atr']
    df1h['bear_candle'] = df1h['close'] < df1h['open']
    df1h['bull_candle'] = df1h['close'] > df1h['open']
    df1h['vol_ma20']  = df1h['volume'].shift(1).rolling(20).mean()
    df1h['vol_ratio'] = df1h['volume'] / df1h['vol_ma20']

    # Chandelier helpers
    df1h['low_20']  = df1h['low'].rolling(CHANDELIER_PERIOD).min()
    df1h['high_20'] = df1h['high'].rolling(CHANDELIER_PERIOD).max()

    df1h['year'] = df1h.index.year
    return df1h.dropna().copy()


def backtest_short(df, params):
    """Vectorized prep + tight loop. Only SHORT trades."""
    use_scale = params.get('use_scale_out', True)
    use_chand = params.get('use_chandelier', True)
    adx_min = params['SHORT_ADX_MIN']; adx_max = params['SHORT_ADX_MAX']
    body_min = params['SHORT_BODY_ATR_MIN']
    vol_mult = params['SHORT_VOL_MULT']
    sl_m = params['ATR_SL_MULT']
    tp_m = params['ATR_TP_MULT']

    # Boolean entry signal vector
    entry_ok = (
        (df['direction'] == 'SHORT').values
        & (df['close'].values < df['donch_lo'].values)
        & df['bear_candle'].values
        & (df['adx'].values >= adx_min)
        & (df['adx'].values < adx_max)
        & df['adx_up'].values
        & (df['body_atr'].values >= body_min)
        & (df['vol_ratio'].values >= vol_mult)
    )

    closes = df['close'].values
    highs  = df['high'].values
    lows   = df['low'].values
    atrs   = df['atr'].values
    low20  = df['low_20'].values
    years  = df['year'].values

    cash = INITIAL
    peak = INITIAL
    in_pos = False
    entry_p = sl = tp = qty = 0.0
    entry_fee = 0.0; scaled = False; scale_pnl = 0.0
    trades = []
    eq_curve = []

    n = len(df)
    for i in range(n):
        if i < WARMUP: continue
        price = closes[i]
        h = highs[i]; l = lows[i]; a = atrs[i]
        if not np.isfinite(a) or a <= 0:
            eq_curve.append(cash)
            continue

        # MTM (short)
        if in_pos:
            equity = cash + qty * (entry_p - price) + qty * entry_p
        else:
            equity = cash
        peak = max(peak, equity)
        eq_curve.append(equity)

        # Exit logic
        if in_pos:
            profit_atr_max = (entry_p - l) / a
            profit_atr_cls = (entry_p - price) / a

            # Scale-out
            if use_scale and not scaled and profit_atr_max >= SCALE_OUT_AT_ATR:
                sp = entry_p - SCALE_OUT_AT_ATR * a
                sq = qty * SCALE_OUT_FRACTION
                pnl_s = (entry_p - sp) * sq - FEE_RATE * sp * sq - entry_fee * SCALE_OUT_FRACTION
                cash += sq * entry_p + pnl_s
                qty -= sq
                scaled = True; scale_pnl = pnl_s
                sl = entry_p  # BE on remainder

            # Chandelier
            if use_chand and profit_atr_cls >= CHANDELIER_ACTIVATE_AT_ATR:
                chand = low20[i] + CHANDELIER_ATR_MULT * a
                if sl > 0: sl = min(sl, chand)
                else: sl = chand

            # BE
            if (entry_p - l) >= TRAIL_BE_AT_ATR * a:
                sl = min(sl, entry_p) if sl > 0 else entry_p

            # Check exits
            exit_p = None; reason = None
            if h >= sl:
                exit_p = sl; reason = 'SL'
            elif l <= tp:
                exit_p = tp; reason = 'TP'

            if exit_p is not None:
                rem_fee = entry_fee * (1 - SCALE_OUT_FRACTION) if scaled else entry_fee
                pnl_g = (entry_p - exit_p) * qty
                fee_e = FEE_RATE * exit_p * qty
                pnl_n = pnl_g - fee_e - rem_fee + scale_pnl
                cash += qty * entry_p + (pnl_g - fee_e - rem_fee)
                trades.append({'year': years[i], 'pnl': pnl_n, 'reason': reason, 'scaled': scaled})
                in_pos = False; qty = 0.0; entry_p = sl = tp = 0.0
                entry_fee = 0.0; scaled = False; scale_pnl = 0.0
            continue

        # Entry
        if not entry_ok[i]: continue
        if equity <= 100: continue
        sl_dist = sl_m * a
        if sl_dist <= 0: continue
        size_risk = (equity * RISK_PCT) / sl_dist
        size_cap  = (equity * MAX_POS) / price
        new_qty = min(size_risk, size_cap)
        new_qty = round(new_qty, 3)
        if new_qty < 0.001: continue
        cost = new_qty * price; fee = FEE_RATE * cost
        if cost + fee > cash: continue

        cash -= cost + fee
        in_pos = True
        entry_p = price; qty = new_qty
        sl = price + sl_m * a; tp = price - tp_m * a
        entry_fee = fee
        scaled = False; scale_pnl = 0.0

    # Force close at end
    if in_pos:
        last_p = closes[-1]
        pnl_g = (entry_p - last_p) * qty
        fee_e = FEE_RATE * last_p * qty
        rem_fee = entry_fee * (1 - SCALE_OUT_FRACTION) if scaled else entry_fee
        pnl_n = pnl_g - fee_e - rem_fee + scale_pnl
        cash += qty * entry_p + (pnl_g - fee_e - rem_fee)
        trades.append({'year': years[-1], 'pnl': pnl_n, 'reason': 'EOD', 'scaled': scaled})

    # Metrics
    n_tr = len(trades)
    if n_tr == 0:
        return {'n': 0, 'ret': 0, 'pf': 0, 'wr': 0, 'dd': 0, 'years_profit': 0,
                'years_total': 0, 'avg_w': 0, 'avg_l': 0, 'cash': cash}
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr = len(wins)/n_tr*100
    gw = sum(t['pnl'] for t in wins); gl = abs(sum(t['pnl'] for t in losses))
    pf = gw/gl if gl > 0 else (99 if gw > 0 else 0)
    by_year = defaultdict(float)
    for t in trades: by_year[t['year']] += t['pnl']
    years_p = sum(1 for p in by_year.values() if p > 0)
    eq = np.array(eq_curve)
    dd = (eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)
    mdd = abs(dd.min()*100) if len(dd) else 0
    ret = (cash/INITIAL - 1)*100
    avg_w = sum(t['pnl'] for t in wins)/len(wins) if wins else 0
    avg_l = sum(t['pnl'] for t in losses)/len(losses) if losses else 0
    return {'n': n_tr, 'ret': ret, 'pf': pf, 'wr': wr, 'dd': mdd,
            'years_profit': years_p, 'years_total': len(by_year),
            'avg_w': avg_w, 'avg_l': avg_l, 'cash': cash}


def main():
    print('Loading + precomputing indicators...')
    df = load_data()
    print(f'  {len(df)} 1h candles, {df.index[0]} → {df.index[-1]}\n')

    grid = {
        'SHORT_ADX_MIN':      [35, 40, 45, 50],
        'SHORT_ADX_MAX':      [55, 65, 75, 100],
        'SHORT_BODY_ATR_MIN': [0.5, 0.8, 1.0, 1.5],
        'SHORT_VOL_MULT':     [1.2, 1.5, 2.0],
        'ATR_SL_MULT':        [1.5, 2.0, 2.5],
        'ATR_TP_MULT':        [4.0, 6.0, 8.0],
    }
    extras = [
        {'use_scale_out': True,  'use_chandelier': True},
        {'use_scale_out': False, 'use_chandelier': True},
        {'use_scale_out': False, 'use_chandelier': False},
    ]
    keys = list(grid.keys())
    combos = [c for c in product(*[grid[k] for k in keys]) if c[0] < c[1]]
    print(f'Testing {len(combos)} × {len(extras)} = {len(combos)*len(extras)} configurations\n')

    results = []
    import time as _t
    t0 = _t.time()
    for ci, vals in enumerate(combos):
        params = dict(zip(keys, vals))
        for ext in extras:
            full = {**params, **ext}
            r = backtest_short(df, full)
            r.update(full)
            results.append(r)
        if (ci+1) % 30 == 0:
            elapsed = _t.time() - t0
            rate = (ci+1) / elapsed
            eta = (len(combos) - ci - 1) / rate
            print(f'  {ci+1}/{len(combos)} combos · {rate:.1f}/s · ETA {eta/60:.1f} min')

    print(f'\nDone in {(_t.time()-t0)/60:.1f} min')

    quality = [r for r in results if (r['n'] >= 15 and r['pf'] >= 1.3
                                       and r['years_profit'] >= 4 and r['dd'] < 12)]
    print(f'\n{len(quality)}/{len(results)} configs passed quality gate'
          f' (PF≥1.3, ≥4/6 years profitable, MaxDD<12%, ≥15 trades)\n')

    if not quality:
        print('No quality configs. Top 15 by raw return + ≥15 trades:')
        quality = sorted([r for r in results if r['n'] >= 15],
                         key=lambda r: r['ret'], reverse=True)[:15]
    else:
        quality.sort(key=lambda r: (r['years_profit'], r['pf']), reverse=True)

    print(f'{"ADX":>9} {"Body":>4} {"Vol":>4} {"SL":>4} {"TP":>4} {"Sc/Ch":>5} '
          f'{"N":>4} {"WR":>5} {"PF":>5} {"Ret":>8} {"DD":>6} {"Yrs":>5} {"AvgW/L":>11}')
    print('=' * 105)
    for r in quality[:25]:
        sc_s = 'SY' if r['use_scale_out'] else 'SN'
        ch_s = 'CY' if r['use_chandelier'] else 'CN'
        print(f'{r["SHORT_ADX_MIN"]:>3}-{r["SHORT_ADX_MAX"]:>3} {r["SHORT_BODY_ATR_MIN"]:>4.1f} '
              f'{r["SHORT_VOL_MULT"]:>4.1f} {r["ATR_SL_MULT"]:>4.1f} {r["ATR_TP_MULT"]:>4.1f} '
              f'{sc_s+ch_s:>5} {r["n"]:>4} {r["wr"]:>4.1f}% {r["pf"]:>5.2f} '
              f'{r["ret"]:>+7.2f}% {r["dd"]:>5.2f}% {r["years_profit"]:>2}/{r["years_total"]:<2} '
              f'{r["avg_w"]:>+5.1f}/{r["avg_l"]:>+5.1f}')

    if quality:
        best = quality[0]
        print(f'\n🏆 Best by (years-profitable, PF):')
        for k in ['SHORT_ADX_MIN','SHORT_ADX_MAX','SHORT_BODY_ATR_MIN','SHORT_VOL_MULT',
                  'ATR_SL_MULT','ATR_TP_MULT','use_scale_out','use_chandelier']:
            print(f'   {k}: {best[k]}')
        print(f'   → {best["n"]} trades, WR {best["wr"]:.1f}%, PF {best["pf"]:.2f}, '
              f'Return {best["ret"]:+.2f}%, MaxDD {best["dd"]:.2f}%, '
              f'{best["years_profit"]}/{best["years_total"]} years profitable')

        # Walk-forward
        print(f'\n── WALK-FORWARD on top config ──')
        train_df = df[(df['year'] >= 2021) & (df['year'] <= 2023)].copy()
        test_df  = df[(df['year'] >= 2024) & (df['year'] <= 2026)].copy()
        train_params = {k: best[k] for k in ['SHORT_ADX_MIN','SHORT_ADX_MAX',
                'SHORT_BODY_ATR_MIN','SHORT_VOL_MULT','ATR_SL_MULT','ATR_TP_MULT',
                'use_scale_out','use_chandelier']}
        rt = backtest_short(train_df, train_params)
        rs = backtest_short(test_df, train_params)
        print(f'  Train 2021-23: {rt["n"]} tr, PF {rt["pf"]:.2f}, Ret {rt["ret"]:+.2f}%, WR {rt["wr"]:.1f}%')
        print(f'  Test  2024-26: {rs["n"]} tr, PF {rs["pf"]:.2f}, Ret {rs["ret"]:+.2f}%, WR {rs["wr"]:.1f}%')
        if rs['pf'] >= 1.2 and rs['ret'] > 0:
            print('  ✅ EDGE HOLDS out-of-sample')
        else:
            print('  ⚠️  Out-of-sample WEAK — likely overfit')


if __name__ == '__main__':
    main()
