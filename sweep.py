"""
Parameter sweep — tries a small grid of reasonable configs.
Not a brute-force overfit: only 8 configs based on diagnostic insight from
first backtest (88% SL exits → widen stops + tighten entries).
"""
import sys, csv, math
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as cfg
import strategy as strat
import importlib

DATA = Path(__file__).parent / 'data'
INITIAL = 10_000.0
WARMUP_15M = 250


def load_csv(path: Path) -> list:
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            ts = datetime.fromisoformat(r['timestamp'])
            rows.append({'ts': ts, 'open': float(r['open']), 'high': float(r['high']),
                         'low': float(r['low']), 'close': float(r['close']),
                         'vol': float(r['volume']), 'hour': ts.hour})
    return rows


def to_kline(b):
    return [int(b['ts'].timestamp()*1000), b['open'], b['high'], b['low'],
            b['close'], b['vol'], 0, 0, 0, 0, 0, 0]


def find_htf_idx(htf, ts):
    target = ts.timestamp()
    lo, hi = 0, len(htf) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if htf[mid]['ts'].timestamp() <= target: lo = mid
        else: hi = mid - 1
    return lo


def run_one(bars_15m, bars_1h, params):
    # Override cfg with params
    for k, v in params.items():
        setattr(cfg, k, v)
    importlib.reload(strat)

    cash = INITIAL
    pos_side = 'NONE'; pos_qty = 0.0; state = {}; trades = []; eq = []
    peak = INITIAL; daily_pnl = {}

    for idx in range(WARMUP_15M, len(bars_15m)):
        bar = bars_15m[idx]
        kline15 = [to_kline(b) for b in bars_15m[max(0, idx-249):idx+1]]
        htf_end = find_htf_idx(bars_1h, bar['ts'])
        kline1h = [to_kline(b) for b in bars_1h[max(0, htf_end-249):htf_end+1]]
        if len(kline15) < 240 or len(kline1h) < 200: continue

        s = strat.compute_signal(kline15, kline1h)
        atr_val = s['atr']

        if pos_side == 'LONG':
            equity = cash + pos_qty * bar['close']
        elif pos_side == 'SHORT':
            equity = cash + pos_qty*(state['entry_price']-bar['close']) + pos_qty*state['entry_price']
        else:
            equity = cash
        peak = max(peak, equity)

        if pos_side != 'NONE':
            entry, sl, tp = state['entry_price'], state['stop_loss'], state['take_profit']
            if pos_side == 'LONG' and bar['high']-entry >= cfg.TRAIL_BE_AT_ATR*atr_val:
                sl = max(sl, entry)
            elif pos_side == 'SHORT' and entry-bar['low'] >= cfg.TRAIL_BE_AT_ATR*atr_val:
                sl = min(sl, entry)
            state['stop_loss'] = sl

            flip = (pos_side == 'LONG' and s['direction']=='SHORT') or (pos_side=='SHORT' and s['direction']=='LONG')
            exit_price, reason = None, None
            if pos_side == 'LONG':
                if   bar['low']  <= sl: exit_price, reason = sl, 'SL'
                elif bar['high'] >= tp: exit_price, reason = tp, 'TP'
                elif flip:              exit_price, reason = bar['close'], 'FLIP'
            else:
                if   bar['high'] >= sl: exit_price, reason = sl, 'SL'
                elif bar['low']  <= tp: exit_price, reason = tp, 'TP'
                elif flip:              exit_price, reason = bar['close'], 'FLIP'

            if exit_price:
                if pos_side == 'LONG': pnl_g = (exit_price-entry)*pos_qty
                else:                  pnl_g = (entry-exit_price)*pos_qty
                fee_e = cfg.FEE_RATE * exit_price * pos_qty
                pnl_n = pnl_g - fee_e - state.get('entry_fee', 0)
                cash = cash + pos_qty*entry + pnl_n
                trades.append({'side': pos_side, 'pnl_net': pnl_n, 'reason': reason})
                d = bar['ts'].date()
                daily_pnl[d] = daily_pnl.get(d, 0) + pnl_n
                pos_side = 'NONE'; pos_qty = 0.0; state = {}
            eq.append(equity); continue

        d = bar['ts'].date()
        dp = daily_pnl.get(d, 0)
        if equity > 0 and dp < 0 and abs(dp)/equity >= cfg.DAILY_LOSS_HALT_PCT:
            eq.append(equity); continue
        if peak > 0 and (peak-equity)/peak >= cfg.MAX_DD_HALT_PCT:
            eq.append(equity); continue
        if bar['hour'] in cfg.LOW_LIQ_UTC_HOURS:
            eq.append(equity); continue

        if not (s['long_signal'] or s['short_signal']):
            eq.append(equity); continue

        side = 'LONG' if s['long_signal'] else 'SHORT'
        price = s['price']
        qty = strat.compute_qty(equity, price, atr_val)
        if qty < 0.001: eq.append(equity); continue

        cost = qty * price
        fee_e = cfg.FEE_RATE * cost
        if cost + fee_e > cash: eq.append(equity); continue

        if side == 'LONG':
            sl_p = price - cfg.ATR_SL_MULT*atr_val
            tp_p = price + cfg.ATR_TP_MULT*atr_val
        else:
            sl_p = price + cfg.ATR_SL_MULT*atr_val
            tp_p = price - cfg.ATR_TP_MULT*atr_val

        cash -= cost + fee_e
        pos_side = side; pos_qty = qty
        state = {'entry_price': price, 'stop_loss': sl_p, 'take_profit': tp_p,
                 'qty': qty, 'entry_fee': fee_e}
        eq.append(equity)

    if pos_side != 'NONE':
        last = bars_15m[-1]; entry = state['entry_price']
        if pos_side == 'LONG': pnl_g = (last['close']-entry)*pos_qty
        else:                  pnl_g = (entry-last['close'])*pos_qty
        fee_e = cfg.FEE_RATE * last['close'] * pos_qty
        pnl_n = pnl_g - fee_e - state.get('entry_fee', 0)
        cash = cash + pos_qty*entry + pnl_n
        trades.append({'side': pos_side, 'pnl_net': pnl_n, 'reason': 'EOD'})

    if not trades:
        return {'no_trades': True}
    n = len(trades)
    wins = [t for t in trades if t['pnl_net'] > 0]
    losses = [t for t in trades if t['pnl_net'] <= 0]
    wr = len(wins)/n*100
    gw = sum(t['pnl_net'] for t in wins); gl = abs(sum(t['pnl_net'] for t in losses))
    pf = gw/gl if gl > 0 else float('inf')
    peak = INITIAL; mdd = 0
    for e in eq:
        peak = max(peak, e); mdd = max(mdd, (peak-e)/peak)
    return {
        'final': cash, 'return_pct': (cash-INITIAL)/INITIAL*100,
        'trades': n, 'wr': wr, 'pf': pf if pf != float('inf') else 99,
        'max_dd': mdd*100,
    }


if __name__ == '__main__':
    print('Loading data once...')
    bars_15m = load_csv(DATA / 'btc_15m.csv')
    bars_1h  = load_csv(DATA / 'btc_1h.csv')

    # Configs to test — based on insight: stops too tight, entries too loose
    configs = [
        {'name': 'BASELINE',     'ATR_SL_MULT': 1.5, 'ATR_TP_MULT': 3.0, 'ADX_MIN': 20, 'EMA_PULLBACK_TOL': 0.003, 'RSI_LONG_MAX': 45, 'RSI_SHORT_MIN': 55},
        {'name': 'WIDER_STOPS',  'ATR_SL_MULT': 2.5, 'ATR_TP_MULT': 5.0, 'ADX_MIN': 20, 'EMA_PULLBACK_TOL': 0.003, 'RSI_LONG_MAX': 45, 'RSI_SHORT_MIN': 55},
        {'name': 'WIDE+ADX25',   'ATR_SL_MULT': 2.5, 'ATR_TP_MULT': 5.0, 'ADX_MIN': 25, 'EMA_PULLBACK_TOL': 0.003, 'RSI_LONG_MAX': 45, 'RSI_SHORT_MIN': 55},
        {'name': 'WIDE+TIGHT_PB','ATR_SL_MULT': 2.5, 'ATR_TP_MULT': 5.0, 'ADX_MIN': 25, 'EMA_PULLBACK_TOL': 0.0015, 'RSI_LONG_MAX': 45, 'RSI_SHORT_MIN': 55},
        {'name': 'WIDE+EXT_RSI', 'ATR_SL_MULT': 2.5, 'ATR_TP_MULT': 5.0, 'ADX_MIN': 25, 'EMA_PULLBACK_TOL': 0.003, 'RSI_LONG_MAX': 35, 'RSI_SHORT_MIN': 65},
        {'name': '3x_SL_4x_TP',  'ATR_SL_MULT': 3.0, 'ATR_TP_MULT': 4.0, 'ADX_MIN': 25, 'EMA_PULLBACK_TOL': 0.003, 'RSI_LONG_MAX': 40, 'RSI_SHORT_MIN': 60},
        {'name': 'BIG_TP',       'ATR_SL_MULT': 2.0, 'ATR_TP_MULT': 8.0, 'ADX_MIN': 25, 'EMA_PULLBACK_TOL': 0.0015, 'RSI_LONG_MAX': 40, 'RSI_SHORT_MIN': 60},
        {'name': 'STRICT',       'ATR_SL_MULT': 2.5, 'ATR_TP_MULT': 5.0, 'ADX_MIN': 28, 'EMA_PULLBACK_TOL': 0.0015, 'RSI_LONG_MAX': 35, 'RSI_SHORT_MIN': 65},
    ]

    print(f'\n{"name":<16} {"ret%":>8} {"trades":>7} {"wr%":>6} {"pf":>6} {"maxDD%":>7}')
    print('-' * 60)
    results = []
    for c in configs:
        name = c.pop('name')
        m = run_one(bars_15m, bars_1h, c)
        if m.get('no_trades'):
            print(f'{name:<16} {"NO TRADES"}')
            continue
        print(f'{name:<16} {m["return_pct"]:>+8.2f} {m["trades"]:>7} {m["wr"]:>5.1f}% {m["pf"]:>6.2f} {m["max_dd"]:>6.2f}%')
        results.append((name, m))

    print('\nBest by return:')
    for name, m in sorted(results, key=lambda x: -x[1]['return_pct'])[:3]:
        print(f'  {name}: {m["return_pct"]:+.2f}%  PF={m["pf"]:.2f}  WR={m["wr"]:.1f}%  trades={m["trades"]}')
