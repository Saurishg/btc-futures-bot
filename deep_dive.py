"""
DEEP DIVE — capture every trade with full context, then segment win/loss
to identify what's actually causing losses.

Captures per trade:
  - entry/exit/PnL
  - market state at entry: RSI, ADX, ATR%, vol_ratio, donchian_dist
  - hour of day, day of week
  - what happened in the 5 bars BEFORE entry (was breakout already late?)
  - what happened in the 5 bars AFTER entry (immediate whipsaw?)
  - max favorable / max adverse excursion
"""
import sys, csv, math
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import config as cfg
import strategy as strat
from indicators import ema, rsi, atr, adx

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
                         'vol': float(r['volume']), 'hour': ts.hour, 'dow': ts.weekday()})
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


def run_with_logging(bars15, bars1h):
    cash = INITIAL
    side = 'NONE'; qty = 0.0; state = {}; trades = []

    for idx in range(WARMUP, len(bars15)):
        bar = bars15[idx]
        k15 = [to_kline(b) for b in bars15[max(0,idx-249):idx+1]]
        h_idx = find_htf(bars1h, bar['ts'])
        k1h = [to_kline(b) for b in bars1h[max(0,h_idx-249):h_idx+1]]
        if len(k15) < 240 or len(k1h) < 200: continue

        s = strat.compute_signal(k15, k1h)
        atr_val = s['atr']

        if side != 'NONE':
            entry = state['entry_price']; sl = state['stop_loss']; tp = state['take_profit']
            highs = [float(k[2]) for k in k15]; lows = [float(k[3]) for k in k15]

            chand = strat.chandelier_stop(highs, lows, atr_val, side)
            if side == 'LONG':
                sl = max(sl, chand)
                if bar['high']-entry >= cfg.TRAIL_BE_AT_ATR*atr_val: sl = max(sl, entry)
            else:
                sl = min(sl, chand) if sl > 0 else chand
                if entry-bar['low'] >= cfg.TRAIL_BE_AT_ATR*atr_val: sl = min(sl, entry)
            state['stop_loss'] = sl

            # Track MFE/MAE
            if side == 'LONG':
                state['mfe'] = max(state.get('mfe', 0), bar['high'] - entry)
                state['mae'] = min(state.get('mae', 0), bar['low']  - entry)
            else:
                state['mfe'] = max(state.get('mfe', 0), entry - bar['low'])
                state['mae'] = min(state.get('mae', 0), entry - bar['high'])

            ex_p, reason = None, None
            if side == 'LONG':
                if   bar['low']  <= sl: ex_p, reason = sl, 'SL'
                elif bar['high'] >= tp: ex_p, reason = tp, 'TP'
            else:
                if   bar['high'] >= sl: ex_p, reason = sl, 'SL'
                elif bar['low']  <= tp: ex_p, reason = tp, 'TP'

            if ex_p:
                if side == 'LONG': pnl_g = (ex_p-entry)*qty
                else:              pnl_g = (entry-ex_p)*qty
                fee = cfg.FEE_RATE * ex_p * qty
                pnl_n = pnl_g - fee - state.get('entry_fee', 0)
                cash = cash + qty*entry + pnl_n
                trades.append({
                    **state, 'side': side, 'exit_idx': idx, 'exit_price': ex_p,
                    'exit_ts': bar['ts'], 'pnl_net': pnl_n, 'reason': reason,
                    'r_multiple': pnl_n / state.get('risk_dollars', 1),
                })
                side = 'NONE'; qty = 0.0; state = {}
            continue

        if not (s['long_signal'] or s['short_signal']): continue
        new_side = 'LONG' if s['long_signal'] else 'SHORT'
        price = s['price']
        new_qty = strat.compute_qty(cash, price, atr_val)
        if new_qty < 0.001: continue
        cost = new_qty * price; fee = cfg.FEE_RATE * cost
        if cost + fee > cash: continue

        if new_side == 'LONG':
            sl_p = price - cfg.ATR_SL_MULT*atr_val
            tp_p = price + cfg.ATR_TP_MULT*atr_val
        else:
            sl_p = price + cfg.ATR_SL_MULT*atr_val
            tp_p = price - cfg.ATR_TP_MULT*atr_val

        # Capture context at entry
        closes = [float(k[4]) for k in k15]
        prev5_change = (closes[-1] - closes[-6]) / closes[-6] * 100  # pct change last 5 bars
        donch_break_pct = (
            (price - s['donchian_high']) / s['donchian_high'] * 100 if new_side == 'LONG'
            else (s['donchian_low'] - price) / s['donchian_low'] * 100
        )
        risk_dollars = abs(price - sl_p) * new_qty + fee

        cash -= cost + fee
        side = new_side; qty = new_qty
        state = {
            'entry_idx': idx, 'entry_ts': bar['ts'], 'entry_price': price,
            'stop_loss': sl_p, 'take_profit': tp_p, 'qty': new_qty,
            'entry_fee': fee, 'risk_dollars': risk_dollars,
            'rsi': s['rsi'], 'adx': s['adx'], 'atr': atr_val,
            'atr_pct': atr_val / price * 100,
            'hour': bar['hour'], 'dow': bar['dow'],
            'prev5_change_pct': prev5_change,
            'donch_break_pct': donch_break_pct,
            'mfe': 0, 'mae': 0,
        }

    return trades


def analyze(trades):
    if not trades:
        print('No trades.'); return

    n = len(trades)
    wins = [t for t in trades if t['pnl_net'] > 0]
    losses = [t for t in trades if t['pnl_net'] <= 0]

    print(f'\n{"="*70}\nDEEP DIVE — {n} trades total\n{"="*70}')
    print(f'  Wins: {len(wins)}  Losses: {len(losses)}  WR: {len(wins)/n*100:.1f}%')

    def stats(tlist, key):
        if not tlist: return None
        vals = [t[key] for t in tlist]
        return sum(vals)/len(vals), min(vals), max(vals)

    print(f'\n--- Entry conditions: WIN vs LOSS averages ---')
    fields = ['rsi', 'adx', 'atr_pct', 'prev5_change_pct', 'donch_break_pct']
    print(f'{"field":<22} {"WIN avg":>10} {"LOSS avg":>10} {"diff":>10}')
    for f in fields:
        w = stats(wins, f); l = stats(losses, f)
        if w and l:
            print(f'{f:<22} {w[0]:>10.2f} {l[0]:>10.2f} {w[0]-l[0]:>+10.2f}')

    print(f'\n--- By side ---')
    longs  = [t for t in trades if t['side'] == 'LONG']
    shorts = [t for t in trades if t['side'] == 'SHORT']
    for label, lst in [('LONG', longs), ('SHORT', shorts)]:
        if not lst: continue
        w = sum(1 for t in lst if t['pnl_net'] > 0)
        total_pnl = sum(t['pnl_net'] for t in lst)
        print(f'  {label}: {len(lst)} trades, {w}/{len(lst)} wins ({w/len(lst)*100:.1f}%), '
              f'PnL ${total_pnl:+.2f}')

    print(f'\n--- By exit reason ---')
    by_reason = {}
    for t in trades:
        by_reason.setdefault(t['reason'], []).append(t)
    for reason, lst in by_reason.items():
        avg = sum(t['pnl_net'] for t in lst)/len(lst)
        total = sum(t['pnl_net'] for t in lst)
        print(f'  {reason}: {len(lst)} trades, avg ${avg:+.2f}, total ${total:+.2f}')

    print(f'\n--- By hour of day (UTC) ---')
    by_hour = {}
    for t in trades:
        by_hour.setdefault(t['hour'], []).append(t)
    print(f'{"hour":>4} {"n":>4} {"wr%":>5} {"avg_pnl":>10} {"total_pnl":>10}')
    for hour in sorted(by_hour.keys()):
        lst = by_hour[hour]
        w = sum(1 for t in lst if t['pnl_net'] > 0)
        avg = sum(t['pnl_net'] for t in lst)/len(lst)
        total = sum(t['pnl_net'] for t in lst)
        print(f'{hour:>4} {len(lst):>4} {w/len(lst)*100:>4.1f}% {avg:>+10.2f} {total:>+10.2f}')

    print(f'\n--- ADX bucket performance ---')
    buckets = [(0,25), (25,35), (35,50), (50,200)]
    for lo, hi in buckets:
        lst = [t for t in trades if lo <= t['adx'] < hi]
        if not lst: continue
        w = sum(1 for t in lst if t['pnl_net'] > 0)
        total = sum(t['pnl_net'] for t in lst)
        print(f'  ADX [{lo:>3}-{hi:>3}): {len(lst):>3} trades, WR {w/len(lst)*100:.1f}%, total ${total:+.2f}')

    print(f'\n--- Donchian breakout strength (% past channel) ---')
    buckets = [(0,0.05), (0.05,0.15), (0.15,0.5), (0.5,5)]
    for lo, hi in buckets:
        lst = [t for t in trades if lo <= t['donch_break_pct'] < hi]
        if not lst: continue
        w = sum(1 for t in lst if t['pnl_net'] > 0)
        total = sum(t['pnl_net'] for t in lst)
        print(f'  break_pct [{lo:.2f}-{hi:.2f}%): {len(lst):>3} trades, WR {w/len(lst)*100:.1f}%, total ${total:+.2f}')

    print(f'\n--- Prev-5-bar momentum at entry ---')
    print('  (positive = market already moved in our direction before we entered)')
    buckets = [(-5,-1), (-1,0), (0,1), (1,3), (3,10)]
    for lo, hi in buckets:
        # For SHORTS, flip sign so "+" means already moving down (right direction)
        lst = []
        for t in trades:
            d = t['prev5_change_pct'] if t['side']=='LONG' else -t['prev5_change_pct']
            if lo <= d < hi: lst.append(t)
        if not lst: continue
        w = sum(1 for t in lst if t['pnl_net'] > 0)
        total = sum(t['pnl_net'] for t in lst)
        print(f'  prev5 [{lo:>+4.1f},{hi:>+4.1f})%: {len(lst):>3} trades, WR {w/len(lst)*100:.1f}%, total ${total:+.2f}')

    print(f'\n--- MFE / MAE analysis ---')
    print('  (MFE=max favorable, MAE=max adverse, both in dollars)')
    avg_mfe_w = sum(t['mfe']*t['qty'] for t in wins)/len(wins) if wins else 0
    avg_mae_w = sum(t['mae']*t['qty'] for t in wins)/len(wins) if wins else 0
    avg_mfe_l = sum(t['mfe']*t['qty'] for t in losses)/len(losses) if losses else 0
    avg_mae_l = sum(t['mae']*t['qty'] for t in losses)/len(losses) if losses else 0
    print(f'  Wins   — avg MFE: ${avg_mfe_w:+.2f}  avg MAE: ${avg_mae_w:+.2f}')
    print(f'  Losses — avg MFE: ${avg_mfe_l:+.2f}  avg MAE: ${avg_mae_l:+.2f}')

    # Critical: in losses, did we get any profit at all before reversing?
    lost_after_profit = [t for t in losses if t['mfe']*t['qty'] > 5]
    print(f'\n  Losses that went +$5 favorable before reversing to SL: {len(lost_after_profit)}/{len(losses)}')
    if lost_after_profit:
        avg_mfe = sum(t['mfe']*t['qty'] for t in lost_after_profit)/len(lost_after_profit)
        print(f'    Avg max favorable on these: ${avg_mfe:+.2f}')


if __name__ == '__main__':
    print('Loading 8 months of data...')
    b15 = load_csv(DATA / 'btc_15m.csv')
    b1h = load_csv(DATA / 'btc_1h.csv')
    print(f'  {len(b15)} bars 15m, {len(b1h)} bars 1h')

    print('Running instrumented backtest...')
    trades = run_with_logging(b15, b1h)
    analyze(trades)
