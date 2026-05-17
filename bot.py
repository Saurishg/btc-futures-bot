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
import os, sys, time, json, logging, threading
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config as cfg
from exchange import (
    set_leverage, set_margin_type, get_balance, get_position,
    get_klines, market_order, cancel_all_orders, get_open_orders,
    stop_market_order, take_profit_order,
)
from strategy import compute_signal, compute_signal_sym, compute_qty

if cfg.ADAPT_ENABLED:
    import adapt as _adapt


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

_adapt_lock    = threading.Lock()
_adapt_running = False
PNL_FILE   = Path(__file__).parent / 'pnl.json'
LOCK_FILE  = Path(__file__).parent / '.bot_lock'
PEAK_FILE  = Path(__file__).parent / '.peak_equity'


# ── Per-symbol state ─────────────────────────────────────────────────────

def state_file(sym: str) -> Path:
    return Path(__file__).parent / f'state_{sym}.json'

def load_state(sym: str) -> dict:
    sf = state_file(sym)
    return json.loads(sf.read_text()) if sf.exists() else {}

def save_state(sym: str, s: dict):
    state_file(sym).write_text(json.dumps(s, indent=2))

def log_pnl(action, side, price, qty, pnl=0, reason='', sym=''):
    rec = json.loads(PNL_FILE.read_text()) if PNL_FILE.exists() else []
    rec.append({
        'ts': datetime.now().isoformat(), 'action': action, 'side': side,
        'price': price, 'qty': qty, 'pnl': round(pnl, 2), 'reason': reason,
        'env': cfg.ENV, 'symbol': sym,
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


def force_close_position(pos: dict, reason: str, sym: str = None):
    """Close any open position at market — used by kill switch / daily halt."""
    if pos['side'] == 'NONE': return
    sym = sym or cfg.SYMBOL
    close_side = 'SELL' if pos['side'] == 'LONG' else 'BUY'
    try:
        market_order(close_side, pos['qty'], reduce_only=True, symbol=sym)
        cancel_all_orders(symbol=sym)
        log_pnl('CLOSE', pos['side'], 0, pos['qty'], pos.get('unrealized_pnl', 0), reason, sym)
        save_state(sym, {})
        log.warning(f'[{sym}] FORCE-CLOSED {pos["side"]} {pos["qty"]} — reason: {reason}')
    except Exception as e:
        log.error(f'[{sym}] Force-close failed: {e}')


# ── Main tick ────────────────────────────────────────────────────────────

def initialize():
    log.info(f'Multi-Symbol BTC Futures Bot')
    log.info(f'ENV={cfg.ENV.upper()} | URL={cfg.BASE_URL}')
    symbols_enabled = [s for s, p in cfg.SYMBOLS.items() if p.get('enabled', True)]
    log.info(f'Symbols={symbols_enabled} | TF={cfg.TF_ENTRY}/{cfg.TF_HTF} | Leverage={cfg.LEVERAGE}x | Margin={cfg.MARGIN_TYPE}')
    log.info(f'Risk: {cfg.RISK_PCT*100:.2f}%/trade | Max pos: ${cfg.MAX_POSITION_USD:,.0f} | Min bal: ${cfg.MIN_BALANCE_USD:,.0f}')
    log.info(f'Daily halt: {cfg.DAILY_LOSS_HALT_PCT*100}% | Max DD halt: {cfg.MAX_DD_HALT_PCT*100}% | Max concurrent: {cfg.MAX_CONCURRENT_POSITIONS}')
    if cfg.ENV == 'live':
        log.warning('LIVE MODE — REAL MONEY')

    # Startup state reconciliation for all symbols
    for sym in symbols_enabled:
        sf = state_file(sym)
        if sf.exists():
            try:
                saved = json.loads(sf.read_text())
                if saved.get('side'):
                    log.info(f'[{sym}] Recovered state: was {saved["side"]} {saved.get("qty","?")} @ ${saved.get("entry_price","?")}')
            except Exception as e:
                log.warning(f'[{sym}] State file unreadable: {e}')

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
        bal = get_balance()
        log.info(f'Balance: {bal:.2f} USDT')
    except Exception as e:
        log.error(f'Init failed: {e}')
        raise

    for sym in symbols_enabled:
        try:
            set_leverage(cfg.LEVERAGE, symbol=sym)
            set_margin_type(cfg.MARGIN_TYPE, symbol=sym)
            log.info(f'[{sym}] Leverage={cfg.LEVERAGE}x margin={cfg.MARGIN_TYPE} set')
        except Exception as e:
            log.error(f'[{sym}] Init failed: {e}')
            raise

    # ── Load learned params from last adaptation run ──────────────────
    if cfg.ADAPT_ENABLED:
        try:
            loaded = _adapt.load_and_apply_learned_params()
            if not loaded:
                log.info('No learned_params.json — using config.py defaults')
        except Exception as e:
            log.warning(f'Adapt load failed: {e} — using config.py defaults')


def tick():
    bal = get_balance()
    equity = bal

    # Count open positions across all symbols
    open_positions: dict = {}
    for sym in cfg.SYMBOLS:
        if not cfg.SYMBOLS[sym].get('enabled', True):
            continue
        pos = get_position(sym)
        if pos['side'] != 'NONE':
            open_positions[sym] = pos
            equity += pos.get('unrealized_pnl', 0)

    open_count = len(open_positions)

    sym_snapshots: dict = {}
    for sym, sym_params in cfg.SYMBOLS.items():
        if not sym_params.get('enabled', True):
            continue
        try:
            snap = tick_symbol(sym, sym_params, bal, equity, open_positions.get(sym), open_count)
            if snap:
                sym_snapshots[sym] = snap
        except Exception as e:
            log.error(f'[{sym}] tick error: {e}', exc_info=True)

    # Write multi-symbol indicators snapshot
    try:
        snapshot = {
            'ts': datetime.now().isoformat(),
            'env': cfg.ENV,
            'tf_entry': cfg.TF_ENTRY,
            'tf_htf': cfg.TF_HTF,
            'leverage': cfg.LEVERAGE,
            'balance': bal,
            'equity': equity,
            'open_count': open_count,
            'symbols': sym_snapshots,
        }
        (Path(__file__).parent / 'indicators.json').write_text(json.dumps(snapshot, indent=2))
    except Exception as e:
        log.warning(f'Snapshot write failed: {e}')


def tick_symbol(
    sym: str,
    sym_params: dict,
    bal: float,
    equity: float,
    pos,
    open_count: int,
) -> dict:
    """Handle one symbol per tick. Returns a signal snapshot dict or None."""
    from strategy import chandelier_stop

    klines_1h = get_klines(cfg.TF_ENTRY, 300, symbol=sym)
    klines_4h = get_klines(cfg.TF_HTF, 500, symbol=sym)
    if len(klines_1h) < 240 or len(klines_4h) < 400:
        log.warning(f'[{sym}] Not enough candles')
        return None

    s = compute_signal_sym(klines_1h, klines_4h, sym_params)
    if pos is None:
        pos = get_position(sym)
    state = load_state(sym)

    log.info(
        f'[{sym}] Price=${s["price"]:,.2f} | dir={s["direction"]} | RSI={s["rsi"]:.0f} | '
        f'ADX={s["adx"]:.0f}{"up" if s["adx_rising"] else ""} | '
        f'long_sig={s["long_signal"]} short_sig={s["short_signal"]}'
    )

    snap = {
        'price': s['price'], 'atr': s['atr'], 'rsi': s['rsi'],
        'adx': s['adx'], 'adx_rising': s['adx_rising'],
        'direction': s['direction'],
        'donchian_high': s['donchian_high'], 'donchian_low': s['donchian_low'],
        'body_atr': s['body_atr'], 'vol_ratio': s['vol_ratio'],
        'long_signal': s['long_signal'], 'short_signal': s['short_signal'],
        'has_pos': pos['side'] != 'NONE',
        'pos_side': pos['side'],
        'pos_qty': pos.get('qty', 0),
        'pos_entry': pos.get('entry', 0),
        'pos_unrealized': pos.get('unrealized_pnl', 0),
    }

    # ── Kill switch / daily halt while in position: force-close ───
    if pos['side'] != 'NONE':
        halted, why = is_halted(equity)
        if halted and ('KILL SWITCH' in why or 'daily loss' in why or 'max DD' in why):
            log.warning(f'[{sym}] Halt while in position: {why} — closing now')
            force_close_position(pos, why, sym=sym)
            return snap

    # ── In a position: manage exit ─────────────────────────────────
    if pos['side'] != 'NONE':
        side = pos['side']
        qty = pos['qty']
        entry = pos['entry']
        price = s['price']

        highs = [float(k[2]) for k in klines_1h]
        lows  = [float(k[3]) for k in klines_1h]
        sl = state.get('stop_loss', entry)

        if side == 'LONG':
            profit_atr = (price - entry) / s['atr'] if s['atr'] > 0 else 0
        else:
            profit_atr = (entry - price) / s['atr'] if s['atr'] > 0 else 0

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

        close_side = 'SELL' if side == 'LONG' else 'BUY'

        # Scale-out
        if cfg.SCALE_OUT_ENABLED and not state.get('scaled_out') and profit_atr >= cfg.SCALE_OUT_AT_ATR:
            scale_qty = round(qty * cfg.SCALE_OUT_FRACTION, 3)
            if scale_qty >= 0.001:
                try:
                    market_order(close_side, scale_qty, reduce_only=True, symbol=sym)
                    pnl_partial = pos.get('unrealized_pnl', 0) * cfg.SCALE_OUT_FRACTION
                    log_pnl('SCALE_OUT', side, price, scale_qty, pnl_partial, 'scale_out', sym)
                    state['scaled_out'] = True
                    save_state(sym, state)
                    qty = round(qty - scale_qty, 3)
                    log.info(f'[{sym}] SCALE-OUT {scale_qty:.3f} @ ${price:,.2f} | remaining {qty:.3f} | PnL ${pnl_partial:+.2f}')
                except Exception as e:
                    log.warning(f'[{sym}] Scale-out failed: {e}')

        # Update server-side SL if it moved meaningfully
        if abs(new_sl - sl) > s['atr'] * 0.1:
            state['stop_loss'] = new_sl
            save_state(sym, state)
            try:
                cancel_all_orders(symbol=sym)
                stop_market_order(close_side, round(new_sl, 2), qty, symbol=sym)
                take_profit_order(close_side, round(state['take_profit'], 2), qty, symbol=sym)
                log.info(f'[{sym}] Trail: SL ${sl:,.2f} -> ${new_sl:,.2f}')
            except Exception as e:
                log.warning(f'[{sym}] SL update failed: {e}')

        # Regime flip -> exit
        flip = (side == 'LONG' and s['direction'] == 'SHORT') or \
               (side == 'SHORT' and s['direction'] == 'LONG')
        if flip:
            try:
                market_order(close_side, qty, reduce_only=True, symbol=sym)
                cancel_all_orders(symbol=sym)
                pnl = pos.get('unrealized_pnl', 0)
                log_pnl('CLOSE', side, price, qty, pnl, 'FLIP', sym)
                save_state(sym, {})
                log.info(f'[{sym}] CLOSE {side} {qty:.3f} @ ${price:,.2f} | FLIP | PnL ${pnl:+,.2f}')
            except Exception as e:
                log.error(f'[{sym}] Close failed: {e}')
            return snap

        log.info(f'[{sym}] {side} {qty:.3f} @ ${entry:,.2f} | uPnL=${pos.get("unrealized_pnl", 0):+.2f} | SL ${sl:,.2f}')
        return snap

    # ── No position: scan entry ───────────────────────────────────
    halted, why = is_halted(equity)
    if halted:
        log.info(f'[{sym}] HALTED: {why}')
        return snap

    if open_count >= cfg.MAX_CONCURRENT_POSITIONS:
        log.info(f'[{sym}] Max concurrent positions ({cfg.MAX_CONCURRENT_POSITIONS}) reached, skipping entry')
        return snap

    if not (s['long_signal'] or s['short_signal']):
        return snap

    side = 'LONG' if s['long_signal'] else 'SHORT'
    price = s['price']
    atr_val = s['atr']

    if side == 'LONG':
        sl_mult = sym_params.get('long_sl_mult', cfg.LONG_ATR_SL_MULT)
        tp_mult = sym_params.get('long_tp_mult', cfg.LONG_ATR_TP_MULT)
        sl_p = price - sl_mult * atr_val
        tp_p = price + tp_mult * atr_val
        order_side = 'BUY'
        close_side = 'SELL'
    else:
        sl_mult = sym_params.get('short_sl_mult', cfg.ATR_SL_MULT)
        tp_mult = sym_params.get('short_tp_mult', cfg.ATR_TP_MULT)
        sl_p = price + sl_mult * atr_val
        tp_p = price - tp_mult * atr_val
        order_side = 'SELL'
        close_side = 'BUY'

    qty = compute_qty(bal, price, atr_val, sl_mult)

    qty_usd_cap = cfg.MAX_POSITION_USD / price
    if qty > qty_usd_cap:
        log.info(f'[{sym}] Qty capped by MAX_POSITION_USD: {qty:.3f} -> {qty_usd_cap:.3f}')
        qty = round(qty_usd_cap, 3)

    if qty < 0.001:
        log.warning(f'[{sym}] Qty too small: {qty}')
        return snap

    try:
        result = market_order(order_side, qty, symbol=sym)
        log.info(f'[{sym}] OPEN {side} order: {result}')
        if result.get('status') not in ('NEW', 'FILLED'):
            log.error(f'[{sym}] Order rejected: {result}')
            return snap
    except Exception as e:
        log.error(f'[{sym}] Open failed: {e}')
        return snap

    # Place server-side SL + TP — CRITICAL: if SL fails, emergency close
    sl_placed = False
    try:
        sl_result = stop_market_order(close_side, round(sl_p, 2), qty, symbol=sym)
        if sl_result.get('orderId'):
            sl_placed = True
        else:
            log.error(f'[{sym}] SL order rejected: {sl_result}')
    except Exception as e:
        log.error(f'[{sym}] SL placement EXCEPTION: {e}')

    if not sl_placed:
        log.error(f'[{sym}] SL FAILED TO PLACE — emergency closing position')
        try:
            market_order(close_side, qty, reduce_only=True, symbol=sym)
            log_pnl('CLOSE', side, price, qty, 0, 'SL_FAILED', sym)
            save_state(sym, {})
            log.warning(f'[{sym}] Position emergency-closed due to missing SL')
        except Exception as e:
            log.critical(f'[{sym}] EMERGENCY CLOSE FAILED — POSITION UNPROTECTED: {e}')
        return snap

    try:
        take_profit_order(close_side, round(tp_p, 2), qty, symbol=sym)
    except Exception as e:
        log.warning(f'[{sym}] TP placement failed (SL is in place, continuing): {e}')

    save_state(sym, {
        'side': side, 'entry_price': price, 'qty': qty,
        'stop_loss': sl_p, 'take_profit': tp_p,
        'entered_at': datetime.now().isoformat(),
    })
    log_pnl('OPEN', side, price, qty, 0, 'entry', sym)
    log.info(f'[{sym}] OPEN {side} {qty:.3f} @ ${price:,.2f} | SL ${sl_p:,.2f} | TP ${tp_p:,.2f}')
    return snap


def run_loop():
    global _adapt_running

    initialize()

    _next_adapt = (
        time.time() + cfg.ADAPT_INTERVAL_DAYS * 86400
        if cfg.ADAPT_ENABLED else float('inf')
    )

    def _adapt_thread():
        global _adapt_running
        try:
            updated = _adapt.run_adaptation()
            if updated:
                log.info('Adaptation done — new params active on next tick')
            else:
                log.info('Adaptation done — no param change')
        except Exception as e:
            log.error(f'Adaptation failed: {e}', exc_info=True)
        finally:
            _adapt_running = False

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

        # ── Periodic parameter adaptation (background thread) ─────────
        if cfg.ADAPT_ENABLED and time.time() >= _next_adapt and not _adapt_running:
            try:
                in_pos = any(
                    get_position(sym)['side'] != 'NONE'
                    for sym in cfg.SYMBOLS
                    if cfg.SYMBOLS[sym].get('enabled', True)
                )
            except Exception:
                in_pos = True  # conservative: assume in position on API error
            if not in_pos:
                with _adapt_lock:
                    if not _adapt_running:
                        _adapt_running = True
                        _next_adapt = time.time() + cfg.ADAPT_INTERVAL_DAYS * 86400
                        t = threading.Thread(target=_adapt_thread, daemon=True, name='adapt')
                        t.start()
                        log.info(f'Adapt thread launched (next in {cfg.ADAPT_INTERVAL_DAYS}d)')
            else:
                log.info('Adaptation deferred — position is open')

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
