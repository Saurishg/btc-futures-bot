"""Binance USDⓈ-M Futures API client (testnet or live)."""
import time, hmac, hashlib, requests
import config as cfg


def _sign(params: dict) -> dict:
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 5000
    query = '&'.join(f'{k}={v}' for k, v in params.items())
    sig = hmac.new(cfg.API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params['signature'] = sig
    return params


def _headers():
    return {'X-MBX-APIKEY': cfg.API_KEY}


def fapi_get(path, params=None, signed=True):
    p = dict(params or {})
    if signed: p = _sign(p)
    r = requests.get(cfg.BASE_URL + path, params=p, headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def fapi_post(path, params=None):
    p = _sign(dict(params or {}))
    r = requests.post(cfg.BASE_URL + path, params=p, headers=_headers(), timeout=10)
    if r.status_code >= 400:
        try: return r.json()
        except: r.raise_for_status()
    return r.json()


def fapi_delete(path, params=None):
    p = _sign(dict(params or {}))
    r = requests.delete(cfg.BASE_URL + path, params=p, headers=_headers(), timeout=10)
    return r.json() if r.status_code < 400 else {'error': r.text}


# ── Account setup ────────────────────────────────────────────────────────

def _sym(symbol) -> str:
    """Resolve symbol, falling back to cfg.SYMBOL if None."""
    return symbol if symbol is not None else cfg.SYMBOL


def set_leverage(leverage: int, symbol: str = None):
    return fapi_post('/fapi/v1/leverage', {'symbol': _sym(symbol), 'leverage': leverage})


def set_margin_type(margin_type: str, symbol: str = None):
    try:
        return fapi_post('/fapi/v1/marginType', {'symbol': _sym(symbol), 'marginType': margin_type})
    except Exception as e:
        # already set is fine
        if 'No need to change' in str(e): return {'msg': 'already set'}
        return {'error': str(e)}


def get_balance() -> float:
    """Available USDT balance for futures."""
    for asset in fapi_get('/fapi/v2/balance'):
        if asset['asset'] == 'USDT':
            return float(asset['availableBalance'])
    return 0.0


def get_position(symbol: str = None) -> dict:
    """Returns current position info or {'side':'NONE',...}"""
    sym = _sym(symbol)
    for p in fapi_get('/fapi/v2/positionRisk', {'symbol': sym}):
        if p['symbol'] == sym:
            qty = float(p['positionAmt'])
            if abs(qty) > 0.0001:
                return {
                    'side': 'LONG' if qty > 0 else 'SHORT',
                    'qty': abs(qty),
                    'entry': float(p['entryPrice']),
                    'unrealized_pnl': float(p['unRealizedProfit']),
                    'leverage': int(p['leverage']),
                    'mark_price': float(p['markPrice']),
                }
    return {'side': 'NONE', 'qty': 0.0, 'entry': 0.0}


# ── Market data (no auth) ────────────────────────────────────────────────

def get_klines(interval: str, limit: int = 250, symbol: str = None) -> list:
    return fapi_get('/fapi/v1/klines', {
        'symbol': _sym(symbol), 'interval': interval, 'limit': limit
    }, signed=False)


def get_mark_price(symbol: str = None) -> float:
    return float(fapi_get('/fapi/v1/premiumIndex', {'symbol': _sym(symbol)}, signed=False)['markPrice'])


# ── Orders ───────────────────────────────────────────────────────────────

def market_order(side: str, qty: float, reduce_only: bool = False, symbol: str = None) -> dict:
    params = {
        'symbol': _sym(symbol), 'side': side,
        'type': 'MARKET', 'quantity': f'{qty:.3f}',
    }
    if reduce_only:
        params['reduceOnly'] = 'true'
    return fapi_post('/fapi/v1/order', params)


def stop_market_order(side: str, stop_price: float, qty: float, symbol: str = None) -> dict:
    """Server-side stop loss. side = direction to close (SELL for LONG, BUY for SHORT)."""
    return fapi_post('/fapi/v1/order', {
        'symbol': _sym(symbol), 'side': side,
        'type': 'STOP_MARKET', 'stopPrice': f'{stop_price:.2f}',
        'quantity': f'{qty:.3f}', 'reduceOnly': 'true',
        'timeInForce': 'GTE_GTC',
    })


def take_profit_order(side: str, stop_price: float, qty: float, symbol: str = None) -> dict:
    return fapi_post('/fapi/v1/order', {
        'symbol': _sym(symbol), 'side': side,
        'type': 'TAKE_PROFIT_MARKET', 'stopPrice': f'{stop_price:.2f}',
        'quantity': f'{qty:.3f}', 'reduceOnly': 'true',
        'timeInForce': 'GTE_GTC',
    })


def cancel_all_orders(symbol: str = None) -> dict:
    return fapi_delete('/fapi/v1/allOpenOrders', {'symbol': _sym(symbol)})


def get_open_orders(symbol: str = None) -> list:
    return fapi_get('/fapi/v1/openOrders', {'symbol': _sym(symbol)})
