"""Quick sweep on the breakout strategy: tune trail/TP."""
import sys, csv
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import config as cfg
import strategy as strat
import importlib

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
                         'vol': float(r['volume']), 'hour': ts.hour})
    return rows


def to_kline(b):
    return [int(b['ts'].timestamp()*1000), b['open'], b['high'], b['low'],
            b['close'], b['vol'], 0, 0, 0, 0, 0, 0]


def find_htf(htf, ts):
    target = ts.timestamp(); lo, hi = 0, len(htf)-1
    while lo < hi:
        mid = (lo+hi+1)//2
        if htf[mid]['ts'].timestamp() <= target: lo = mid
        else: hi = mid - 1
    return lo


def run(bars15, bars1h, params, use_chandelier=True):
    for k, v in params.items(): setattr(cfg, k, v)
    importlib.reload(strat)

    cash = INITIAL
    side = 'NONE'; qty = 0.0; state = {}; trades = []; eq = []
    peak = INITIAL; dpnl = {}

    for idx in range(WARMUP, len(bars15)):
        bar = bars15[idx]
        k15 = [to_kline(b) for b in bars15[max(0,idx-249):idx+1]]
        h_idx = find_htf(bars1h, bar['ts'])
        k1h = [to_kline(b) for b in bars1h[max(0,h_idx-249):h_idx+1]]
        if len(k15) < 240 or len(k1h) < 200: continue

        s = strat.compute_signal(k15, k1h)
        atr_val = s['atr']

        if side == 'LONG':   equity = cash + qty * bar['close']
        elif side == 'SHORT':equity = cash + qty*(state['entry_price']-bar['close']) + qty*state['entry_price']
        else: equity = cash
        peak = max(peak, equity)

        if side != 'NONE':
            entry, sl, tp = state['entry_price'], state['stop_loss'], state['take_profit']
            highs = [float(k[2]) for k in k15]; lows = [float(k[3]) for k in k15]

            if use_chandelier:
                chand = strat.chandelier_stop(highs, lows, atr_val, side)
                if side == 'LONG':  sl = max(sl, chand)
                else:               sl = min(sl, chand) if sl > 0 else chand

            if side == 'LONG' and bar['high']-entry >= cfg.TRAIL_BE_AT_ATR*atr_val:
                sl = max(sl, entry)
            elif side == 'SHORT' and entry-bar['low'] >= cfg.TRAIL_BE_AT_ATR*atr_val:
                sl = min(sl, entry)
            state['stop_loss'] = sl

            flip = (side == 'LONG' and s['direction']=='SHORT') or (side=='SHORT' and s['direction']=='LONG')
            ex_p, reason = None, None
            if side == 'LONG':
                if   bar['low']  <= sl: ex_p, reason = sl, 'SL'
                elif bar['high'] >= tp: ex_p, reason = tp, 'TP'
                elif flip:              ex_p, reason = bar['close'], 'FLIP'
            else:
                if   bar['high'] >= sl: ex_p, reason = sl, 'SL'
                elif bar['low']  <= tp: ex_p, reason = tp, 'TP'
                elif flip:              ex_p, reason = bar['close'], 'FLIP'

            if ex_p:
                if side == 'LONG': pnl_g = (ex_p-entry)*qty
                else:              pnl_g = (entry-ex_p)*qty
                fee = cfg.FEE_RATE * ex_p * qty
                pnl_n = pnl_g - fee - state.get('entry_fee', 0)
                cash = cash + qty*entry + pnl_n
                trades.append({'side': side, 'pnl_net': pnl_n, 'reason': reason})
                d = bar['ts'].date(); dpnl[d] = dpnl.get(d, 0) + pnl_n
                side = 'NONE'; qty = 0.0; state = {}
            eq.append(equity); continue

        d = bar['ts'].date(); dp = dpnl.get(d, 0)
        if equity > 0 and dp < 0 and abs(dp)/equity >= cfg.DAILY_LOSS_HALT_PCT:
            eq.append(equity); continue
        if peak > 0 and (peak-equity)/peak >= cfg.MAX_DD_HALT_PCT:
            eq.append(equity); continue
        if bar['hour'] in cfg.LOW_LIQ_UTC_HOURS:
            eq.append(equity); continue

        if not (s['long_signal'] or s['short_signal']):
            eq.append(equity); continue

        new_side = 'LONG' if s['long_signal'] else 'SHORT'
        price = s['price']
        new_qty = strat.compute_qty(equity, price, atr_val)
        if new_qty < 0.001: eq.append(equity); continue

        cost = new_qty * price; fee = cfg.FEE_RATE * cost
        if cost + fee > cash: eq.append(equity); continue

        if new_side == 'LONG':
            sl_p = price - cfg.ATR_SL_MULT*atr_val
            tp_p = price + cfg.ATR_TP_MULT*atr_val
        else:
            sl_p = price + cfg.ATR_SL_MULT*atr_val
            tp_p = price - cfg.ATR_TP_MULT*atr_val

        cash -= cost + fee
        side = new_side; qty = new_qty
        state = {'entry_price': price, 'stop_loss': sl_p, 'take_profit': tp_p,
                 'qty': new_qty, 'entry_fee': fee}
        eq.append(equity)

    if side != 'NONE':
        last = bars15[-1]; entry = state['entry_price']
        if side == 'LONG': pnl_g = (last['close']-entry)*qty
        else:              pnl_g = (entry-last['close'])*qty
        fee = cfg.FEE_RATE * last['close'] * qty
        pnl_n = pnl_g - fee - state.get('entry_fee', 0)
        cash = cash + qty*entry + pnl_n
        trades.append({'side': side, 'pnl_net': pnl_n, 'reason': 'EOD'})

    if not trades: return {'no_trades': True}
    n = len(trades)
    wins = [t for t in trades if t['pnl_net']>0]; losses = [t for t in trades if t['pnl_net']<=0]
    gw = sum(t['pnl_net'] for t in wins); gl = abs(sum(t['pnl_net'] for t in losses))
    pf = gw/gl if gl > 0 else float('inf')
    peak = INITIAL; mdd = 0
    for e in eq:
        peak = max(peak, e); mdd = max(mdd, (peak-e)/peak)
    by_r = {}
    for t in trades: by_r[t['reason']] = by_r.get(t['reason'], 0) + 1
    return {
        'final': cash, 'return_pct': (cash-INITIAL)/INITIAL*100,
        'trades': n, 'wr': len(wins)/n*100,
        'pf': pf if pf != float('inf') else 99,
        'max_dd': mdd*100, 'by_r': by_r,
    }


if __name__ == '__main__':
    print('Loading data...')
    b15 = load_csv(DATA / 'btc_15m.csv')
    b1h = load_csv(DATA / 'btc_1h.csv')

    configs = [
        # (name, params, use_chandelier)
        ('CHAND10/2x',     {'CHANDELIER_PERIOD':10,'CHANDELIER_ATR_MULT':2.0,'ATR_TP_MULT':4.0}, True),
        ('CHAND20/3x',     {'CHANDELIER_PERIOD':20,'CHANDELIER_ATR_MULT':3.0,'ATR_TP_MULT':4.0}, True),
        ('CHAND30/3x',     {'CHANDELIER_PERIOD':30,'CHANDELIER_ATR_MULT':3.0,'ATR_TP_MULT':6.0}, True),
        ('NO_CHAND_TP4',   {'ATR_TP_MULT':4.0}, False),
        ('NO_CHAND_TP6',   {'ATR_TP_MULT':6.0}, False),
        ('NO_CHAND_TP8',   {'ATR_TP_MULT':8.0}, False),
        ('NO_CHAND_TP10',  {'ATR_TP_MULT':10.0}, False),
        ('SL2_TP6',        {'ATR_SL_MULT':2.0,'ATR_TP_MULT':6.0}, False),
        ('SL2_TP8',        {'ATR_SL_MULT':2.0,'ATR_TP_MULT':8.0}, False),
        ('SL2.5_TP10',     {'ATR_SL_MULT':2.5,'ATR_TP_MULT':10.0}, False),
    ]

    print(f'\n{"name":<14} {"ret%":>7} {"trades":>6} {"wr%":>5} {"pf":>5} {"maxDD%":>7} reasons')
    print('-' * 75)
    results = []
    for name, params, chand in configs:
        # reset to defaults each run
        for k, v in [('CHANDELIER_PERIOD',10),('CHANDELIER_ATR_MULT',2.0),
                     ('ATR_SL_MULT',1.5),('ATR_TP_MULT',4.0)]:
            setattr(cfg, k, v)
        m = run(b15, b1h, params, use_chandelier=chand)
        if m.get('no_trades'):
            print(f'{name:<14} NO TRADES'); continue
        print(f'{name:<14} {m["return_pct"]:>+7.2f} {m["trades"]:>6} {m["wr"]:>4.1f}% {m["pf"]:>5.2f} {m["max_dd"]:>6.2f}% {m["by_r"]}')
        results.append((name, m))

    print('\nTop 3 by return:')
    for name, m in sorted(results, key=lambda x: -x[1]['return_pct'])[:3]:
        print(f'  {name}: {m["return_pct"]:+.2f}%  PF={m["pf"]:.2f}  WR={m["wr"]:.1f}%  trades={m["trades"]}  DD={m["max_dd"]:.1f}%')
