"""
Backtest for 15m BTC futures bot.
Uses the EXACT functions from strategy.py + indicators.py (same as live).
"""
import csv, math
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import strategy as strat
import config as cfg

INITIAL = 10_000.0
WARMUP_15M = 250
DATA = Path(__file__).parent / 'data'


def load_csv(path: Path) -> list:
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            ts = datetime.fromisoformat(r['timestamp'])
            rows.append({
                'ts': ts, 'open': float(r['open']), 'high': float(r['high']),
                'low': float(r['low']), 'close': float(r['close']),
                'vol': float(r['volume']), 'hour': ts.hour,
            })
    return rows


def to_kline(b: dict) -> list:
    return [int(b['ts'].timestamp()*1000), b['open'], b['high'], b['low'],
            b['close'], b['vol'], 0, 0, 0, 0, 0, 0]


def find_htf_idx(htf: list, ts_15m: datetime) -> int:
    """Last 1h bar that closed at or before this 15m timestamp."""
    target = ts_15m.timestamp()
    lo, hi = 0, len(htf) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if htf[mid]['ts'].timestamp() <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


def run_backtest():
    print('Loading data...')
    bars_15m = load_csv(DATA / 'btc_15m.csv')
    bars_1h  = load_csv(DATA / 'btc_1h.csv')
    print(f'  {len(bars_15m)} × 15m bars, {len(bars_1h)} × 1h bars')
    print(f'  Range: {bars_15m[0]["ts"]} → {bars_15m[-1]["ts"]}')

    cash = INITIAL
    pos_side = 'NONE'   # LONG / SHORT / NONE
    pos_qty  = 0.0
    state = {}
    trades = []
    eq_curve = []
    peak = INITIAL
    daily_pnl = {}

    bh_start = bars_15m[WARMUP_15M]['close']
    bh_end   = bars_15m[-1]['close']
    bh_return = (bh_end / bh_start - 1) * 100

    for idx in range(WARMUP_15M, len(bars_15m)):
        bar = bars_15m[idx]
        # Build kline windows
        kline15 = [to_kline(b) for b in bars_15m[max(0, idx-249):idx+1]]
        htf_end = find_htf_idx(bars_1h, bar['ts'])
        kline1h = [to_kline(b) for b in bars_1h[max(0, htf_end-249):htf_end+1]]
        if len(kline15) < 240 or len(kline1h) < 200:
            continue

        s = strat.compute_signal(kline15, kline1h)
        atr_val = s['atr']

        # Mark-to-market
        if pos_side == 'LONG':
            equity = cash + pos_qty * bar['close']
        elif pos_side == 'SHORT':
            equity = cash + pos_qty * (state['entry_price'] - bar['close']) + pos_qty * state['entry_price']
        else:
            equity = cash
        peak = max(peak, equity)

        # ── In a position: manage ─────────────────────────────────
        if pos_side != 'NONE':
            entry = state['entry_price']
            sl    = state['stop_loss']
            tp    = state['take_profit']

            # Compute current profit in ATR units
            if pos_side == 'LONG':
                profit_atr = (bar['close'] - entry) / atr_val if atr_val > 0 else 0
                profit_atr_high = (bar['high'] - entry) / atr_val if atr_val > 0 else 0
            else:
                profit_atr = (entry - bar['close']) / atr_val if atr_val > 0 else 0
                profit_atr_high = (entry - bar['low']) / atr_val if atr_val > 0 else 0

            # Scale-out: close fraction at +SCALE_OUT_AT_ATR profit (NEW)
            if cfg.SCALE_OUT_ENABLED and not state.get('scaled_out') and profit_atr_high >= cfg.SCALE_OUT_AT_ATR:
                scale_qty = pos_qty * cfg.SCALE_OUT_FRACTION
                # Use the price that triggered the scale-out
                if pos_side == 'LONG':
                    scale_price = entry + cfg.SCALE_OUT_AT_ATR * atr_val
                    pnl_g = (scale_price - entry) * scale_qty
                else:
                    scale_price = entry - cfg.SCALE_OUT_AT_ATR * atr_val
                    pnl_g = (entry - scale_price) * scale_qty
                fee = cfg.FEE_RATE * scale_price * scale_qty
                pnl_n = pnl_g - fee - state.get('entry_fee', 0) * cfg.SCALE_OUT_FRACTION
                cash = cash + scale_qty * entry + pnl_n
                pos_qty -= scale_qty
                state['qty'] = pos_qty
                state['scaled_out'] = True
                state['scale_pnl'] = pnl_n
                # Move SL to BE on remainder (already covered by BE trail below, but make explicit)
                state['stop_loss'] = entry
                sl = entry

            # Chandelier trail (only AFTER profit threshold reached — fix from deep dive)
            chandelier_active = profit_atr >= cfg.CHANDELIER_ACTIVATE_AT_ATR
            if chandelier_active:
                chand = strat.chandelier_stop(
                    [float(k[2]) for k in kline15],
                    [float(k[3]) for k in kline15],
                    atr_val, pos_side
                )
                if pos_side == 'LONG':
                    sl = max(sl, chand)
                else:
                    sl = min(sl, chand) if sl > 0 else chand

            # BE trail
            if pos_side == 'LONG':
                if bar['high'] - entry >= cfg.TRAIL_BE_AT_ATR * atr_val:
                    sl = max(sl, entry)
            else:
                if entry - bar['low'] >= cfg.TRAIL_BE_AT_ATR * atr_val:
                    sl = min(sl, entry) if sl > 0 else entry
            state['stop_loss'] = sl

            # Regime flip exit
            regime_flip = (
                (pos_side == 'LONG' and s['direction'] == 'SHORT') or
                (pos_side == 'SHORT' and s['direction'] == 'LONG')
            )

            exit_price, reason = None, None
            if pos_side == 'LONG':
                if bar['low']  <= sl:        exit_price, reason = sl, 'SL'
                elif bar['high'] >= tp:      exit_price, reason = tp, 'TP'
                elif regime_flip:            exit_price, reason = bar['close'], 'FLIP'
            else:  # SHORT
                if bar['high'] >= sl:        exit_price, reason = sl, 'SL'
                elif bar['low']  <= tp:      exit_price, reason = tp, 'TP'
                elif regime_flip:            exit_price, reason = bar['close'], 'FLIP'

            if exit_price:
                # Apply remaining-fraction fee correction
                remaining_entry_fee = state.get('entry_fee', 0)
                if state.get('scaled_out'):
                    remaining_entry_fee *= (1 - cfg.SCALE_OUT_FRACTION)
                if pos_side == 'LONG':
                    pnl_gross = (exit_price - entry) * pos_qty
                else:
                    pnl_gross = (entry - exit_price) * pos_qty
                fee_exit  = cfg.FEE_RATE * exit_price * pos_qty
                pnl_net   = pnl_gross - fee_exit - remaining_entry_fee
                cash = cash + pos_qty * entry + pnl_net  # release remaining margin + pnl
                # Total trade PnL includes any scale-out
                total_pnl = pnl_net + state.get('scale_pnl', 0)
                trades.append({
                    'entry_ts': state['entry_ts'], 'exit_ts': bar['ts'],
                    'side': pos_side, 'entry': entry, 'exit': exit_price,
                    'qty': state.get('initial_qty', pos_qty), 'pnl_net': total_pnl,
                    'reason': reason + ('+SCALE' if state.get('scaled_out') else ''),
                    'bars_held': idx - state['entry_idx'],
                })
                d = bar['ts'].date()
                daily_pnl[d] = daily_pnl.get(d, 0) + total_pnl
                pos_side = 'NONE'; pos_qty = 0.0; state = {}
            eq_curve.append((bar['ts'], equity))
            continue

        # ── No position: scan entry ───────────────────────────────
        # Risk halts
        d = bar['ts'].date()
        dp = daily_pnl.get(d, 0)
        if equity > 0 and dp < 0 and abs(dp)/equity >= cfg.DAILY_LOSS_HALT_PCT:
            eq_curve.append((bar['ts'], equity)); continue
        if peak > 0 and (peak-equity)/peak >= cfg.MAX_DD_HALT_PCT:
            eq_curve.append((bar['ts'], equity)); continue
        if bar['hour'] in cfg.LOW_LIQ_UTC_HOURS:
            eq_curve.append((bar['ts'], equity)); continue

        if not (s['long_signal'] or s['short_signal']):
            eq_curve.append((bar['ts'], equity)); continue

        side = 'LONG' if s['long_signal'] else 'SHORT'
        price = s['price']
        qty = strat.compute_qty(equity, price, atr_val)
        if qty < 0.001: 
            eq_curve.append((bar['ts'], equity)); continue

        cost = qty * price
        fee_entry = cfg.FEE_RATE * cost
        if cost + fee_entry > cash:
            eq_curve.append((bar['ts'], equity)); continue

        if side == 'LONG':
            sl_price = price - cfg.ATR_SL_MULT * atr_val
            tp_price = price + cfg.ATR_TP_MULT * atr_val
        else:
            sl_price = price + cfg.ATR_SL_MULT * atr_val
            tp_price = price - cfg.ATR_TP_MULT * atr_val

        cash -= cost + fee_entry
        pos_side = side
        pos_qty  = qty
        state = {
            'entry_price': price, 'stop_loss': sl_price, 'take_profit': tp_price,
            'qty': qty, 'initial_qty': qty,
            'entry_ts': bar['ts'], 'entry_idx': idx,
            'entry_fee': fee_entry,
        }
        eq_curve.append((bar['ts'], equity))

    # Close any open position at last price
    if pos_side != 'NONE':
        last = bars_15m[-1]
        entry = state['entry_price']
        if pos_side == 'LONG':
            pnl_gross = (last['close'] - entry) * pos_qty
        else:
            pnl_gross = (entry - last['close']) * pos_qty
        fee_exit = cfg.FEE_RATE * last['close'] * pos_qty
        pnl_net  = pnl_gross - fee_exit - state.get('entry_fee', 0)
        cash = cash + pos_qty * entry + pnl_net
        trades.append({
            'entry_ts': state['entry_ts'], 'exit_ts': last['ts'],
            'side': pos_side, 'entry': entry, 'exit': last['close'],
            'qty': pos_qty, 'pnl_net': pnl_net, 'reason': 'EOD',
            'bars_held': len(bars_15m)-1 - state['entry_idx'],
        })

    return cash, trades, eq_curve, bh_return


def metrics(final, trades, eq, initial=INITIAL):
    if not trades:
        return {'no_trades': True}
    n = len(trades)
    wins   = [t for t in trades if t['pnl_net'] > 0]
    losses = [t for t in trades if t['pnl_net'] <= 0]
    wr = len(wins)/n*100
    gw = sum(t['pnl_net'] for t in wins)
    gl = abs(sum(t['pnl_net'] for t in losses))
    pf = gw/gl if gl > 0 else float('inf')
    avg_win  = (gw/len(wins))   if wins   else 0
    avg_loss = (gl/len(losses)) if losses else 0
    expectancy = (gw - gl) / n

    peak = initial; mdd = 0
    for _, e in eq:
        peak = max(peak, e)
        mdd  = max(mdd, (peak-e)/peak)

    rets = []
    for i in range(1, len(eq)):
        prev, curr = eq[i-1][1], eq[i][1]
        if prev > 0: rets.append((curr-prev)/prev)
    if rets:
        m = sum(rets)/len(rets)
        v = sum((r-m)**2 for r in rets)/len(rets)
        sd = math.sqrt(v)
        # 15m bars: 96/day, ~35040/yr
        sharpe = (m/sd)*math.sqrt(35040) if sd > 0 else 0
    else:
        sharpe = 0

    by_reason = {}; by_side = {}
    for t in trades:
        by_reason[t['reason']] = by_reason.get(t['reason'], 0) + 1
        by_side[t['side']]     = by_side.get(t['side'], 0) + 1

    avg_hold = sum(t['bars_held'] for t in trades) / n

    return {
        'final': round(final, 2),
        'return_pct': round((final-initial)/initial*100, 2),
        'trades': n, 'wr': round(wr, 1),
        'pf': round(pf, 2) if pf != float('inf') else 'inf',
        'sharpe': round(sharpe, 2),
        'max_dd': round(mdd*100, 2),
        'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
        'expectancy': round(expectancy, 2),
        'avg_hold_bars': round(avg_hold, 1),
        'by_reason': by_reason, 'by_side': by_side,
    }


def report(m, bh):
    if m.get('no_trades'):
        print('  No trades generated.'); return
    print(f'  Final equity:    ${m["final"]:>10,.2f}')
    print(f'  Total return:    {m["return_pct"]:>+10.2f}%')
    print(f'  Buy & Hold:      {bh:>+10.2f}%')
    print(f'  Alpha:           {m["return_pct"]-bh:>+10.2f}%')
    print(f'  Trades:          {m["trades"]}  ({m["by_side"]})')
    print(f'  Win rate:        {m["wr"]:.1f}%')
    print(f'  Profit factor:   {m["pf"]}')
    print(f'  Sharpe (annual): {m["sharpe"]}')
    print(f'  Max drawdown:    {m["max_dd"]:.2f}%')
    print(f'  Avg win/loss:    ${m["avg_win"]} / ${m["avg_loss"]}')
    print(f'  Expectancy:      ${m["expectancy"]} per trade')
    print(f'  Avg hold:        {m["avg_hold_bars"]:.1f} bars (~{m["avg_hold_bars"]*15/60:.1f}h)')
    print(f'  Exit reasons:    {m["by_reason"]}')


if __name__ == '__main__':
    final, trades, eq, bh = run_backtest()
    m = metrics(final, trades, eq)
    print(f'\n=== BACKTEST RESULT ({INITIAL:,.0f} initial) ===')
    report(m, bh)
