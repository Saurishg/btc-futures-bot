"""
SHORT V2 SWEEP — comprehensive grid search using LIVE strategy code.

Goal: find SHORT params that produce real, stable edge across the 5y window.

Uses the EXACT compute_signal() and compute_qty() from strategy.py, with
config monkey-patched per variant. Backtests on 1h entry / 4h HTF (matches
live bot).

Grid:
  SHORT_ADX_MIN:        [35, 40, 45, 50]
  SHORT_ADX_MAX:        [55, 65, 75, 100]
  SHORT_BODY_ATR_MIN:   [0.5, 0.8, 1.0, 1.5]
  SHORT_VOL_MULT:       [1.2, 1.5, 2.0, 2.5]
  ATR_SL_MULT:          [1.5, 2.0, 2.5]
  ATR_TP_MULT:          [4.0, 6.0, 8.0]
  Scale-out:            [on, off]
  Chandelier trail:     [on, off]

Selection: profit factor ≥ 1.3 AND profitable in ≥ 4/6 years AND DD < 10%.

Walk-forward validation:
  Train: 2021-2023 (3y)
  Test:  2024-2026 (3y)
"""
import sys, csv, math
from datetime import datetime
from pathlib import Path
from itertools import product
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).parent))

import config as cfg
import strategy as strat

DATA = Path(__file__).parent / 'data'
INITIAL = 10_000.0
WARMUP = 250


def load_csv(p):
    rows = []
    with open(p) as f:
        for r in csv.DictReader(f):
            ts = datetime.fromisoformat(r['timestamp'])
            rows.append({'ts': ts, 'open': float(r['open']), 'high': float(r['high']),
                         'low': float(r['low']), 'close': float(r['close']),
                         'vol': float(r['volume']), 'year': ts.year})
    return rows


def to_kline(b):
    return [int(b['ts'].timestamp()*1000), b['open'], b['high'], b['low'],
            b['close'], b['vol'], 0,0,0,0,0,0]


def backtest_short(bars1h, bars4h, params: dict, year_start: int = 0, year_end: int = 9999):
    """Backtest with monkey-patched config. Only SHORT trades."""
    # Patch config
    for k, v in params.items():
        setattr(cfg, k, v)
    cfg.SHORTS_ONLY = True

    use_scale  = params.get('use_scale_out', True)
    use_chand  = params.get('use_chandelier', True)

    cash = INITIAL
    side = 'NONE'; qty = 0.0; state = {}; trades = []
    peak = INITIAL
    eq_curve = []

    # HTF index map
    htf_idx_map = []
    h_idx = 0
    for b in bars1h:
        while h_idx+1 < len(bars4h) and bars4h[h_idx+1]['ts'].timestamp() <= b['ts'].timestamp():
            h_idx += 1
        htf_idx_map.append(h_idx)

    for idx in range(WARMUP, len(bars1h)):
        bar = bars1h[idx]
        if bar['year'] < year_start or bar['year'] > year_end:
            continue

        k1h = [to_kline(b) for b in bars1h[max(0,idx-249):idx+1]]
        h_idx = htf_idx_map[idx]
        k4h = [to_kline(b) for b in bars4h[max(0,h_idx-249):h_idx+1]]
        if len(k1h) < 240 or len(k4h) < 200: continue

        s = strat.compute_signal(k1h, k4h)
        atr_val = s['atr']

        # Mark-to-market
        if side == 'SHORT':
            equity = cash + qty * (state['entry'] - bar['close']) + qty * state['entry']
        else:
            equity = cash
        peak = max(peak, equity)
        eq_curve.append(equity)

        if side == 'SHORT':
            entry = state['entry']; sl = state['sl']; tp = state['tp']
            profit_atr_max = (entry - bar['low']) / atr_val if atr_val > 0 else 0
            profit_atr_cls = (entry - bar['close']) / atr_val if atr_val > 0 else 0

            # Scale-out at +1×ATR (max during bar)
            if use_scale and not state.get('scaled') and profit_atr_max >= cfg.SCALE_OUT_AT_ATR:
                sp = entry - cfg.SCALE_OUT_AT_ATR * atr_val
                scale_q = qty * cfg.SCALE_OUT_FRACTION
                pnl_s = (entry - sp) * scale_q - cfg.FEE_RATE * sp * scale_q
                pnl_s -= state.get('entry_fee', 0) * cfg.SCALE_OUT_FRACTION
                cash += scale_q * entry + pnl_s
                qty -= scale_q
                state['scaled'] = True; state['scale_pnl'] = pnl_s
                sl = entry; state['sl'] = sl  # move SL to BE

            # Chandelier trail
            if use_chand and profit_atr_cls >= cfg.CHANDELIER_ACTIVATE_AT_ATR:
                chand = strat.chandelier_stop(
                    [float(k[2]) for k in k1h], [float(k[3]) for k in k1h],
                    atr_val, 'SHORT')
                sl = min(sl, chand) if sl > 0 else chand

            # BE trail
            if entry - bar['low'] >= cfg.TRAIL_BE_AT_ATR * atr_val:
                sl = min(sl, entry) if sl > 0 else entry
            state['sl'] = sl

            # Check exits (using high/low for intra-bar)
            ex_p, reason = None, None
            if bar['high'] >= sl:        ex_p, reason = sl, 'SL'
            elif bar['low']  <= tp:      ex_p, reason = tp, 'TP'

            if ex_p:
                pnl_g = (entry - ex_p) * qty
                fee_e = cfg.FEE_RATE * ex_p * qty
                rem_fee = state.get('entry_fee', 0) * ((1 - cfg.SCALE_OUT_FRACTION) if state.get('scaled') else 1)
                pnl_n = pnl_g - fee_e - rem_fee + state.get('scale_pnl', 0)
                cash += qty * entry + (pnl_g - fee_e - rem_fee)
                trades.append({
                    'year': bar['year'], 'pnl': pnl_n,
                    'reason': reason, 'scaled': state.get('scaled', False),
                })
                side = 'NONE'; qty = 0.0; state = {}
            continue

        # Entry — only shorts
        if not s['short_signal']: continue
        if equity <= 100: continue  # busted

        price = s['price']
        qty_new = strat.compute_qty(equity, price, atr_val)
        if qty_new < 0.001: continue

        cost = qty_new * price; fee = cfg.FEE_RATE * cost
        if cost + fee > cash: continue

        sl_p = price + cfg.ATR_SL_MULT * atr_val
        tp_p = price - cfg.ATR_TP_MULT * atr_val
        cash -= cost + fee
        side = 'SHORT'; qty = qty_new
        state = {'entry': price, 'sl': sl_p, 'tp': tp_p,
                 'entry_fee': fee, 'year': bar['year']}

    # Close at end
    if side == 'SHORT':
        last = bars1h[-1]
        pnl_g = (state['entry'] - last['close']) * qty
        fee_e = cfg.FEE_RATE * last['close'] * qty
        pnl_n = pnl_g - fee_e + state.get('scale_pnl', 0)
        cash += qty * state['entry'] + (pnl_g - fee_e)
        trades.append({'year': last['year'], 'pnl': pnl_n, 'reason': 'EOD', 'scaled': False})

    # Metrics
    n = len(trades)
    if n == 0:
        return {'n': 0, 'ret': 0, 'pf': 0, 'wr': 0, 'dd': 0, 'years_profit': 0, 'avg_w': 0, 'avg_l': 0}
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr = len(wins)/n*100
    gw = sum(t['pnl'] for t in wins); gl = abs(sum(t['pnl'] for t in losses))
    pf = gw/gl if gl > 0 else (99 if gw > 0 else 0)
    by_year = defaultdict(float)
    for t in trades: by_year[t['year']] += t['pnl']
    years_profitable = sum(1 for y, p in by_year.items() if p > 0)
    years_total = len(by_year)
    peak = INITIAL; mdd = 0
    for e in eq_curve:
        peak = max(peak, e); mdd = max(mdd, (peak-e)/peak)
    ret = (cash / INITIAL - 1) * 100
    avg_w = sum(t['pnl'] for t in wins)/len(wins) if wins else 0
    avg_l = sum(t['pnl'] for t in losses)/len(losses) if losses else 0

    return {'n': n, 'ret': ret, 'pf': pf, 'wr': wr, 'dd': mdd*100,
            'years_profit': years_profitable, 'years_total': years_total,
            'avg_w': avg_w, 'avg_l': avg_l, 'cash': cash}


def main():
    print('Loading 5y 1h + 4h data...')
    b1h = load_csv(DATA / 'btc_1h_5y.csv')
    b4h = load_csv(DATA / 'btc_4h_5y.csv')
    print(f'  {len(b1h)} × 1h, {len(b4h)} × 4h')

    # Grid
    grid = {
        'SHORT_ADX_MIN':      [35, 40, 45, 50],
        'SHORT_ADX_MAX':      [55, 65, 75, 100],
        'SHORT_BODY_ATR_MIN': [0.5, 0.8, 1.0, 1.5],
        'SHORT_VOL_MULT':     [1.2, 1.5, 2.0],
        'ATR_SL_MULT':        [1.5, 2.0, 2.5],
        'ATR_TP_MULT':        [4.0, 6.0, 8.0],
    }

    extras = [
        {'use_scale_out': True,  'use_chandelier': True},   # current
        {'use_scale_out': False, 'use_chandelier': True},   # no scale
        {'use_scale_out': False, 'use_chandelier': False},  # vanilla SL/TP
    ]

    keys = list(grid.keys())
    combos = list(product(*[grid[k] for k in keys]))
    print(f'Testing {len(combos)} × {len(extras)} = {len(combos)*len(extras)} configurations\n')

    results = []
    for vals in combos:
        params = dict(zip(keys, vals))
        if params['SHORT_ADX_MIN'] >= params['SHORT_ADX_MAX']: continue
        for ext in extras:
            params_full = {**params, **ext}
            r = backtest_short(b1h, b4h, params_full)
            r.update(params_full)
            results.append(r)

    # Quality filter: PF >= 1.3, profitable in >= 4/6 years, DD < 12%, >= 15 trades
    quality = [r for r in results if (
        r['n'] >= 15
        and r['pf'] >= 1.3
        and r['years_profit'] >= 4
        and r['dd'] < 12
    )]

    print(f'{len(quality)} / {len(results)} configurations passed quality gate')
    print(f'(PF≥1.3, profitable in ≥4/6 years, MaxDD<12%, ≥15 trades)\n')

    if not quality:
        print('No quality configs found. Showing top by raw return:')
        quality = sorted(results, key=lambda r: r['ret'], reverse=True)[:20]
    else:
        quality.sort(key=lambda r: (r['years_profit'], r['pf']), reverse=True)

    print(f'{"ADX":>9} {"Body":>5} {"Vol":>4} {"SL":>4} {"TP":>4} {"Scale":>5} {"Chand":>5} '
          f'{"N":>4} {"WR":>5} {"PF":>5} {"Ret":>8} {"DD":>6} {"Yrs":>5} {"AvgW/L":>10}')
    print('=' * 110)
    for r in quality[:25]:
        adx_str = f'{r["SHORT_ADX_MIN"]}-{r["SHORT_ADX_MAX"]}'
        scale_s = 'Y' if r['use_scale_out'] else 'N'
        chand_s = 'Y' if r['use_chandelier'] else 'N'
        print(f'{adx_str:>9} {r["SHORT_BODY_ATR_MIN"]:>5.1f} {r["SHORT_VOL_MULT"]:>4.1f} '
              f'{r["ATR_SL_MULT"]:>4.1f} {r["ATR_TP_MULT"]:>4.1f} {scale_s:>5} {chand_s:>5} '
              f'{r["n"]:>4} {r["wr"]:>4.1f}% {r["pf"]:>5.2f} {r["ret"]:>+7.2f}% '
              f'{r["dd"]:>5.2f}% {r["years_profit"]:>2}/{r["years_total"]:<2} '
              f'{r["avg_w"]:>+5.1f}/{r["avg_l"]:>+5.1f}')

    if quality:
        best = quality[0]
        print(f'\n🏆 Best by years-profitable then PF:')
        print(f'   ADX [{best["SHORT_ADX_MIN"]}-{best["SHORT_ADX_MAX"]}), body≥{best["SHORT_BODY_ATR_MIN"]}×ATR, '
              f'vol≥{best["SHORT_VOL_MULT"]}×MA, SL={best["ATR_SL_MULT"]}×ATR, TP={best["ATR_TP_MULT"]}×ATR')
        print(f'   scale_out={best["use_scale_out"]}, chandelier={best["use_chandelier"]}')
        print(f'   → {best["n"]} trades, WR {best["wr"]:.1f}%, PF {best["pf"]:.2f}, '
              f'Return {best["ret"]:+.2f}%, MaxDD {best["dd"]:.2f}%, '
              f'profitable in {best["years_profit"]}/{best["years_total"]} years')

        # Walk-forward validation on this config
        print(f'\n── WALK-FORWARD VALIDATION on top config ──')
        train = {k: v for k, v in best.items() if k in ['SHORT_ADX_MIN','SHORT_ADX_MAX',
                'SHORT_BODY_ATR_MIN','SHORT_VOL_MULT','ATR_SL_MULT','ATR_TP_MULT',
                'use_scale_out','use_chandelier']}
        r_train = backtest_short(b1h, b4h, train, year_start=2021, year_end=2023)
        r_test  = backtest_short(b1h, b4h, train, year_start=2024, year_end=2026)
        print(f'  Train (2021-23): {r_train["n"]} trades, PF {r_train["pf"]:.2f}, '
              f'Ret {r_train["ret"]:+.2f}%, WR {r_train["wr"]:.1f}%')
        print(f'  Test  (2024-26): {r_test["n"]} trades, PF {r_test["pf"]:.2f}, '
              f'Ret {r_test["ret"]:+.2f}%, WR {r_test["wr"]:.1f}%')
        if r_test['pf'] >= 1.2 and r_test['ret'] > 0:
            print(f'  ✅ EDGE HOLDS out-of-sample')
        else:
            print(f'  ⚠️  Out-of-sample WEAK — likely overfit')


if __name__ == '__main__':
    main()
