"""One-off: sweep SHORT breakout params for ETH/SOL using the same backtest
engine as freq_sweep.py (closed-bar, identical filter logic to live strategy)."""
import sys
import numpy as np
import pandas as pd
from pathlib import Path

import freq_sweep as fs  # reuse _ema/_atr/_adx/bt_short/_simulate (no main() on import)

DATA = Path(__file__).parent / 'data'

FILES = {
    'ETHUSDT': ('eth_1h_5y.csv', 'eth_4h_5y.csv', dict(sl_m=1.5, tp_m=3.0)),
    'SOLUSDT': ('sol_1h_5y.csv', 'sol_4h_5y.csv', dict(sl_m=2.0, tp_m=4.0)),
}


def load_df(f1h, f4h):
    df1h = pd.read_csv(DATA / f1h, parse_dates=['timestamp']).set_index('timestamp')
    df4h = pd.read_csv(DATA / f4h, parse_dates=['timestamp']).set_index('timestamp')
    ef = fs._ema(df4h['close'], 50); es = fs._ema(df4h['close'], 200)
    df4h['direction'] = np.where(ef > es * 1.001, 'LONG', np.where(ef < es * 0.999, 'SHORT', 'NONE'))
    df1h = df1h.sort_index(); df4h = df4h.sort_index()
    df1h['direction'] = pd.merge_asof(
        df1h.reset_index(), df4h[['direction']].reset_index(),
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
    df1h['vol_ma20'] = df1h['volume'].shift(1).rolling(20).mean()
    df1h['vol_ratio'] = df1h['volume'] / df1h['vol_ma20']
    df1h['low_20'] = df1h['low'].rolling(20).min()
    df1h['high_20'] = df1h['high'].rolling(20).max()
    df1h['year'] = df1h.index.year
    return df1h.dropna().copy()


for sym, (f1h, f4h, exits) in FILES.items():
    df = load_df(f1h, f4h)
    ny = (df.index[-1] - df.index[0]).days / 365.25
    print(f'\n{"="*70}\n{sym} SHORT — {len(df)} bars, {ny:.1f}y  (sl={exits["sl_m"]} tp={exits["tp_m"]})\n{"="*70}')
    print(f'  {"ADX range":>11} {"body":>4} {"vol":>4} {"N":>4} {"/yr":>4} {"WR%":>5} {"PF":>5} {"Ret%":>6} {"DD%":>5} {"Yrs":>4}')
    # current settings per symbol first, then progressively looser
    if sym == 'ETHUSDT':
        combos = [(45,55,0.5,1.0),(45,65,0.5,1.0),(43,70,0.5,1.0),(40,70,0.5,1.0),
                  (40,70,0.6,1.0),(40,70,0.6,1.2),(38,72,0.5,1.0)]
    else:
        combos = [(50,55,0.8,1.0),(48,65,0.8,1.0),(45,70,0.6,1.0),(43,70,0.6,1.0),
                  (40,70,0.6,1.0),(40,70,0.6,1.2),(38,72,0.6,1.0)]
    for am, ax, bm, vm in combos:
        r = fs.bt_short(df, am, ax, bm, vm, sl_m=exits['sl_m'], tp_m=exits['tp_m'])
        if r['n'] == 0:
            print(f'  [{am:2d}-{ax:2d})     {bm:>4} {vm:>4}    0    —     —     —      —     —   —'); continue
        cur = ' <-- current' if (sym=='ETHUSDT' and (am,ax,bm,vm)==(45,55,0.5,1.0)) or \
                                 (sym=='SOLUSDT' and (am,ax,bm,vm)==(50,55,0.8,1.0)) else ''
        print(f'  [{am:2d}-{ax:2d})     {bm:>4} {vm:>4} {r["n"]:>4} {r["n"]/ny:>4.1f} {r["wr"]:>5.1f} '
              f'{r["pf"]:>5.2f} {r["ret"]:>+6.1f} {r["dd"]:>5.1f} {r["yp"]}/{r["yt"]}{cur}')
