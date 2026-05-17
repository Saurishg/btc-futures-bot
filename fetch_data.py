"""Fetch historical 15m & 1h BTCUSDT klines from Binance Futures (public endpoint)."""
import csv, time, requests
from datetime import datetime, timedelta
from pathlib import Path

OUT_DIR = Path(__file__).parent / 'data'
OUT_DIR.mkdir(exist_ok=True)

BASE = 'https://fapi.binance.com'
SYMBOL = 'BTCUSDT'


def fetch_klines(interval: str, start_ms: int, end_ms: int) -> list:
    """Fetch klines paginated. Returns list of klines."""
    out = []
    cur = start_ms
    while cur < end_ms:
        params = {
            'symbol': SYMBOL, 'interval': interval,
            'startTime': cur, 'endTime': end_ms,
            'limit': 1500,
        }
        r = requests.get(f'{BASE}/fapi/v1/klines', params=params, timeout=15)
        r.raise_for_status()
        batch = r.json()
        if not batch: break
        out.extend(batch)
        cur = batch[-1][0] + 1
        time.sleep(0.15)  # rate-limit politeness
    return out


def save_csv(klines: list, path: Path):
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        for k in klines:
            ts = datetime.utcfromtimestamp(k[0]/1000).isoformat()
            w.writerow([ts, k[1], k[2], k[3], k[4], k[5]])


if __name__ == '__main__':
    # 5 years of history
    end = datetime.utcnow()
    start = end - timedelta(days=365 * 5)
    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(end.timestamp()   * 1000)

    print(f'Fetching 15m klines from {start.date()} to {end.date()}...')
    k15 = fetch_klines('15m', start_ms, end_ms)
    save_csv(k15, OUT_DIR / 'btc_15m.csv')
    print(f'  {len(k15)} 15m bars saved')

    print(f'Fetching 1h klines (HTF)...')
    k1h = fetch_klines('1h', start_ms, end_ms)
    save_csv(k1h, OUT_DIR / 'btc_1h.csv')
    print(f'  {len(k1h)} 1h bars saved')

    print('Done.')
