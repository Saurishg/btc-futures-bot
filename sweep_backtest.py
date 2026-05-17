"""
Liquidity Sweep Backtest — 5-Year 1H data, all 3 symbols.

Signal: price wicks beyond N-bar swing high/low by ≥ WICK_ATR_MIN × ATR,
        then closes back through the swept level with a reversal body.
Entry:  close of the sweep candle (simulated as next open via close≈open heuristic)
SL:     just beyond the wick tip + 0.15×ATR buffer  (tight, precise)
TP:     price ± TP_MULT × ATR
Exits:  same scale-out (50% @ 1×ATR), chandelier trail, BE trail as main bot.
"""
import sys, csv, math
from pathlib import Path
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from indicators import ema, atr, adx

DATA    = Path(__file__).parent / 'data'
INITIAL = 10_000.0
WARMUP  = 250
FEE     = 0.0004

# ── Exit system (same as main bot) ──────────────────────────────────────────
SCALE_OUT_ATR        = 1.0
SCALE_OUT_FRAC       = 0.5
CHANDELIER_PERIOD    = 20
CHANDELIER_MULT      = 3.0
CHANDELIER_ACTIVATE  = 1.5
TRAIL_BE_ATR         = 1.0
RISK_PCT             = 0.01
MAX_POS_PCT          = 0.30

# ── Symbol configs ────────────────────────────────────────────────────────
SYMBOLS = {
    'BTCUSDT': {
        'file1h': 'btc_1h_5y.csv', 'file4h': 'btc_4h_5y.csv',
        'shorts_only': False,
        'sweep_wick_atr_min': 0.25, 'sweep_lookback': 20,
        'sweep_short_tp_mult': 3.0,  'sweep_long_tp_mult': 5.0,
        'sl_buffer_atr': 0.15,
    },
    'ETHUSDT': {
        'file1h': 'eth_1h_5y.csv', 'file4h': 'eth_4h_5y.csv',
        'shorts_only': True,
        'sweep_wick_atr_min': 0.20, 'sweep_lookback': 20,
        'sweep_short_tp_mult': 2.5,
        'sl_buffer_atr': 0.15,
    },
    'SOLUSDT': {
        'file1h': 'sol_1h_5y.csv', 'file4h': 'sol_4h_5y.csv',
        'shorts_only': True,
        'sweep_wick_atr_min': 0.25, 'sweep_lookback': 20,
        'sweep_short_tp_mult': 3.0,
        'sl_buffer_atr': 0.15,
    },
}


def load_csv(p):
    rows = []
    with open(p) as f:
        for r in csv.DictReader(f):
            ts = datetime.fromisoformat(r['timestamp'])
            rows.append({'ts': ts, 'open': float(r['open']), 'high': float(r['high']),
                         'low': float(r['low']), 'close': float(r['close']),
                         'vol': float(r['volume']), 'year': ts.year, 'hour': ts.hour})
    return rows


def htf_direction(htf_closes, fast=50, slow=200):
    if len(htf_closes) < slow: return 'NONE'
    ef = ema(htf_closes[-fast*3:], fast)
    es = ema(htf_closes, slow)
    if ef > es * 1.001: return 'LONG'
    if ef < es * 0.999: return 'SHORT'
    return 'NONE'


def adx_rising(highs, lows, closes, n=14, lb=5):
    if len(closes) < lb + 30: return False
    return adx(highs, lows, closes, n) > adx(highs[:-lb], lows[:-lb], closes[:-lb], n)


def chandelier_stop(highs, lows, atr_val, side):
    if side == 'LONG': return max(highs[-CHANDELIER_PERIOD:]) - CHANDELIER_MULT * atr_val
    return min(lows[-CHANDELIER_PERIOD:]) + CHANDELIER_MULT * atr_val


def detect_sweep(highs, lows, closes, opens, atr_val, direction, cfg):
    n        = cfg['sweep_lookback']
    wick_min = cfg['sweep_wick_atr_min']
    if len(highs) < n + 2 or atr_val <= 0:
        return False, False

    swing_hi = max(highs[-n-1:-1])
    swing_lo = min(lows[-n-1:-1])
    hi, lo, cl, op = highs[-1], lows[-1], closes[-1], opens[-1]

    above_wick = hi - swing_hi   # >0 → wicked above swing high
    below_wick = swing_lo - lo   # >0 → wicked below swing low

    sw_short = (
        direction == 'SHORT'
        and above_wick >= wick_min * atr_val
        and cl < swing_hi        # closed back below swept level
        and cl < op              # bearish body
    )
    sw_long = (
        direction == 'LONG'
        and not cfg.get('shorts_only', False)
        and below_wick >= wick_min * atr_val
        and cl > swing_lo        # closed back above swept level
        and cl > op              # bullish body
    )
    return sw_long, sw_short


def run_symbol(bars1h, bars4h, cfg, wick_override=None):
    """Run one symbol. Returns (final_cash, trades, equity_curve)."""
    if wick_override is not None:
        cfg = {**cfg, 'sweep_wick_atr_min': wick_override}

    cash = INITIAL
    side = 'NONE'; qty = 0.0; state = {}; trades = []; eq = []

    # Build 4h index: for each 1h bar, which 4h bar was most-recent-complete
    h_idx = 0
    htf_map = []
    for b in bars1h:
        while h_idx + 1 < len(bars4h) and bars4h[h_idx+1]['ts'].timestamp() <= b['ts'].timestamp():
            h_idx += 1
        htf_map.append(h_idx)

    for idx in range(WARMUP, len(bars1h)):
        bar = bars1h[idx]
        W = 250
        sl = max(0, idx - W + 1)
        highs  = [bars1h[i]['high']  for i in range(sl, idx+1)]
        lows   = [bars1h[i]['low']   for i in range(sl, idx+1)]
        closes = [bars1h[i]['close'] for i in range(sl, idx+1)]
        opens  = [bars1h[i]['open']  for i in range(sl, idx+1)]

        h_end = htf_map[idx]
        htf_closes = [bars4h[i]['close'] for i in range(max(0, h_end-249), h_end+1)]
        if len(closes) < 240 or len(htf_closes) < 200:
            eq.append(INITIAL); continue

        direction = htf_direction(htf_closes)
        atr_val   = atr(highs, lows, closes, 14)
        price     = closes[-1]
        if atr_val <= 0:
            eq.append(cash); continue

        equity = cash
        if side == 'LONG':
            equity = cash + qty * (price - state['entry_price'])
        elif side == 'SHORT':
            equity = cash + qty * (state['entry_price'] - price)

        # ── In position ─────────────────────────────────────────────
        if side != 'NONE':
            entry = state['entry_price']
            sl_p  = state['stop_loss']
            tp_p  = state['take_profit']

            if side == 'LONG':
                profit_atr = (bar['high'] - entry) / atr_val
                profit_atr_close = (bar['close'] - entry) / atr_val
            else:
                profit_atr = (entry - bar['low']) / atr_val
                profit_atr_close = (entry - bar['close']) / atr_val

            # Scale-out at 1×ATR
            if not state.get('scaled_out') and profit_atr >= SCALE_OUT_ATR:
                scale_qty = qty * SCALE_OUT_FRAC
                sp = entry + SCALE_OUT_ATR*atr_val if side=='LONG' else entry - SCALE_OUT_ATR*atr_val
                pnl_s = ((sp - entry) if side=='LONG' else (entry - sp)) * scale_qty
                pnl_s -= FEE * sp * scale_qty + state.get('entry_fee',0) * SCALE_OUT_FRAC
                cash += pnl_s
                qty -= scale_qty
                state.update({'scaled_out': True, 'scale_pnl': pnl_s, 'stop_loss': entry})
                sl_p = entry

            # Chandelier trail
            if profit_atr_close >= CHANDELIER_ACTIVATE:
                chand = chandelier_stop(highs, lows, atr_val, side)
                if side == 'LONG': sl_p = max(sl_p, chand)
                else:              sl_p = min(sl_p, chand) if sl_p > 0 else chand

            # BE trail
            if side == 'LONG' and bar['high'] - entry >= TRAIL_BE_ATR * atr_val:
                sl_p = max(sl_p, entry)
            elif side == 'SHORT' and entry - bar['low'] >= TRAIL_BE_ATR * atr_val:
                sl_p = min(sl_p, entry) if sl_p > 0 else entry
            state['stop_loss'] = sl_p

            # MFE/MAE
            if side == 'LONG':
                state['mfe'] = max(state.get('mfe',0), bar['high'] - entry)
                state['mae'] = min(state.get('mae',0), bar['low']  - entry)
            else:
                state['mfe'] = max(state.get('mfe',0), entry - bar['low'])
                state['mae'] = min(state.get('mae',0), entry - bar['high'])

            ex_p = reason = None
            if side == 'LONG':
                if   bar['low']  <= sl_p: ex_p, reason = sl_p, 'SL'
                elif bar['high'] >= tp_p: ex_p, reason = tp_p, 'TP'
            else:
                if   bar['high'] >= sl_p: ex_p, reason = sl_p, 'SL'
                elif bar['low']  <= tp_p: ex_p, reason = tp_p, 'TP'

            if ex_p:
                pnl_g = ((ex_p-entry) if side=='LONG' else (entry-ex_p)) * qty
                fee_e = FEE * ex_p * qty
                rem_fee = state.get('entry_fee',0) * (SCALE_OUT_FRAC if state.get('scaled_out') else 1)
                pnl_n = pnl_g - fee_e - rem_fee + state.get('scale_pnl', 0)
                cash += pnl_n
                trades.append({
                    'side': side, 'pnl_net': pnl_n, 'reason': reason,
                    'year': bar['year'], 'hour': bar['hour'],
                    'mfe': state.get('mfe',0), 'mae': state.get('mae',0),
                    'scaled': state.get('scaled_out', False),
                    'sl_atr': state.get('sl_atr', 0),
                })
                side = 'NONE'; qty = 0.0; state = {}

            eq.append(cash); continue

        # ── Entry scan ───────────────────────────────────────────────
        sw_long, sw_short = detect_sweep(highs, lows, closes, opens, atr_val, direction, cfg)
        if not (sw_long or sw_short):
            eq.append(cash); continue

        new_side = 'LONG' if sw_long else 'SHORT'

        # SL just beyond the wick tip
        buf = cfg['sl_buffer_atr'] * atr_val
        if new_side == 'SHORT':
            sl_price = highs[-1] + buf           # just above the wick high
            tp_mult  = cfg.get('sweep_short_tp_mult', 3.0)
            tp_price = price - tp_mult * atr_val
            sl_atr   = (sl_price - price) / atr_val
        else:
            sl_price = lows[-1] - buf            # just below the wick low
            tp_mult  = cfg.get('sweep_long_tp_mult', 5.0)
            tp_price = price + tp_mult * atr_val
            sl_atr   = (price - sl_price) / atr_val

        sl_dist = abs(price - sl_price)
        if sl_dist <= 0: eq.append(cash); continue

        qty_r = (cash * RISK_PCT) / sl_dist
        qty_c = (cash * MAX_POS_PCT) / price
        new_qty = round(min(qty_r, qty_c), 4)
        if new_qty < 0.0001: eq.append(cash); continue

        entry_fee = FEE * price * new_qty
        if price * new_qty + entry_fee > cash: eq.append(cash); continue

        side = new_side; qty = new_qty
        state = {
            'entry_price': price, 'stop_loss': sl_price, 'take_profit': tp_price,
            'entry_fee': entry_fee, 'mfe': 0, 'mae': 0, 'sl_atr': sl_atr,
        }
        eq.append(cash)

    # Close open position at end
    if side != 'NONE':
        last = bars1h[-1]; entry = state['entry_price']
        pnl_g = ((last['close']-entry) if side=='LONG' else (entry-last['close'])) * qty
        fee_e = FEE * last['close'] * qty
        pnl_n = pnl_g - fee_e + state.get('scale_pnl', 0)
        cash += pnl_n
        trades.append({
            'side': side, 'pnl_net': pnl_n, 'reason': 'EOD',
            'year': last['year'], 'hour': last['hour'],
            'mfe': state.get('mfe',0), 'mae': state.get('mae',0),
            'scaled': state.get('scaled_out', False), 'sl_atr': state.get('sl_atr', 0),
        })

    return cash, trades, eq


def summarise(sym, final, trades, eq, bh_pct):
    n = len(trades)
    if not n:
        print(f'\n[{sym}] No trades fired.')
        return

    wins   = [t for t in trades if t['pnl_net'] > 0]
    losses = [t for t in trades if t['pnl_net'] <= 0]
    gw = sum(t['pnl_net'] for t in wins)
    gl = abs(sum(t['pnl_net'] for t in losses))
    pf = gw / gl if gl > 0 else 99.0
    wr = len(wins) / n * 100

    peak_e = INITIAL; mdd = 0.0
    for e in eq:
        peak_e = max(peak_e, e)
        mdd = max(mdd, (peak_e - e) / peak_e * 100 if peak_e > 0 else 0)

    ret = (final - INITIAL) / INITIAL * 100
    print(f'\n{"="*62}')
    print(f'  {sym} — LIQUIDITY SWEEP — 5Y')
    print(f'{"="*62}')
    print(f'  Trades: {n:>4}  |  WR: {wr:>5.1f}%  |  PF: {pf:>5.2f}')
    print(f'  Return: {ret:>+7.2f}%  |  B&H: {bh_pct:>+7.2f}%  |  Max DD: {mdd:>5.2f}%')
    print(f'  Avg win: ${gw/len(wins) if wins else 0:,.2f}  |  Avg loss: ${gl/len(losses) if losses else 0:,.2f}')

    # By year
    print(f'\n  {"Year":<6} {"N":>4} {"WR%":>6} {"Total $":>10} {"PF":>5}')
    by_year = defaultdict(list)
    for t in trades: by_year[t['year']].append(t)
    for y in sorted(by_year):
        lst = by_year[y]
        w = [t for t in lst if t['pnl_net'] > 0]
        gw_y = sum(t['pnl_net'] for t in w)
        gl_y = abs(sum(t['pnl_net'] for t in lst if t['pnl_net'] <= 0))
        pf_y = gw_y / gl_y if gl_y else 99.0
        tot  = sum(t['pnl_net'] for t in lst)
        pf_y_str = f'{pf_y:5.2f}' if pf_y < 99 else ' 99+'
        print(f'  {y:<6} {len(lst):>4} {len(w)/len(lst)*100:>5.1f}% {tot:>+10.2f} {pf_y_str}')

    # By side
    print(f'\n  By side:')
    for s in ['LONG', 'SHORT']:
        lst = [t for t in trades if t['side'] == s]
        if not lst: continue
        w = [t for t in lst if t['pnl_net'] > 0]
        gw_s = sum(t['pnl_net'] for t in w)
        gl_s = abs(sum(t['pnl_net'] for t in lst if t['pnl_net'] <= 0))
        print(f'    {s}: {len(lst)} trades  WR {len(w)/len(lst)*100:.1f}%  PF {gw_s/gl_s if gl_s else 99:.2f}  total ${sum(t["pnl_net"] for t in lst):+,.2f}')

    # MFE/MAE on losses (how many could have been saved)
    if losses:
        salvageable = [t for t in losses if t['mfe'] * (INITIAL/10000) > 20]
        print(f'\n  Losses that went +$20 favorable first: {len(salvageable)}/{len(losses)} ({len(salvageable)/len(losses)*100:.0f}%)')

    # Avg sl_atr (shows how tight sweep entries are vs breakout ~1.9×ATR)
    avg_sl = sum(t['sl_atr'] for t in trades) / n
    print(f'  Avg SL distance: {avg_sl:.2f}×ATR  (breakout SL is ~1.9×ATR)')


def param_sweep_wick(sym, bars1h, bars4h, cfg):
    """Sweep wick_atr_min values to find optimal threshold."""
    print(f'\n  --- {sym}: wick_atr_min sweep ---')
    print(f'  {"wick":>6} {"n":>4} {"WR%":>6} {"PF":>5} {"Return%":>8}')
    best = None
    for wick in [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
        final, trades, eq = run_symbol(bars1h, bars4h, cfg, wick_override=wick)
        if not trades:
            print(f'  {wick:>6.2f}     0     —      —        —')
            continue
        wins = [t for t in trades if t['pnl_net'] > 0]
        gl   = abs(sum(t['pnl_net'] for t in trades if t['pnl_net'] <= 0))
        gw   = sum(t['pnl_net'] for t in wins)
        pf   = gw / gl if gl else 99.0
        wr   = len(wins) / len(trades) * 100
        ret  = (final - INITIAL) / INITIAL * 100
        marker = ' ◄ best' if best is None or pf > best['pf'] else ''
        if marker:
            best = {'wick': wick, 'pf': pf}
        print(f'  {wick:>6.2f} {len(trades):>4} {wr:>5.1f}% {pf:>5.2f} {ret:>+7.2f}%{marker}')
    return best


if __name__ == '__main__':
    all_trades = []

    for sym, cfg in SYMBOLS.items():
        f1h = DATA / cfg['file1h']
        f4h = DATA / cfg['file4h']
        if not f1h.exists() or not f4h.exists():
            print(f'[{sym}] Data not found — skipping')
            continue

        print(f'\nLoading {sym} data…')
        b1h = load_csv(f1h)
        b4h = load_csv(f4h)
        print(f'  {len(b1h)} × 1h  |  {len(b4h)} × 4h')

        bh_pct = (b1h[-1]['close'] / b1h[WARMUP]['close'] - 1) * 100

        print(f'Running sweep backtest…')
        final, trades, eq = run_symbol(b1h, b4h, cfg)
        summarise(sym, final, trades, eq, bh_pct)

        for t in trades:
            t['symbol'] = sym
        all_trades.extend(trades)

        # Parameter sweep
        best = param_sweep_wick(sym, b1h, b4h, cfg)

    # ── Combined portfolio summary ───────────────────────────────────────
    if all_trades:
        n = len(all_trades)
        wins  = [t for t in all_trades if t['pnl_net'] > 0]
        gw    = sum(t['pnl_net'] for t in wins)
        gl    = abs(sum(t['pnl_net'] for t in all_trades if t['pnl_net'] <= 0))
        pf    = gw / gl if gl else 99.0
        total = sum(t['pnl_net'] for t in all_trades)
        print(f'\n{"="*62}')
        print(f'  COMBINED PORTFOLIO — all symbols')
        print(f'{"="*62}')
        print(f'  Trades: {n}  |  WR: {len(wins)/n*100:.1f}%  |  PF: {pf:.2f}  |  Total P&L: ${total:+,.2f}')
        print(f'\n  Trades/yr approx: {n/5:.1f}')
        by_sym = defaultdict(list)
        for t in all_trades: by_sym[t['symbol']].append(t)
        for s, lst in sorted(by_sym.items()):
            w = [t for t in lst if t['pnl_net'] > 0]
            print(f'    {s}: {len(lst)} trades  WR {len(w)/len(lst)*100:.1f}%')
