"""
Full 5-year sweep on 15m BTCUSDT with 1h as HTF trend filter.
Same breakout logic as current strategy — just 4× more bars.
"""
import pandas as pd
import numpy as np
from itertools import product
from pathlib import Path

DATA_DIR = Path(__file__).parent / 'data'
WARMUP = 1000  # 1000 × 15m = 10.4 days burn-in
FEE    = 0.0004
RISK   = 0.02
MPOS   = 0.30
INIT   = 10_000.0

def _ema(s, n): return s.ewm(span=n, adjust=False).mean()
def _atr(h,l,c,n=14):
    pc=c.shift(1); tr=pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()   # Wilder smoothing for 15m
def _adx(h,l,c,n=14):
    pc=c.shift(1); tr=pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
    up=h-h.shift(1); dn=l.shift(1)-l
    pdm=pd.Series(np.where((up>dn)&(up>0),up,0.0),index=h.index)
    ndm=pd.Series(np.where((dn>up)&(dn>0),dn,0.0),index=h.index)
    atr_s=tr.ewm(alpha=1/n,adjust=False).mean()
    pdi=100*pdm.ewm(alpha=1/n,adjust=False).mean()/atr_s
    ndi=100*ndm.ewm(alpha=1/n,adjust=False).mean()/atr_s
    dx=(100*(pdi-ndi).abs()/(pdi+ndi).replace(0,np.nan)).fillna(0)
    return dx.ewm(alpha=1/n,adjust=False).mean()

def load_df():
    f15 = DATA_DIR / 'btc_15m.csv'
    f1h = DATA_DIR / 'btc_1h.csv'
    if not f1h.exists():
        f1h = DATA_DIR / 'btc_1h_5y.csv'

    df15 = pd.read_csv(f15, parse_dates=['timestamp']).set_index('timestamp')
    df1h = pd.read_csv(f1h, parse_dates=['timestamp']).set_index('timestamp')

    # 1h trend (HTF)
    ef = _ema(df1h['close'], 50); es = _ema(df1h['close'], 200)
    df1h['direction'] = np.where(ef>es*1.001,'LONG', np.where(ef<es*0.999,'SHORT','NONE'))
    df15 = df15.sort_index(); df1h = df1h.sort_index()
    df15['direction'] = pd.merge_asof(
        df15.reset_index(), df1h[['direction']].reset_index(),
        on='timestamp', direction='backward')['direction'].values

    # 15m indicators — use 20-bar Donchian (5h breakout on 15m)
    BRKT = 20
    df15['atr']      = _atr(df15['high'], df15['low'], df15['close'], 14)
    df15['adx']      = _adx(df15['high'], df15['low'], df15['close'], 14)
    df15['adx_up']   = df15['adx'] > df15['adx'].shift(5)
    df15['donch_hi'] = df15['high'].shift(1).rolling(BRKT).max()
    df15['donch_lo'] = df15['low'].shift(1).rolling(BRKT).min()
    body             = (df15['close'] - df15['open']).abs()
    df15['body_atr'] = body / df15['atr']
    df15['bull_candle'] = df15['close'] > df15['open']
    df15['bear_candle'] = df15['close'] < df15['open']
    df15['vol_ma20']    = df15['volume'].shift(1).rolling(20).mean()
    df15['vol_ratio']   = df15['volume'] / df15['vol_ma20']
    df15['low_20']      = df15['low'].rolling(20).min()
    df15['high_20']     = df15['high'].rolling(20).max()
    df15['year']        = df15.index.year
    return df15.dropna().copy()


def simulate(df, entry_ok, sl_m, tp_m, side):
    closes=df['close'].values; highs=df['high'].values; lows=df['low'].values
    atrs=df['atr'].values; hi20=df['high_20'].values; lo20=df['low_20'].values
    years=df['year'].values
    cash=INIT; peak=INIT
    in_pos=False; entry_p=sl=tp=qty=0.0; entry_fee=0.0; scaled=False; scale_pnl=0.0
    trades=[]; eq_curve=[]; n=len(df)
    SCALE_AT=1.0; SCALE_F=0.5; CHAND_M=3.0; CHAND_ACT=1.5; BE_AT=1.0
    for i in range(n):
        if i<WARMUP: continue
        price=closes[i]; h=highs[i]; l=lows[i]; a=atrs[i]
        if not np.isfinite(a) or a<=0: eq_curve.append(cash); continue
        equity=(cash+qty*(price-entry_p) if side=='LONG' else cash+qty*(entry_p-price)) if in_pos else cash
        peak=max(peak,equity); eq_curve.append(equity)
        if in_pos:
            if side=='LONG':
                pr_max=(h-entry_p)/a
                if not scaled and pr_max>=SCALE_AT:
                    sp=entry_p+SCALE_AT*a; sq=qty*SCALE_F
                    cash+=sq*sp+(sp-entry_p)*sq-FEE*sp*sq-entry_fee*SCALE_F
                    qty-=sq; scaled=True; scale_pnl=(sp-entry_p)*sq-FEE*sp*sq-entry_fee*SCALE_F; sl=entry_p
                if (price-entry_p)/a>=CHAND_ACT: sl=max(sl, hi20[i]-CHAND_M*a)
                if (h-entry_p)>=BE_AT*a: sl=max(sl,entry_p)
                exit_p=reason=None
                if l<=sl: exit_p,reason=sl,'SL'
                elif h>=tp: exit_p,reason=tp,'TP'
                if exit_p:
                    rem=entry_fee*(1-SCALE_F) if scaled else entry_fee
                    pnl_g=(exit_p-entry_p)*qty; fee_e=FEE*exit_p*qty
                    cash+=qty*exit_p
                    trades.append({'year':years[i],'pnl':pnl_g-fee_e-rem+scale_pnl})
                    in_pos=False; qty=0.0; entry_fee=0.0; scaled=False; scale_pnl=0.0
            else:
                pr_max=(entry_p-l)/a
                if not scaled and pr_max>=SCALE_AT:
                    sp=entry_p-SCALE_AT*a; sq=qty*SCALE_F
                    pnl_s=(entry_p-sp)*sq-FEE*sp*sq-entry_fee*SCALE_F
                    cash+=sq*entry_p+pnl_s; qty-=sq; scaled=True; scale_pnl=pnl_s; sl=entry_p
                if (entry_p-price)/a>=CHAND_ACT:
                    chand=lo20[i]+CHAND_M*a; sl=min(sl,chand) if sl>0 else chand
                if (entry_p-l)>=BE_AT*a: sl=min(sl,entry_p) if sl>0 else entry_p
                exit_p=reason=None
                if h>=sl: exit_p,reason=sl,'SL'
                elif l<=tp: exit_p,reason=tp,'TP'
                if exit_p:
                    rem=entry_fee*(1-SCALE_F) if scaled else entry_fee
                    pnl_g=(entry_p-exit_p)*qty; fee_e=FEE*exit_p*qty
                    cash+=qty*entry_p+(pnl_g-fee_e-rem)
                    trades.append({'year':years[i],'pnl':pnl_g-fee_e-rem+scale_pnl})
                    in_pos=False; qty=0.0; entry_fee=0.0; scaled=False; scale_pnl=0.0
            continue
        if not entry_ok[i] or equity<=100: continue
        sl_dist=sl_m*a
        if sl_dist<=0: continue
        new_qty=round(min((equity*RISK)/sl_dist,(equity*MPOS)/price),3)
        if new_qty<0.001: continue
        cost=new_qty*price; fee=FEE*cost
        if cost+fee>cash: continue
        cash-=cost+fee; in_pos=True; entry_p=price; qty=new_qty
        sl=(price-sl_m*a if side=='LONG' else price+sl_m*a)
        tp=(price+tp_m*a if side=='LONG' else price-tp_m*a)
        entry_fee=fee; scaled=False; scale_pnl=0.0
    if in_pos:
        last_p=closes[-1]
        pnl_g=(last_p-entry_p)*qty if side=='LONG' else (entry_p-last_p)*qty
        fee_e=FEE*last_p*qty; rem=entry_fee*(1-SCALE_F) if scaled else entry_fee
        trades.append({'year':years[-1],'pnl':pnl_g-fee_e-rem+scale_pnl})
    n_tr=len(trades)
    if n_tr==0: return {'n':0,'ret':0,'pf':0,'wr':0,'dd':0,'yp':0,'yt':0}
    wins=[t for t in trades if t['pnl']>0]; losses=[t for t in trades if t['pnl']<=0]
    gw=sum(t['pnl'] for t in wins); gl=abs(sum(t['pnl'] for t in losses))
    pf=gw/gl if gl>0 else (99.0 if gw>0 else 0.0)
    eq=np.array(eq_curve)
    mdd=abs(((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min()*100) if len(eq) else 0
    ys=sorted(set(t['year'] for t in trades))
    yp=sum(1 for y in ys if sum(t['pnl'] for t in trades if t['year']==y)>0)
    return {'n':n_tr,'ret':(cash/INIT-1)*100,'pf':pf,'wr':len(wins)/n_tr*100,'dd':mdd,'yp':yp,'yt':len(ys)}


if __name__ == '__main__':
    print('Loading 15m data with 1h HTF...')
    df = load_df()
    ny = (df.index[-1] - df.index[0]).days / 365.25
    print(f'  {len(df):,} 15m bars, {ny:.1f} years\n')

    SHORT_GRID = {
        'adx_min':  [30, 35, 40, 45],
        'adx_max':  [60, 65, 70],
        'body_min': [0.5, 0.6, 0.8],
        'vol_mult': [1.0, 1.2],
        'sl_m':     [1.5, 2.0],
        'tp_m':     [3.0, 4.0],
    }
    LONG_GRID = {
        'adx_min':  [30, 35, 40],
        'adx_max':  [55, 60, 65],
        'body_min': [0.8, 1.0, 1.5],
        'vol_mult': [1.0, 1.2],
        'sl_m':     [1.5, 2.0],
        'tp_m':     [4.0, 6.0],
    }

    print('=== SHORT sweep on 15m ===')
    keys = list(SHORT_GRID.keys())
    short_results = []
    for vals in product(*SHORT_GRID.values()):
        p = dict(zip(keys, vals))
        if p['adx_min'] >= p['adx_max']: continue
        ok = (
            (df['direction']=='SHORT').values
            &(df['close'].values < df['donch_lo'].values)
            &df['bear_candle'].values
            &(df['adx'].values>=p['adx_min'])&(df['adx'].values<p['adx_max'])
            &df['adx_up'].values
            &(df['body_atr'].values>=p['body_min'])
            &(df['vol_ratio'].values>=p['vol_mult'])
        )
        r = simulate(df, ok, p['sl_m'], p['tp_m'], 'SHORT')
        r.update(p); short_results.append(r)

    quality_s = [r for r in short_results if r['n']>=10 and r['pf']>=1.3 and r['dd']<=35 and r['yp']>=4]
    quality_s.sort(key=lambda r:(r['yp'], r['pf'], r['n']), reverse=True)
    print(f'  {len(quality_s)}/{len(short_results)} passed (n≥10, PF≥1.3, DD≤35%, 4+yrs)')
    print(f'  {"ADX range":>12} {"body":>5} {"vol":>4} {"SL":>4} {"TP":>4} {"N":>5} {"/yr":>4} {"WR%":>5} {"PF":>5} {"Ret%":>5} {"DD%":>5} {"Yrs":>4}')
    for r in quality_s[:15]:
        print(f'  [{r["adx_min"]:2d}-{r["adx_max"]:2d})     ≥{r["body_min"]:.1f}  {r["vol_mult"]:.1f}  {r["sl_m"]:.1f}  {r["tp_m"]:.1f}'
              f'  {r["n"]:>5} {r["n"]/ny:>4.0f} {r["wr"]:>5.1f} {r["pf"]:>5.2f} {r["ret"]:>+5.1f} {r["dd"]:>5.1f} {r["yp"]}/{r["yt"]}')

    print('\n=== LONG sweep on 15m ===')
    keys = list(LONG_GRID.keys())
    long_results = []
    for vals in product(*LONG_GRID.values()):
        p = dict(zip(keys, vals))
        if p['adx_min'] >= p['adx_max']: continue
        ok = (
            (df['direction']=='LONG').values
            &(df['close'].values > df['donch_hi'].values)
            &df['bull_candle'].values
            &(df['adx'].values>=p['adx_min'])&(df['adx'].values<p['adx_max'])
            &df['adx_up'].values
            &(df['body_atr'].values>=p['body_min'])
            &(df['vol_ratio'].values>=p['vol_mult'])
        )
        r = simulate(df, ok, p['sl_m'], p['tp_m'], 'LONG')
        r.update(p); long_results.append(r)

    quality_l = [r for r in long_results if r['n']>=10 and r['pf']>=1.3 and r['dd']<=35 and r['yp']>=3]
    quality_l.sort(key=lambda r:(r['yp'], r['pf'], r['n']), reverse=True)
    print(f'  {len(quality_l)}/{len(long_results)} passed (n≥10, PF≥1.3, DD≤35%, 3+yrs)')
    print(f'  {"ADX range":>12} {"body":>5} {"vol":>4} {"SL":>4} {"TP":>4} {"N":>5} {"/yr":>4} {"WR%":>5} {"PF":>5} {"Ret%":>5} {"DD%":>5} {"Yrs":>4}')
    for r in quality_l[:15]:
        print(f'  [{r["adx_min"]:2d}-{r["adx_max"]:2d})     ≥{r["body_min"]:.1f}  {r["vol_mult"]:.1f}  {r["sl_m"]:.1f}  {r["tp_m"]:.1f}'
              f'  {r["n"]:>5} {r["n"]/ny:>4.0f} {r["wr"]:>5.1f} {r["pf"]:>5.2f} {r["ret"]:>+5.1f} {r["dd"]:>5.1f} {r["yp"]}/{r["yt"]}')

    # Best combined
    if quality_s and quality_l:
        bs = quality_s[0]; bl = quality_l[0]
        tot = bs['n'] + bl['n']
        print(f'\n=== BEST 15m COMBINED ===')
        print(f'  SHORT: ADX[{bs["adx_min"]}-{bs["adx_max"]}) body≥{bs["body_min"]} vol≥{bs["vol_mult"]} SL×{bs["sl_m"]} TP×{bs["tp_m"]}')
        print(f'         n={bs["n"]} ({bs["n"]/ny:.0f}/yr) WR={bs["wr"]:.1f}% PF={bs["pf"]:.2f} ret={bs["ret"]:+.1f}% DD={bs["dd"]:.1f}% {bs["yp"]}/{bs["yt"]}y')
        print(f'  LONG:  ADX[{bl["adx_min"]}-{bl["adx_max"]}) body≥{bl["body_min"]} vol≥{bl["vol_mult"]} SL×{bl["sl_m"]} TP×{bl["tp_m"]}')
        print(f'         n={bl["n"]} ({bl["n"]/ny:.0f}/yr) WR={bl["wr"]:.1f}% PF={bl["pf"]:.2f} ret={bl["ret"]:+.1f}% DD={bl["dd"]:.1f}% {bl["yp"]}/{bl["yt"]}y')
        print(f'  TOTAL: {tot} trades in {ny:.1f}y = {tot/ny:.0f}/yr')
