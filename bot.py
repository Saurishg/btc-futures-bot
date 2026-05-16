#!/usr/bin/env python3
"""
BTC Futures 15m Live Bot — long/short on Binance USDⓈ-M Futures.

Endpoints controlled by .env:
  BINANCE_FUT_ENV=testnet  (default — uses testnet.binancefuture.com)
  BINANCE_FUT_ENV=live     (uses fapi.binance.com — REAL MONEY)

Required .env keys:
  testnet:  BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET
  live:     BINANCE_FUT_API_KEY     / BINANCE_FUT_API_SECRET
"""
import os, sys, time, json, logging
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config as cfg
from exchange import (
    set_leverage, set_margin_type, get_balance, get_position,
    get_klines, market_order, cancel_all_orders, get_open_orders,
    stop_market_order, take_profit_order,
)
from strategy import compute_signal, compute_qty


# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / 'bot.log'),
    ]
)
log = logging.getLogger('bot')

STATE_FILE = Path(__file__).parent / 'state.json'
PNL_FILE   = Path(__file__).parent / 'pnl.json'
LOCK_FILE  = Path(__file__).parent / '.bot_lock'
PEAK_FILE  = Path(__file__).parent / '.peak_equity'


# ── State ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2))

def log_pnl(action, side, price, qty, pnl=0, reason=''):
    rec = json.loads(PNL_FILE.read_text()) if PNL_FILE.exists() else []
    rec.append({
        'ts': datetime.now().isoformat(), 'action': action, 'side': side,
        'price': price, 'qty': qty, 'pnl': round(pnl, 2), 'reason': reason,
        'env': cfg.ENV,
    })
    PNL_FILE.write_text(json.dumps(rec, indent=2))

def get_daily_pnl() -> float:
    if not PNL_FILE.exists(): return 0.0
    today = date.today().isoformat()
    return sum(t.get('pnl', 0) for t in json.loads(PNL_FILE.read_text())
               if t['ts'].startswith(today))

def update_peak(equity: float) -> float:
    peak = float(PEAK_FILE.read_text()) if PEAK_FILE.exists() else 0.0
    if equity > peak:
        PEAK_FILE.write_text(str(equity))
        return equity
    return peak

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        if time.time() - LOCK_FILE.stat().st_mtime < 300: return False
        LOCK_FILE.unlink()
    LOCK_FILE.write_text(str(os.getpid()))
    return True

def release_lock():
    try: LOCK_FILE.unlink()
    except: pass


# ── Risk halts ───────────────────────────────────────────────────────────

KILL_SWITCH_PATH = Path(__file__).parent / cfg.KILL_SWITCH_FILE

def kill_switch_active() -> bool:
    return KILL_SWITCH_PATH.exists()

def is_halted(equity: float) -> tuple[bool, str]:
    if kill_switch_active():
        return True, 'KILL SWITCH active (delete .kill_switch to resume)'
    daily = get_daily_pnl()
    if equity > 0 and daily < 0 and abs(daily)/equity >= cfg.DAILY_LOSS_HALT_PCT:
        return True, f'daily loss {daily:+.2f} (≥{cfg.DAILY_LOSS_HALT_PCT*100}%)'
    peak = update_peak(equity)
    if peak > 0 and (peak-equity)/peak >= cfg.MAX_DD_HALT_PCT:
        return True, f'max DD {(peak-equity)/peak*100:.1f}%'
    if equity < cfg.MIN_BALANCE_USD:
        return True, f'equity ${equity:.2f} below MIN_BALANCE_USD ${cfg.MIN_BALANCE_USD}'
    if datetime.utcnow().hour in cfg.LOW_LIQ_UTC_HOURS:
        return True, f'low-liq UTC hour'
    return False, ''


def force_close_position(pos: dict, reason: str):
    """Close any open position at market — used by kill switch / daily halt."""
    if pos['side'] == 'NONE': return
    close_side = 'SELL' if pos['side'] == 'LONG' else 'BUY'
    try:
        market_order(close_side, pos['qty'], reduce_only=True)
        cancel_all_orders()
        log_pnl('CLOSE', pos['side'], 0, pos['qty'], pos.get('unrealized_pnl', 0), reason)
        save_state({})
        log.warning(f'⚠️ FORCE-CLOSED {pos["side"]} {pos["qty"]} — reason: {reason}')
    except Exception as e:
        log.error(f'Force-close failed: {e}')


# ── Main tick ────────────────────────────────────────────────────────────

def initialize():
    log.info(f'═══ BTC Futures Bot (v2) ═══')
    log.info(f'ENV={cfg.ENV.upper()} | URL={cfg.BASE_URL}')
    log.info(f'Symbol={cfg.SYMBOL} | TF={cfg.TF_ENTRY}/{cfg.TF_HTF} | Leverage={cfg.LEVERAGE}x | Margin={cfg.MARGIN_TYPE}')
    log.info(f'Risk: {cfg.RISK_PCT*100:.2f}%/trade | Max pos: ${cfg.MAX_POSITION_USD:,.0f} | Min bal: ${cfg.MIN_BALANCE_USD:,.0f}')
    log.info(f'Daily halt: {cfg.DAILY_LOSS_HALT_PCT*100}% | Max DD halt: {cfg.MAX_DD_HALT_PCT*100}%')
    if cfg.ENV == 'live':
        log.warning('🚨 LIVE MODE — REAL MONEY 🚨')

    # Startup state reconciliation
    state_file = Path(__file__).parent / 'state.json'
    if state_file.exists():
        try:
            saved = json.loads(state_file.read_text())
            if saved.get('side'):
                log.info(f'Recovered state: was {saved["side"]} {saved.get("qty","?")} @ ${saved.get("entry_price","?")}')
        except Exception as e:
            log.warning(f'State file unreadable: {e}')

    if not cfg.API_KEY:
        log.error('No API key configured for ENV=' + cfg.ENV)
        log.error('Add to .env:')
        if cfg.ENV == 'testnet':
            log.error('  BINANCE_TESTNET_API_KEY=...')
            log.error('  BINANCE_TESTNET_API_SECRET=...')
        else:
            log.error('  BINANCE_FUT_API_KEY=...')
            log.error('  BINANCE_FUT_API_SECRET=...')
        sys.exit(1)
    try:
        set_leverage(cfg.LEVERAGE)
        set_margin_type(cfg.MARGIN_TYPE)
        bal = get_balance()
        log.info(f'Balance: {bal:.2f} USDT')
    except Exception as e:
        log.error(f'Init failed: {e}')
        raise


def tick():
    klines_15m = get_klines(cfg.TF_ENTRY, 250)
    klines_1h  = get_klines(cfg.TF_HTF, 250)
    if len(klines_15m) < 240 or len(klines_1h) < 200:
        log.warning('Not enough candles')
        return

    s = compute_signal(klines_15m, klines_1h)
    pos = get_position()
    state = load_state()

    bal = get_balance()
    pos_value = pos['qty'] * pos['entry'] if pos['side'] != 'NONE' else 0
    equity = bal + pos.get('unrealized_pnl', 0)

    # Snapshot for public status page (no trading logic — read by update_btc_futures.sh)
    try:
        snapshot = {
            'ts':            datetime.now().isoformat(),
            'env':           cfg.ENV,
            'symbol':        cfg.SYMBOL,
            'tf_entry':      cfg.TF_ENTRY,
            'tf_htf':        cfg.TF_HTF,
            'leverage':      cfg.LEVERAGE,
            'price':         s['price'],
            'atr':           s['atr'],
            'rsi':           s['rsi'],
            'adx':           s['adx'],
            'adx_rising':    s['adx_rising'],
            'direction':     s['direction'],
            'donchian_high': s['donchian_high'],
            'donchian_low':  s['donchian_low'],
            'body_atr':      s['body_atr'],
            'vol_ratio':     s['vol_ratio'],
            'long_signal':   s['long_signal'],
            'short_signal':  s['short_signal'],
            'balance':       bal,
            'equity':        equity,
            'has_pos':       pos['side'] != 'NONE',
            'pos_side':      pos['side'],
            'pos_qty':       pos.get('qty', 0),
            'pos_entry':     pos.get('entry', 0),
            'pos_unrealized':pos.get('unrealized_pnl', 0),
            'params': {
                'SHORT_ADX_MIN':      cfg.SHORT_ADX_MIN,
                'SHORT_ADX_MAX':      cfg.SHORT_ADX_MAX,
                'SHORT_BODY_ATR_MIN': cfg.SHORT_BODY_ATR_MIN,
                'SHORT_VOL_MULT':     cfg.SHORT_VOL_MULT,
                'ATR_SL_MULT':        cfg.ATR_SL_MULT,
                'ATR_TP_MULT':        cfg.ATR_TP_MULT,
            },
        }
        (Path(__file__).parent / 'indicators.json').write_text(json.dumps(snapshot, indent=2))
    except Exception as e:
        log.warning(f'Snapshot write failed: {e}')

    log.info(f'Price=${s["price"]:,.0f} | dir={s["direction"]} | RSI={s["rsi"]:.0f} | '
             f'ADX={s["adx"]:.0f}{"↑" if s["adx_rising"] else ""} | '
             f'donch_hi=${s["donchian_high"]:,.0f} donch_lo=${s["donchian_low"]:,.0f} | '
             f'long_sig={s["long_signal"]} short_sig={s["short_signal"]}')

    # ── Kill switch / daily halt while in position: force-close ───
    if pos['side'] != 'NONE':
        halted, why = is_halted(equity)
        if halted and ('KILL SWITCH' in why or 'daily loss' in why or 'max DD' in why):
            log.warning(f'Halt while in position: {why} — closing now')
            force_close_position(pos, why)
            return

    # ── In a position: manage exit ─────────────────────────────────
    if pos['side'] != 'NONE':
        side = pos['side']; qty = pos['qty']
        entry = pos['entry']; price = s['price']

        highs = [float(k[2]) for k in klines_15m]
        lows  = [float(k[3]) for k in klines_15m]
        sl = state.get('stop_loss', 0)

        # Compute current profit in ATR units
        if side == 'LONG':
            profit_atr = (price - entry) / s['atr'] if s['atr'] > 0 else 0
        else:
            profit_atr = (entry - price) / s['atr'] if s['atr'] > 0 else 0

        # Chandelier trail (only AFTER profit threshold reached — fix from deep dive)
        from strategy import chandelier_stop
        new_sl = sl
        if profit_atr >= cfg.CHANDELIER_ACTIVATE_AT_ATR:
            chand = chandelier_stop(highs, lows, s['atr'], side)
            if side == 'LONG':
                new_sl = max(new_sl, chand)
            else:
                new_sl = min(new_sl, chand) if new_sl > 0 else chand

        # BE trail
        if side == 'LONG':
            if price - entry >= cfg.TRAIL_BE_AT_ATR * s['atr']:
                new_sl = max(new_sl, entry)
        else:
            if entry - price >= cfg.TRAIL_BE_AT_ATR * s['atr']:
                new_sl = min(new_sl, entry) if new_sl > 0 else entry

        # Update server-side SL if it actually moved meaningfully
        if abs(new_sl - sl) > s['atr'] * 0.1:
            state['stop_loss'] = new_sl
            save_state(state)
            try:
                cancel_all_orders()
                close_side = 'SELL' if side == 'LONG' else 'BUY'
                stop_market_order(close_side, round(new_sl, 2), qty)
                take_profit_order(close_side, round(state['take_profit'], 2), qty)
                log.info(f'  Trail: SL ${sl:,.0f} → ${new_sl:,.0f}')
            except Exception as e:
                log.warning(f'SL update failed: {e}')

        # Regime flip → exit
        flip = (side == 'LONG' and s['direction'] == 'SHORT') or \
               (side == 'SHORT' and s['direction'] == 'LONG')
        if flip:
            close_side = 'SELL' if side == 'LONG' else 'BUY'
            try:
                market_order(close_side, qty, reduce_only=True)
                cancel_all_orders()
                pnl = pos.get('unrealized_pnl', 0)
                log_pnl('CLOSE', side, price, qty, pnl, 'FLIP')
                save_state({})
                log.info(f'CLOSE {side} {qty:.3f} @ ${price:,.0f} | FLIP | PnL ${pnl:+,.2f}')
            except Exception as e:
                log.error(f'Close failed: {e}')
            return

        log.info(f'  {side} {qty:.3f} @ ${entry:,.0f} | uPnL=${pos.get("unrealized_pnl", 0):+.2f} | SL ${sl:,.0f}')
        return

    # ── No position: scan entry ───────────────────────────────────
    halted, why = is_halted(equity)
    if halted:
        log.info(f'  HALTED: {why}')
        return

    if not (s['long_signal'] or s['short_signal']):
        return

    side = 'LONG' if s['long_signal'] else 'SHORT'
    price = s['price']; atr_val = s['atr']
    qty = compute_qty(bal, price, atr_val)

    # Hard USD cap (safety rail for live trading)
    qty_usd_cap = cfg.MAX_POSITION_USD / price
    if qty > qty_usd_cap:
        log.info(f'  Qty capped by MAX_POSITION_USD: {qty:.3f} → {qty_usd_cap:.3f}')
        qty = round(qty_usd_cap, 3)

    if qty < 0.001:
        log.warning(f'  Qty too small: {qty}')
        return

    if side == 'LONG':
        sl_p = price - cfg.ATR_SL_MULT * atr_val
        tp_p = price + cfg.ATR_TP_MULT * atr_val
        order_side = 'BUY'; close_side = 'SELL'
    else:
        sl_p = price + cfg.ATR_SL_MULT * atr_val
        tp_p = price - cfg.ATR_TP_MULT * atr_val
        order_side = 'SELL'; close_side = 'BUY'

    # Open position
    try:
        result = market_order(order_side, qty)
        log.info(f'OPEN {side} order: {result}')
        if result.get('status') not in ('NEW', 'FILLED'):
            log.error(f'Order rejected: {result}')
            return
    except Exception as e:
        log.error(f'Open failed: {e}'); return

    # Place server-side SL + TP — CRITICAL: if SL fails, emergency close
    sl_placed = False
    try:
        sl_result = stop_market_order(close_side, round(sl_p, 2), qty)
        if 'error' not in str(sl_result).lower():
            sl_placed = True
        else:
            log.error(f'SL order rejected: {sl_result}')
    except Exception as e:
        log.error(f'SL placement EXCEPTION: {e}')

    if not sl_placed:
        log.error('⚠️ SL FAILED TO PLACE — emergency closing position')
        try:
            market_order(close_side, qty, reduce_only=True)
            log_pnl('CLOSE', side, price, qty, 0, 'SL_FAILED')
            save_state({})
            log.warning('Position emergency-closed due to missing SL')
        except Exception as e:
            log.critical(f'EMERGENCY CLOSE FAILED — POSITION UNPROTECTED: {e}')
        return

    try:
        take_profit_order(close_side, round(tp_p, 2), qty)
    except Exception as e:
        log.warning(f'TP placement failed (SL is in place, continuing): {e}')

    save_state({
        'side': side, 'entry_price': price, 'qty': qty,
        'stop_loss': sl_p, 'take_profit': tp_p,
        'entered_at': datetime.now().isoformat(),
    })
    log_pnl('OPEN', side, price, qty, 0, 'entry')
    log.info(f'OPEN {side} {qty:.3f} BTC @ ${price:,.0f} | SL ${sl_p:,.0f} | TP ${tp_p:,.0f}')


def run_loop():
    initialize()
    while True:
        if acquire_lock():
            try:
                tick()
            except Exception as e:
                log.error(f'Tick error: {e}', exc_info=True)
            finally:
                release_lock()
        else:
            log.warning('Lock held, skipping tick')
        time.sleep(cfg.LOOP_INTERVAL)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--once', action='store_true', help='Run a single tick')
    args = p.parse_args()

    if args.once:
        initialize()
        tick()
    else:
        run_loop()
