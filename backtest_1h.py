"""
1H BACKTEST + DEEP DIVE — same breakout strategy, 1h entry / 4h HTF.
Runs backtest, then segments trades to find where it wins/loses.
"""
import sys, csv, math
from datetime import datetime
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from indicators import ema, ema_arr, rsi, atr, adx

DATA = Path(__file__).parent / 'data'
INITIAL = 10_000.0
WARMUP = 250

# ── Strategy params (adapted for 1h) ────────────────────────────────────
HTF_EMA_FAST = 50
HTF_EMA_SLOW = 200
BREAKOUT_PERIOD = 20
BREAKOUT_VOL_MULT = 1.2
MIN_CANDLE_BODY_ATR = 0.5
ADX_MIN = 35
ATR_PERIOD = 14
RSI_LONG_MAX = 75
RSI_SHORT_MIN = 25
ATR_SL_MULT = 2.5
ATR_TP_MULT = 5.0
CHANDELIER_PERIOD = 20
CHANDELIER_ATR_MULT = 3.0
TRAIL_BE_AT_ATR = 1.0
CHANDELIER_ACTIVATE_AT_ATR = 1.5
SCALE_OUT_AT_ATR = 1.0
SCALE_OUT_FRACTION = 0.5
FEE_RATE = 0.0004


def load_csv(p):
    rows = []
    with open(p) as f:
        for r in csv.DictReader(f):
            ts = datetime.fromisoformat(r['timestamp'])
            rows.append({'ts': ts, 'open': float(r['open']), 'high': float(r['high']),
                         'low': float(r['low']), 'close': float(r['close']),
                         'vol': float(r['volume']), 'hour': ts.hour, 'year': ts.year})
    return rows


def to_kline(b):
    return [int(b['ts'].timestamp()*1000), b['open'], b['high'], b['low'],
            b['close'], b['vol'], 0, 0, 0, 0, 0, 0]


def htf_direction(htf_closes):
    if len(htf_closes) < HTF_EMA_SLOW: return 'NONE'
    ef = ema(htf_closes[-HTF_EMA_FAST*3:], HTF_EMA_FAST)
    es = ema(htf_closes, HTF_EMA_SLOW)
    if ef > es * 1.001: return 'LONG'
    if ef < es * 0.999: return 'SHORT'
    return 'NONE'


def adx_rising(highs, lows, closes, n=14, lookback=5):
    cur = adx(highs, lows, closes, n)
    if len(closes) < lookback + 30: return False
    prev = adx(highs[:-lookback], lows[:-lookback], closes[:-lookback], n)
    return cur > prev


def chandelier_stop(highs, lows, atr_val, side):
    if side == 'LONG':
        return max(highs[-CHANDELIER_PERIOD:]) - CHANDELIER_ATR_MULT * atr_val
    return min(lows[-CHANDELIER_PERIOD:]) + CHANDELIER_ATR_MULT * atr_val


def find_htf(htf, ts):
    target = ts.timestamp(); lo, hi = 0, len(htf)-1
    while lo < hi:
        mid = (lo+hi+1)//2
        if htf[mid]['ts'].timestamp() <= target: lo = mid
        else: hi = mid - 1
    return lo


def run(bars1h, bars4h):
    cash = INITIAL
    side = 'NONE'; qty = 0.0; state = {}; trades = []; eq = []
    peak = INITIAL

    # Pre-compute HTF index
    htf_map = []
    h_idx = 0
    for b in bars1h:
        while h_idx+1 < len(bars4h) and bars4h[h_idx+1]['ts'].timestamp() <= b['ts'].timestamp():
            h_idx += 1
        htf_map.append(h_idx)

    for idx in range(WARMUP, len(bars1h)):
        bar = bars1h[idx]
        closes = [bars1h[i]['close'] for i in range(max(0,idx-249), idx+1)]
        highs  = [bars1h[i]['high']  for i in range(max(0,idx-249), idx+1)]
        lows   = [bars1h[i]['low']   for i in range(max(0,idx-249), idx+1)]
        opens  = [bars1h[i]['open']  for i in range(max(0,idx-249), idx+1)]
        vols   = [bars1h[i]['vol']   for i in range(max(0,idx-249), idx+1)]

        h_end = htf_map[idx]
        htf_closes = [bars4h[i]['close'] for i in range(max(0,h_end-249), h_end+1)]
        if len(closes) < 240 or len(htf_closes) < 200: continue

        direction = htf_direction(htf_closes)
        atr_val = atr(highs, lows, closes, ATR_PERIOD)
        rsi_val = rsi(closes, 14)
        adx_val = adx(highs, lows, closes, 14)
        adx_up  = adx_rising(highs, lows, closes)
        price   = closes[-1]

        equity = cash + (qty * price if side == 'LONG' else
                         qty * (state.get('entry_price',0) - price) + qty * state.get('entry_price',0) if side == 'SHORT' else 0)
        peak = max(peak, equity)

        # ── In position ──────────────────────────────────────────
        if side != 'NONE':
            entry = state['entry_price']; sl = state['stop_loss']; tp = state['take_profit']

            if side == 'LONG':
                profit_atr = (bar['high'] - entry) / atr_val if atr_val > 0 else 0
                profit_atr_close = (bar['close'] - entry) / atr_val if atr_val > 0 else 0
            else:
                profit_atr = (entry - bar['low']) / atr_val if atr_val > 0 else 0
                profit_atr_close = (entry - bar['close']) / atr_val if atr_val > 0 else 0

            # Scale-out
            if not state.get('scaled_out') and profit_atr >= SCALE_OUT_AT_ATR:
                scale_qty = qty * SCALE_OUT_FRACTION
                if side == 'LONG':
                    sp = entry + SCALE_OUT_AT_ATR * atr_val
                    pnl_s = (sp - entry) * scale_qty
                else:
                    sp = entry - SCALE_OUT_AT_ATR * atr_val
                    pnl_s = (entry - sp) * scale_qty
                fee_s = FEE_RATE * sp * scale_qty
                pnl_s -= fee_s + state.get('entry_fee',0) * SCALE_OUT_FRACTION
                cash += scale_qty * entry + pnl_s
                qty -= scale_qty
                state['scaled_out'] = True; state['scale_pnl'] = pnl_s
                sl = entry; state['stop_loss'] = sl

            # Chandelier
            if profit_atr_close >= CHANDELIER_ACTIVATE_AT_ATR:
                chand = chandelier_stop(highs, lows, atr_val, side)
                if side == 'LONG': sl = max(sl, chand)
                else: sl = min(sl, chand) if sl > 0 else chand

            # BE
            if side == 'LONG' and bar['high']-entry >= TRAIL_BE_AT_ATR*atr_val:
                sl = max(sl, entry)
            elif side == 'SHORT' and entry-bar['low'] >= TRAIL_BE_AT_ATR*atr_val:
                sl = min(sl, entry) if sl > 0 else entry
            state['stop_loss'] = sl

            # Track MFE/MAE
            if side == 'LONG':
                state['mfe'] = max(state.get('mfe',0), bar['high']-entry)
                state['mae'] = min(state.get('mae',0), bar['low']-entry)
            else:
                state['mfe'] = max(state.get('mfe',0), entry-bar['low'])
                state['mae'] = min(state.get('mae',0), entry-bar['high'])

            ex_p, reason = None, None
            if side == 'LONG':
                if   bar['low']  <= sl: ex_p, reason = sl, 'SL'
                elif bar['high'] >= tp: ex_p, reason = tp, 'TP'
            else:
                if   bar['high'] >= sl: ex_p, reason = sl, 'SL'
                elif bar['low']  <= tp: ex_p, reason = tp, 'TP'

            if ex_p:
                rem_fee = state.get('entry_fee',0) * (1-SCALE_OUT_FRACTION if state.get('scaled_out') else 1)
                if side == 'LONG': pnl_g = (ex_p-entry)*qty
                else:              pnl_g = (entry-ex_p)*qty
                fee_e = FEE_RATE * ex_p * qty
                pnl_n = pnl_g - fee_e - rem_fee + state.get('scale_pnl', 0)
                cash += qty*entry + (pnl_g - fee_e - rem_fee)
                trades.append({
                    'side': side, 'pnl_net': pnl_n, 'reason': reason,
                    'year': bar['year'], 'hour': bar['hour'],
                    'adx': state.get('adx',0), 'rsi': state.get('rsi',0),
                    'mfe': state.get('mfe',0), 'mae': state.get('mae',0),
                    'qty': state.get('init_qty', qty),
                    'scaled': state.get('scaled_out', False),
                })
                side = 'NONE'; qty = 0.0; state = {}
            eq.append(equity); continue

        # ── No position: entry ───────────────────────────────────
        donch_hi = max(highs[-BREAKOUT_PERIOD-1:-1])
        donch_lo = min(lows[-BREAKOUT_PERIOD-1:-1])
        body = abs(closes[-1] - opens[-1])
        decisive = atr_val > 0 and body >= MIN_CANDLE_BODY_ATR * atr_val
        bull_candle = decisive and closes[-1] > opens[-1]
        bear_candle = decisive and closes[-1] < opens[-1]
        vol_ma = sum(vols[-21:-1])/20 if len(vols) >= 21 else vols[-1]
        vol_ok = vols[-1] >= vol_ma * BREAKOUT_VOL_MULT

        long_sig = (direction=='LONG' and closes[-1]>donch_hi and bull_candle
                    and adx_val>=ADX_MIN and adx_up and vol_ok and rsi_val<RSI_LONG_MAX)
        short_sig = (direction=='SHORT' and closes[-1]<donch_lo and bear_candle
                     and adx_val>=ADX_MIN and adx_up and vol_ok and rsi_val>RSI_SHORT_MIN)

        if not (long_sig or short_sig):
            eq.append(equity); continue

        new_side = 'LONG' if long_sig else 'SHORT'
        sl_dist = ATR_SL_MULT * atr_val
        qty_r = (equity * 0.01) / sl_dist if sl_dist > 0 else 0
        qty_c = (equity * 0.30) / price
        new_qty = round(min(qty_r, qty_c), 3)
        if new_qty < 0.001: eq.append(equity); continue
        cost = new_qty * price; fee = FEE_RATE * cost
        if cost + fee > cash: eq.append(equity); continue

        if new_side == 'LONG':
            sl_p = price - ATR_SL_MULT*atr_val; tp_p = price + ATR_TP_MULT*atr_val
        else:
            sl_p = price + ATR_SL_MULT*atr_val; tp_p = price - ATR_TP_MULT*atr_val

        cash -= cost + fee
        side = new_side; qty = new_qty
        state = {'entry_price': price, 'stop_loss': sl_p, 'take_profit': tp_p,
                 'entry_fee': fee, 'init_qty': new_qty,
                 'adx': adx_val, 'rsi': rsi_val, 'mfe': 0, 'mae': 0}
        eq.append(equity)

    # Close open
    if side != 'NONE':
        last = bars1h[-1]; entry = state['entry_price']
        if side == 'LONG': pnl_g = (last['close']-entry)*qty
        else:              pnl_g = (entry-last['close'])*qty
        fee = FEE_RATE * last['close'] * qty
        pnl_n = pnl_g - fee + state.get('scale_pnl', 0)
        cash += qty*entry + pnl_g - fee
        trades.append({'side': side, 'pnl_net': pnl_n, 'reason': 'EOD',
                       'year': last['year'], 'hour': last['hour'],
                       'adx': state.get('adx',0), 'rsi': state.get('rsi',0),
                       'mfe': state.get('mfe',0), 'mae': state.get('mae',0),
                       'qty': state.get('init_qty', qty), 'scaled': state.get('scaled_out', False)})

    return cash, trades, eq


def analyze(final, trades, eq, bh):
    n = len(trades)
    if not n: print('No trades.'); return
    wins = [t for t in trades if t['pnl_net']>0]
    losses = [t for t in trades if t['pnl_net']<=0]
    total = sum(t['pnl_net'] for t in trades)
    gw = sum(t['pnl_net'] for t in wins); gl = abs(sum(t['pnl_net'] for t in losses))
    pf = gw/gl if gl > 0 else 99
    peak_eq = INITIAL; mdd = 0
    for e in eq:
        peak_eq = max(peak_eq, e); mdd = max(mdd, (peak_eq-e)/peak_eq)

    print(f'\n{"="*60}\n1H BACKTEST — 5 YEARS\n{"="*60}')
    print(f'Final: ${final:,.2f} | Return: {(final-INITIAL)/INITIAL*100:+.2f}% | B&H: {bh:+.2f}%')
    print(f'Trades: {n} | WR: {len(wins)/n*100:.1f}% | PF: {pf:.2f} | Sharpe: n/a')
    print(f'Max DD: {mdd*100:.2f}% | Avg win: ${gw/len(wins) if wins else 0:.2f} | Avg loss: ${gl/len(losses) if losses else 0:.2f}')

    # By year
    print(f'\n--- By YEAR ---')
    by_year = defaultdict(list)
    for t in trades: by_year[t['year']].append(t)
    print(f'{"year":<6} {"n":>4} {"wr%":>5} {"total":>10} {"PF":>5}')
    for y in sorted(by_year):
        lst = by_year[y]; w = [t for t in lst if t['pnl_net']>0]
        gw_y = sum(t['pnl_net'] for t in w); gl_y = abs(sum(t['pnl_net'] for t in lst if t['pnl_net']<=0))
        print(f'{y:<6} {len(lst):>4} {len(w)/len(lst)*100:>4.1f}% {sum(t["pnl_net"] for t in lst):>+10.2f} {gw_y/gl_y if gl_y else 99:>5.2f}')

    # By side
    print(f'\n--- By SIDE ---')
    for s in ['LONG','SHORT']:
        lst = [t for t in trades if t['side']==s]
        if not lst: continue
        w = [t for t in lst if t['pnl_net']>0]
        print(f'  {s}: {len(lst)} trades, WR {len(w)/len(lst)*100:.1f}%, total ${sum(t["pnl_net"] for t in lst):+.2f}')

    # MFE/MAE
    print(f'\n--- MFE/MAE ---')
    if wins and losses:
        print(f'  Wins   MFE ${sum(t["mfe"]*t["qty"] for t in wins)/len(wins):+.2f}  MAE ${sum(t["mae"]*t["qty"] for t in wins)/len(wins):+.2f}')
        print(f'  Losses MFE ${sum(t["mfe"]*t["qty"] for t in losses)/len(losses):+.2f}  MAE ${sum(t["mae"]*t["qty"] for t in losses)/len(losses):+.2f}')
        lost_fav = [t for t in losses if t['mfe']*t['qty'] > 5]
        print(f'  Losers that went +$5 favorable first: {len(lost_fav)}/{len(losses)} ({len(lost_fav)/len(losses)*100:.0f}%)')

    # ADX buckets
    print(f'\n--- ADX BUCKET ---')
    for lo, hi in [(35,45),(45,60),(60,200)]:
        lst = [t for t in trades if lo <= t['adx'] < hi]
        if not lst: continue
        w = [t for t in lst if t['pnl_net']>0]
        gw_b = sum(t['pnl_net'] for t in w); gl_b = abs(sum(t['pnl_net'] for t in lst if t['pnl_net']<=0))
        print(f'  [{lo}-{hi}): {len(lst)} trades, WR {len(w)/len(lst)*100:.1f}%, PnL ${sum(t["pnl_net"] for t in lst):+.2f}, PF {gw_b/gl_b if gl_b else 99:.2f}')

    # Scale-out effectiveness
    scaled = [t for t in trades if t.get('scaled')]
    not_scaled = [t for t in trades if not t.get('scaled')]
    print(f'\n--- SCALE-OUT ---')
    if scaled:
        print(f'  Scaled trades:     {len(scaled)}, avg PnL ${sum(t["pnl_net"] for t in scaled)/len(scaled):+.2f}')
    if not_scaled:
        print(f'  Non-scaled trades: {len(not_scaled)}, avg PnL ${sum(t["pnl_net"] for t in not_scaled)/len(not_scaled):+.2f}')


if __name__ == '__main__':
    print('Loading 5y 1h + 4h data...')
    b1h = load_csv(DATA / 'btc_1h_5y.csv')
    b4h = load_csv(DATA / 'btc_4h_5y.csv')
    print(f'  {len(b1h)} × 1h, {len(b4h)} × 4h')
    bh = (b1h[-1]['close'] / b1h[WARMUP]['close'] - 1) * 100

    print('Running 5y 1h backtest...')
    final, trades, eq = run(b1h, b4h)
    analyze(final, trades, eq, bh)
