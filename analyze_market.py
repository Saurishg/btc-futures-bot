"""Study current market regime for BTC/ETH/SOL futures and identify which
filters of the breakout strategy are binding right now. Pulls fresh 90d of
1h + 4h klines from Binance public API and reuses freq_sweep's indicator
helpers + backtest engine for consistency with the live strategy.

Output: per symbol, a current-state snapshot, 90d regime stats, filter
satisfaction rates, and the binding constraint analysis. Plus a short
recent-window backtest to compare with the 5y-validated params.
"""
import sys, time
from pathlib import Path
import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
import freq_sweep as fs

BASE = "https://fapi.binance.com"
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']

# Live SYMBOLS params (mirror config.py after 2026-05-27 changes)
LIVE = {
    'BTCUSDT': dict(short_adx_min=43, short_adx_max=70, short_body=0.6, short_vol=1.16,
                    short_sl=1.9, short_tp=4.0, shorts_only=False,
                    long_adx_min=40, long_adx_max=55, long_body=1.5, long_vol=1.2,
                    long_sl=2.0, long_tp=6.0),
    'ETHUSDT': dict(short_adx_min=45, short_adx_max=55, short_body=0.5, short_vol=1.0,
                    short_sl=1.5, short_tp=3.0, shorts_only=True),
    'SOLUSDT': dict(short_adx_min=50, short_adx_max=55, short_body=0.8, short_vol=1.0,
                    short_sl=2.0, short_tp=4.0, shorts_only=True),
}


def fetch_klines(symbol, interval, bars_needed):
    end = int(time.time() * 1000)
    out = []
    cur_end = end
    while len(out) < bars_needed:
        r = requests.get(f"{BASE}/fapi/v1/klines", params={
            'symbol': symbol, 'interval': interval, 'limit': 1500, 'endTime': cur_end
        }, timeout=15).json()
        if not r:
            break
        out = r + out
        cur_end = r[0][0] - 1
        if len(r) < 1500:
            break
    df = pd.DataFrame(out, columns=['open_time','open','high','low','close','volume',
                                     'close_time','qa','n','tbav','tqav','ignore'])
    for c in ['open','high','low','close','volume']:
        df[c] = df[c].astype(float)
    df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    df = df.set_index('timestamp')[['open','high','low','close','volume']]
    return df.tail(bars_needed).copy()


def features(df1h, df4h):
    ef = fs._ema(df4h['close'], 50); es = fs._ema(df4h['close'], 200)
    df4h = df4h.copy()
    df4h['direction'] = np.where(ef > es*1.001, 'LONG',
                                 np.where(ef < es*0.999, 'SHORT', 'NONE'))
    df1h = df1h.copy().sort_index()
    df4h = df4h.sort_index()
    df1h['direction'] = pd.merge_asof(df1h.reset_index(),
                                       df4h[['direction']].reset_index(),
                                       on='timestamp', direction='backward')['direction'].values
    df1h['atr'] = fs._atr(df1h['high'], df1h['low'], df1h['close'], 14)
    df1h['adx'] = fs._adx(df1h['high'], df1h['low'], df1h['close'], 14)
    df1h['adx_up'] = df1h['adx'] > df1h['adx'].shift(5)
    df1h['donch_hi'] = df1h['high'].shift(1).rolling(20).max()
    df1h['donch_lo'] = df1h['low'].shift(1).rolling(20).min()
    body = (df1h['close'] - df1h['open']).abs()
    df1h['body_atr'] = body / df1h['atr']
    df1h['bull_candle'] = df1h['close'] > df1h['open']
    df1h['bear_candle'] = df1h['close'] < df1h['open']
    # back-compat aliases (some helpers below use the short names)
    df1h['bull'] = df1h['bull_candle']
    df1h['bear'] = df1h['bear_candle']
    vol_ma = df1h['volume'].shift(1).rolling(20).mean()
    df1h['vol_ratio'] = df1h['volume'] / vol_ma
    df1h['low_20'] = df1h['low'].rolling(20).min()
    df1h['high_20'] = df1h['high'].rolling(20).max()
    df1h['year'] = df1h.index.year
    return df1h.dropna().copy()


def filter_analysis(df, side, p):
    # SHORT filter component truth tables
    if side == 'SHORT':
        dir_ok    = (df['direction'] == 'SHORT').values
        break_ok  = (df['close'] < df['donch_lo']).values
        cand_ok   = df['bear'].values
        adx_in    = ((df['adx'] >= p['short_adx_min']) & (df['adx'] < p['short_adx_max'])).values
        adx_up    = df['adx_up'].values
        body_ok   = (df['body_atr'] >= p['short_body']).values
        vol_ok    = (df['vol_ratio'] >= p['short_vol']).values
    else:
        dir_ok    = (df['direction'] == 'LONG').values
        break_ok  = (df['close'] > df['donch_hi']).values
        cand_ok   = df['bull'].values
        adx_in    = ((df['adx'] >= p['long_adx_min']) & (df['adx'] < p['long_adx_max'])).values
        adx_up    = df['adx_up'].values
        body_ok   = (df['body_atr'] >= p['long_body']).values
        vol_ok    = (df['vol_ratio'] >= p['long_vol']).values
    return dict(dir=dir_ok, brk=break_ok, cand=cand_ok, adx_in=adx_in,
                adx_up=adx_up, body=body_ok, vol=vol_ok)


def report(sym, p):
    print(f"\n{'='*72}\n  {sym}\n{'='*72}")
    df1h = fetch_klines(sym, '1h', 24*95)   # ~95d 1h
    df4h = fetch_klines(sym, '4h',  6*100)  # ~100d 4h (HTF)
    df   = features(df1h, df4h)
    # restrict to last ~90d after warmup
    df = df.tail(24*90).copy()

    last = df.iloc[-1]
    print(f"NOW ({df.index[-1].strftime('%Y-%m-%d %H:%M UTC')}):")
    print(f"  price=${last['close']:,.2f}  dir={last['direction']}  "
          f"ADX={last['adx']:.1f}  body_atr={last['body_atr']:.2f}  "
          f"vol_ratio={last['vol_ratio']:.2f}  RSI=n/a")
    if last['direction'] == 'SHORT':
        gap_break = last['close'] - last['donch_lo']
        gap_adx_lo = p['short_adx_min'] - last['adx']
        print(f"  distance to short signal: close vs donch_lo {gap_break:+.2f}  "
              f"ADX {last['adx']:.1f} (need >= {p['short_adx_min']}, gap {gap_adx_lo:+.1f})")
    elif last['direction'] == 'LONG' and not p.get('shorts_only'):
        gap_break = last['close'] - last['donch_hi']
        gap_adx_lo = p.get('long_adx_min', 40) - last['adx']
        print(f"  distance to long signal: close vs donch_hi {gap_break:+.2f}  "
              f"ADX gap {gap_adx_lo:+.1f}")

    print(f"\n90d regime ({len(df)} bars, {df.index[0].date()} -> {df.index[-1].date()}):")
    for d in ['LONG','SHORT','NONE']:
        pct = (df['direction']==d).mean()*100
        if pct > 0.5: print(f"  dir={d:5}: {pct:5.1f}% of bars")
    print(f"  ADX:       mean {df['adx'].mean():5.1f}  median {df['adx'].median():5.1f}  "
          f"p75 {df['adx'].quantile(.75):5.1f}  p90 {df['adx'].quantile(.9):5.1f}  max {df['adx'].max():5.1f}")
    print(f"  body_atr:  mean {df['body_atr'].mean():5.2f}  p75 {df['body_atr'].quantile(.75):5.2f}  "
          f"p90 {df['body_atr'].quantile(.9):5.2f}")
    print(f"  vol_ratio: mean {df['vol_ratio'].mean():5.2f}  p75 {df['vol_ratio'].quantile(.75):5.2f}  "
          f"p90 {df['vol_ratio'].quantile(.9):5.2f}")
    rng = (df['high'] - df['low']) / df['atr']
    print(f"  hourly range/ATR: mean {rng.mean():.2f}  (≈1.0 = normal, <1 = compressed, >1 = expanded)")

    # SHORT analysis (all 3 symbols trade short)
    f = filter_analysis(df, 'SHORT', p)
    all_filt = f['dir'] & f['brk'] & f['cand'] & f['adx_in'] & f['adx_up'] & f['body'] & f['vol']
    print(f"\n  SHORT filter satisfaction (90d, % of bars):")
    print(f"    dir=SHORT:        {f['dir'].mean()*100:5.1f}%")
    print(f"    Donchian break:   {f['brk'].mean()*100:5.1f}%")
    print(f"    bear candle:      {f['cand'].mean()*100:5.1f}%")
    print(f"    ADX in [{p['short_adx_min']},{p['short_adx_max']}): {f['adx_in'].mean()*100:5.1f}%")
    print(f"    ADX rising:       {f['adx_up'].mean()*100:5.1f}%")
    print(f"    body>={p['short_body']}:        {f['body'].mean()*100:5.1f}%")
    print(f"    vol>={p['short_vol']}:        {f['vol'].mean()*100:5.1f}%")
    print(f"    ALL satisfied:    {all_filt.sum()} bars in last 90d (current params)")

    # "Trades if we drop each filter" - identify the most binding
    base = f['dir'] & f['brk'] & f['cand'] & f['adx_in'] & f['adx_up'] & f['body'] & f['vol']
    drop = {
        'drop ADX band':   (f['dir'] & f['brk'] & f['cand'] & f['adx_up'] & f['body'] & f['vol']).sum(),
        'drop ADX rising': (f['dir'] & f['brk'] & f['cand'] & f['adx_in'] & f['body'] & f['vol']).sum(),
        'drop body':       (f['dir'] & f['brk'] & f['cand'] & f['adx_in'] & f['adx_up'] & f['vol']).sum(),
        'drop vol':        (f['dir'] & f['brk'] & f['cand'] & f['adx_in'] & f['adx_up'] & f['body']).sum(),
        'drop bear cand':  (f['dir'] & f['brk'] & f['adx_in'] & f['adx_up'] & f['body'] & f['vol']).sum(),
        'drop breakout':   (f['dir'] & f['cand'] & f['adx_in'] & f['adx_up'] & f['body'] & f['vol']).sum(),
    }
    print(f"  Bars firing if we DROP one filter (vs base {base.sum()}):")
    for k, v in sorted(drop.items(), key=lambda x: -x[1]):
        gain = v - base.sum()
        marker = ' <- biggest blocker' if gain == max(drop.values()) - base.sum() and gain > 0 else ''
        print(f"    {k:20} -> {v:3d}  (+{gain}){marker}")

    # recent-window backtests (using freq_sweep's bt_short engine)
    orig_warmup = fs.WARMUP; fs.WARMUP = 30
    try:
        # SHORT: compare current vs looser ADX floors
        print('\n  90d SHORT backtest — ADX floor comparison:')
        print(f'    {"variant":35} {"N":>3} {"PF":>5} {"WR%":>5} {"Ret%":>6} {"DD%":>5}')
        floors = sorted({p['short_adx_min'], 40, 38, 35})
        for floor in floors:
            r = fs.bt_short(df.copy(), floor, p['short_adx_max'],
                             p['short_body'], p['short_vol'],
                             sl_m=p['short_sl'], tp_m=p['short_tp'])
            cur = ' <-- live' if floor == p['short_adx_min'] else ''
            print(f'    ADX[{floor:2d}-{p["short_adx_max"]}) body>={p["short_body"]} vol>={p["short_vol"]}      '
                  f'{r["n"]:>3} {r["pf"]:>5.2f} {r["wr"]:>5.1f} {r["ret"]:>+6.1f} {r["dd"]:>5.1f}{cur}')

        # LONG viability check (also for shorts-only symbols — see if regime supports turning them on)
        long_params_default = dict(adx_min=40, adx_max=55, body=1.5, vol=1.2, sl=2.0, tp=6.0)
        lp = long_params_default if p.get('shorts_only') else dict(
            adx_min=p['long_adx_min'], adx_max=p['long_adx_max'],
            body=p['long_body'], vol=p['long_vol'],
            sl=p['long_sl'], tp=p['long_tp'])
        rl = fs.bt_long(df.copy(), lp['adx_min'], lp['adx_max'],
                         lp['body'], lp['vol'], sl_m=lp['sl'], tp_m=lp['tp'])
        tag = 'live' if not p.get('shorts_only') else 'HYPOTHETICAL (currently shorts_only=True)'
        print(f"\n  90d LONG  backtest ({tag}): "
              f"ADX[{lp['adx_min']}-{lp['adx_max']}) body>={lp['body']} "
              f"-> n={rl['n']} PF={rl['pf']:.2f} WR={rl['wr']:.1f}% ret={rl['ret']:+.1f}% DD={rl['dd']:.1f}%")
        # also test a looser LONG for shorts-only symbols (since regime is LONG-heavy)
        if p.get('shorts_only'):
            for am, body in [(30, 1.0), (30, 0.8), (25, 0.8)]:
                rr = fs.bt_long(df.copy(), am, 60, body, 1.0, sl_m=2.0, tp_m=4.0)
                print(f"     looser LONG ADX[{am}-60) body>={body}: n={rr['n']} PF={rr['pf']:.2f} WR={rr['wr']:.1f}% ret={rr['ret']:+.1f}%")
    finally:
        fs.WARMUP = orig_warmup


if __name__ == '__main__':
    for sym in SYMBOLS:
        try:
            report(sym, LIVE[sym])
        except Exception as e:
            print(f"\n[{sym}] error: {e}")
