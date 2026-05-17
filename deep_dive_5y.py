"""
5-YEAR DEEP DIVE — find where the bot wins consistently across regimes.

Segments trades by:
  - Calendar year (does any year show edge?)
  - BTC regime (B&H positive vs negative quarters)
  - Direction (long vs short)
  - ADX bucket (5 buckets)
  - RSI at entry (5 buckets)
  - Hour of day
  - Volatility regime (ATR%-rank quintile)
  - MFE/MAE distribution

Goal: identify ANY consistent profitable subset.
If we find one (e.g., "shorts when ADX>40 + RSI<40 are profitable in EVERY year"),
that's a real edge to exploit.
"""
import sys, csv, math
from datetime import datetime
from collections import defaultdict
from pathlib import Path
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
                         'vol': float(r['volume']), 'hour': ts.hour, 'dow': ts.weekday(),
                         'year': ts.year, 'quarter': (ts.month-1)//3 + 1})
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
    """Faster backtest that captures all trade context."""
    cash = INITIAL
    side = 'NONE'; qty = 0.0; state = {}; trades = []

    # Pre-compute HTF index map (15m bar idx → 1h bar idx) for speed
    htf_idx_map = []
    h_idx = 0
    for b in bars15:
        while h_idx + 1 < len(bars1h) and bars1h[h_idx+1]['ts'].timestamp() <= b['ts'].timestamp():
            h_idx += 1
        htf_idx_map.append(h_idx)

    for idx in range(WARMUP, len(bars15)):
        bar = bars15[idx]
        k15 = [to_kline(b) for b in bars15[max(0,idx-249):idx+1]]
        h_idx = htf_idx_map[idx]
        k1h = [to_kline(b) for b in bars1h[max(0,h_idx-249):h_idx+1]]
        if len(k15) < 240 or len(k1h) < 200: continue

        s = strat.compute_signal(k15, k1h)
        atr_val = s['atr']

        if side != 'NONE':
            entry = state['entry_price']; sl = state['stop_loss']; tp = state['take_profit']
            highs = [float(k[2]) for k in k15]; lows = [float(k[3]) for k in k15]

            if side == 'LONG':
                profit_atr = (bar['close'] - entry) / atr_val if atr_val > 0 else 0
            else:
                profit_atr = (entry - bar['close']) / atr_val if atr_val > 0 else 0

            if profit_atr >= cfg.CHANDELIER_ACTIVATE_AT_ATR:
                chand = strat.chandelier_stop(highs, lows, atr_val, side)
                if side == 'LONG':  sl = max(sl, chand)
                else:               sl = min(sl, chand) if sl > 0 else chand

            if side == 'LONG':
                if bar['high']-entry >= cfg.TRAIL_BE_AT_ATR*atr_val: sl = max(sl, entry)
                state['mfe'] = max(state.get('mfe', 0), bar['high'] - entry)
                state['mae'] = min(state.get('mae', 0), bar['low']  - entry)
            else:
                if entry-bar['low'] >= cfg.TRAIL_BE_AT_ATR*atr_val: sl = min(sl, entry) if sl > 0 else entry
                state['mfe'] = max(state.get('mfe', 0), entry - bar['low'])
                state['mae'] = min(state.get('mae', 0), entry - bar['high'])
            state['stop_loss'] = sl

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
                trades.append({**state, 'side': side, 'exit_price': ex_p,
                               'exit_ts': bar['ts'], 'pnl_net': pnl_n, 'reason': reason})
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

        cash -= cost + fee
        side = new_side; qty = new_qty
        state = {
            'entry_ts': bar['ts'], 'entry_price': price, 'qty': new_qty,
            'stop_loss': sl_p, 'take_profit': tp_p, 'entry_fee': fee,
            'rsi': s['rsi'], 'adx': s['adx'], 'atr_pct': atr_val/price*100,
            'hour': bar['hour'], 'year': bar['year'], 'quarter': bar['quarter'],
            'donch_break_pct': (price-s['donchian_high'])/s['donchian_high']*100 if new_side=='LONG'
                               else (s['donchian_low']-price)/s['donchian_low']*100,
            'mfe': 0, 'mae': 0,
        }

    return trades


def segment_pnl(trades, key_func, label):
    """Group trades by some key and compute stats per group."""
    groups = defaultdict(list)
    for t in trades:
        groups[key_func(t)].append(t)
    print(f'\n--- By {label} ---')
    print(f'{"key":<20} {"n":>4} {"wr%":>5} {"avg_pnl":>9} {"total_pnl":>10} {"PF":>5}')
    rows = []
    for key in sorted(groups.keys()):
        lst = groups[key]
        if not lst: continue
        wins = [t for t in lst if t['pnl_net'] > 0]
        gw = sum(t['pnl_net'] for t in wins)
        gl = abs(sum(t['pnl_net'] for t in lst if t['pnl_net'] <= 0))
        pf = gw/gl if gl > 0 else 99
        avg = sum(t['pnl_net'] for t in lst)/len(lst)
        total = sum(t['pnl_net'] for t in lst)
        wr = len(wins)/len(lst)*100
        print(f'{str(key):<20} {len(lst):>4} {wr:>4.1f}% {avg:>+9.2f} {total:>+10.2f} {pf:>5.2f}')
        rows.append({'key': key, 'n': len(lst), 'wr': wr, 'pf': pf, 'total': total})
    return rows


def analyze(trades):
    n = len(trades)
    wins = [t for t in trades if t['pnl_net'] > 0]
    losses = [t for t in trades if t['pnl_net'] <= 0]
    total = sum(t['pnl_net'] for t in trades)

    print(f'\n{"="*70}\n5-YEAR DEEP DIVE — {n} trades\n{"="*70}')
    print(f'Wins {len(wins)}  Losses {len(losses)}  WR {len(wins)/n*100:.1f}%  Total PnL ${total:+.2f}')

    # By year
    segment_pnl(trades, lambda t: t['year'], 'YEAR')

    # By side
    segment_pnl(trades, lambda t: t['side'], 'SIDE')

    # By year × side
    segment_pnl(trades, lambda t: f"{t['year']}-{t['side']}", 'YEAR × SIDE')

    # By ADX bucket
    def adx_bucket(t):
        a = t['adx']
        if a < 25: return '0-25'
        if a < 35: return '25-35'
        if a < 45: return '35-45'
        if a < 60: return '45-60'
        return '60+'
    segment_pnl(trades, adx_bucket, 'ADX BUCKET')

    # By RSI at entry
    def rsi_bucket(t):
        r = t['rsi']
        if r < 30: return '0-30'
        if r < 45: return '30-45'
        if r < 55: return '45-55'
        if r < 70: return '55-70'
        return '70+'
    segment_pnl(trades, rsi_bucket, 'RSI AT ENTRY')

    # By hour of day
    print('\n--- By HOUR (UTC) — looking for time-of-day edge ---')
    by_hr = defaultdict(list)
    for t in trades: by_hr[t['hour']].append(t)
    print(f'{"hour":>4} {"n":>4} {"wr%":>5} {"total":>10} {"PF":>5}')
    for h in sorted(by_hr.keys()):
        lst = by_hr[h]
        wins_h = [t for t in lst if t['pnl_net']>0]
        gw = sum(t['pnl_net'] for t in wins_h)
        gl = abs(sum(t['pnl_net'] for t in lst if t['pnl_net']<=0))
        pf = gw/gl if gl > 0 else 99
        print(f'{h:>4} {len(lst):>4} {len(wins_h)/len(lst)*100:>4.1f}% {sum(t["pnl_net"] for t in lst):>+10.2f} {pf:>5.2f}')

    # MFE/MAE
    print('\n--- MFE / MAE (key: did losers go favorable first?) ---')
    if wins and losses:
        avg_mfe_w = sum(t['mfe']*t['qty'] for t in wins)/len(wins)
        avg_mae_w = sum(t['mae']*t['qty'] for t in wins)/len(wins)
        avg_mfe_l = sum(t['mfe']*t['qty'] for t in losses)/len(losses)
        avg_mae_l = sum(t['mae']*t['qty'] for t in losses)/len(losses)
        print(f'  Wins   — MFE ${avg_mfe_w:+.2f}  MAE ${avg_mae_w:+.2f}')
        print(f'  Losses — MFE ${avg_mfe_l:+.2f}  MAE ${avg_mae_l:+.2f}')
        lost_after_profit = [t for t in losses if t['mfe']*t['qty'] > 5]
        print(f'  Losers that went +$5 then reversed: {len(lost_after_profit)}/{len(losses)} ({len(lost_after_profit)/len(losses)*100:.1f}%)')

    # Critical: per-year PF for SHORTS vs LONGS
    print('\n--- PER-YEAR consistency check (looking for ANY persistent edge) ---')
    years = sorted(set(t['year'] for t in trades))
    sides = ['LONG', 'SHORT']
    print(f'{"":<8}', end='')
    for s in sides: print(f'{s:>10}', end=' ')
    print()
    for y in years:
        print(f'{y:<8}', end='')
        for s in sides:
            lst = [t for t in trades if t['year']==y and t['side']==s]
            if not lst:
                print(f'{"--":>10}', end=' ')
            else:
                tot = sum(t['pnl_net'] for t in lst)
                print(f'{tot:>+9.0f} ', end=' ')
        print()


if __name__ == '__main__':
    print('Loading 5y data (this takes a sec)...')
    b15 = load_csv(DATA / 'btc_15m.csv')
    b1h = load_csv(DATA / 'btc_1h.csv')
    print(f'  {len(b15)} × 15m, {len(b1h)} × 1h, range {b15[0]["ts"].date()} → {b15[-1]["ts"].date()}')
    print('Running instrumented backtest (≈7 min)...')
    trades = run_with_logging(b15, b1h)
    analyze(trades)
