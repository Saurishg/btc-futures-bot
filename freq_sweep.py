"""
Comprehensive frequency sweep — finds max trade rate with quality floor.
Tests SHORT and LONG edge params across all levers.
"""
import pandas as pd
import numpy as np
from itertools import product
from pathlib import Path

DATA_DIR = Path(__file__).parent / 'data'
WARMUP   = 250
FEE_RATE  = 0.0004
RISK_PCT  = 0.02
MAX_POS   = 0.30
INITIAL   = 10_000.0
SCALE_AT  = 1.0
SCALE_F   = 0.5
CHAND_M   = 3.0
CHAND_ACT = 1.5
BE_AT     = 1.0

def _ema(s, n): return s.ewm(span=n, adjust=False).mean()
def _atr(h,l,c,n=14):
    pc=c.shift(1); tr=pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()
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
    f1h = DATA_DIR/'btc_1h_5y.csv'; f4h = DATA_DIR/'btc_4h_5y.csv'
    df1h = pd.read_csv(f1h, parse_dates=['timestamp']).set_index('timestamp')
    df4h = pd.read_csv(f4h, parse_dates=['timestamp']).set_index('timestamp')
    ef=_ema(df4h['close'],50); es=_ema(df4h['close'],200)
    df4h['direction']=np.where(ef>es*1.001,'LONG',np.where(ef<es*0.999,'SHORT','NONE'))
    df1h=df1h.sort_index(); df4h=df4h.sort_index()
    df1h['direction']=pd.merge_asof(
        df1h.reset_index(),df4h[['direction']].reset_index(),
        on='timestamp',direction='backward')['direction'].values
    df1h['atr']=_atr(df1h['high'],df1h['low'],df1h['close'],14)
    df1h['adx']=_adx(df1h['high'],df1h['low'],df1h['close'],14)
    df1h['adx_up']=df1h['adx']>df1h['adx'].shift(5)
    df1h['donch_hi']=df1h['high'].shift(1).rolling(20).max()
    df1h['donch_lo']=df1h['low'].shift(1).rolling(20).min()
    body=(df1h['close']-df1h['open']).abs()
    df1h['body_atr']=body/df1h['atr']
    df1h['bull_candle']=df1h['close']>df1h['open']
    df1h['bear_candle']=df1h['close']<df1h['open']
    df1h['vol_ma20']=df1h['volume'].shift(1).rolling(20).mean()
    df1h['vol_ratio']=df1h['volume']/df1h['vol_ma20']
    df1h['low_20']=df1h['low'].rolling(20).min()
    df1h['high_20']=df1h['high'].rolling(20).max()
    df1h['year']=df1h.index.year
    return df1h.dropna().copy()

def bt_short(df, adx_min, adx_max, body_min, vol_mult, sl_m=2.0, tp_m=4.0, adx_rising=True):
    ok=(
        (df['direction']=='SHORT').values
        &(df['close'].values<df['donch_lo'].values)
        &df['bear_candle'].values
        &(df['adx'].values>=adx_min)&(df['adx'].values<adx_max)
        &(df['adx_up'].values if adx_rising else np.ones(len(df),bool))
        &(df['body_atr'].values>=body_min)
        &(df['vol_ratio'].values>=vol_mult)
    )
    return _simulate(df, ok, sl_m, tp_m, 'SHORT')

def bt_long(df, adx_min, adx_max, body_min, vol_mult, sl_m=2.0, tp_m=6.0, adx_rising=True):
    ok=(
        (df['direction']=='LONG').values
        &(df['close'].values>df['donch_hi'].values)
        &df['bull_candle'].values
        &(df['adx'].values>=adx_min)&(df['adx'].values<adx_max)
        &(df['adx_up'].values if adx_rising else np.ones(len(df),bool))
        &(df['body_atr'].values>=body_min)
        &(df['vol_ratio'].values>=vol_mult)
    )
    return _simulate(df, ok, sl_m, tp_m, 'LONG')

def _simulate(df, entry_ok, sl_m, tp_m, side):
    closes=df['close'].values; highs=df['high'].values; lows=df['low'].values
    atrs=df['atr'].values
    hi20=df['high_20'].values; lo20=df['low_20'].values
    years=df['year'].values
    cash=INITIAL; peak=INITIAL
    in_pos=False; entry_p=sl=tp=qty=0.0; entry_fee=0.0
    scaled=False; scale_pnl=0.0
    trades=[]; eq_curve=[]
    n=len(df)
    for i in range(n):
        if i<WARMUP: continue
        price=closes[i]; h=highs[i]; l=lows[i]; a=atrs[i]
        if not np.isfinite(a) or a<=0: eq_curve.append(cash); continue
        if side=='LONG':
            equity=cash+qty*(price-entry_p) if in_pos else cash
        else:
            equity=cash+qty*(entry_p-price) if in_pos else cash
        peak=max(peak,equity); eq_curve.append(equity)
        if in_pos:
            if side=='LONG':
                pr_max=(h-entry_p)/a; pr_cls=(price-entry_p)/a
                if not scaled and pr_max>=SCALE_AT:
                    sp=entry_p+SCALE_AT*a; sq=qty*SCALE_F
                    pnl_s=(sp-entry_p)*sq-FEE_RATE*sp*sq-entry_fee*SCALE_F
                    cash+=sq*sp; qty-=sq; scaled=True; scale_pnl=pnl_s; sl=entry_p
                if pr_cls>=CHAND_ACT:
                    chand=hi20[i]-CHAND_M*a; sl=max(sl,chand)
                if (h-entry_p)>=BE_AT*a: sl=max(sl,entry_p)
                exit_p=reason=None
                if l<=sl: exit_p,reason=sl,'SL'
                elif h>=tp: exit_p,reason=tp,'TP'
                if exit_p:
                    rem=entry_fee*(1-SCALE_F) if scaled else entry_fee
                    pnl_g=(exit_p-entry_p)*qty; fee_e=FEE_RATE*exit_p*qty
                    pnl_n=pnl_g-fee_e-rem+scale_pnl; cash+=qty*exit_p
                    trades.append({'year':years[i],'pnl':pnl_n,'reason':reason})
                    in_pos=False; qty=0.0; entry_fee=0.0; scaled=False; scale_pnl=0.0
            else:  # SHORT
                pr_max=(entry_p-l)/a; pr_cls=(entry_p-price)/a
                if not scaled and pr_max>=SCALE_AT:
                    sp=entry_p-SCALE_AT*a; sq=qty*SCALE_F
                    pnl_s=(entry_p-sp)*sq-FEE_RATE*sp*sq-entry_fee*SCALE_F
                    cash+=sq*entry_p+pnl_s; qty-=sq; scaled=True; scale_pnl=pnl_s; sl=entry_p
                if pr_cls>=CHAND_ACT:
                    chand=lo20[i]+CHAND_M*a
                    sl=min(sl,chand) if sl>0 else chand
                if (entry_p-l)>=BE_AT*a:
                    sl=min(sl,entry_p) if sl>0 else entry_p
                exit_p=reason=None
                if h>=sl: exit_p,reason=sl,'SL'
                elif l<=tp: exit_p,reason=tp,'TP'
                if exit_p:
                    rem=entry_fee*(1-SCALE_F) if scaled else entry_fee
                    pnl_g=(entry_p-exit_p)*qty; fee_e=FEE_RATE*exit_p*qty
                    pnl_n=pnl_g-fee_e-rem+scale_pnl
                    cash+=qty*entry_p+(pnl_g-fee_e-rem)
                    trades.append({'year':years[i],'pnl':pnl_n,'reason':reason})
                    in_pos=False; qty=0.0; entry_fee=0.0; scaled=False; scale_pnl=0.0
            continue
        if not entry_ok[i] or equity<=100: continue
        sl_dist=sl_m*a
        if sl_dist<=0: continue
        new_qty=round(min((equity*RISK_PCT)/sl_dist,(equity*MAX_POS)/price),3)
        if new_qty<0.001: continue
        cost=new_qty*price; fee=FEE_RATE*cost
        if cost+fee>cash: continue
        cash-=cost+fee; in_pos=True; entry_p=price; qty=new_qty
        if side=='LONG': sl=price-sl_m*a; tp=price+tp_m*a
        else:            sl=price+sl_m*a; tp=price-tp_m*a
        entry_fee=fee; scaled=False; scale_pnl=0.0
    if in_pos:
        last_p=closes[-1]
        if side=='LONG': pnl_g=(last_p-entry_p)*qty
        else:            pnl_g=(entry_p-last_p)*qty
        fee_e=FEE_RATE*last_p*qty; rem=entry_fee*(1-SCALE_F) if scaled else entry_fee
        pnl_n=pnl_g-fee_e-rem+scale_pnl
        cash+=qty*last_p if side=='LONG' else qty*entry_p+(pnl_g-fee_e-rem)
        trades.append({'year':years[-1],'pnl':pnl_n,'reason':'EOD'})
    n_tr=len(trades)
    if n_tr==0: return {'n':0,'ret':0,'pf':0,'wr':0,'dd':0,'yp':0,'yt':0}
    wins=[t for t in trades if t['pnl']>0]; losses=[t for t in trades if t['pnl']<=0]
    gw=sum(t['pnl'] for t in wins); gl=abs(sum(t['pnl'] for t in losses))
    pf=gw/gl if gl>0 else (99.0 if gw>0 else 0.0)
    eq=np.array(eq_curve); mdd=abs(((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min()*100) if len(eq) else 0
    ys=sorted(set(t['year'] for t in trades))
    yp=sum(1 for y in ys if sum(t['pnl'] for t in trades if t['year']==y)>0)
    return {'n':n_tr,'ret':(cash/INITIAL-1)*100,'pf':pf,'wr':len(wins)/n_tr*100,'dd':mdd,'yp':yp,'yt':len(ys)}


if __name__=='__main__':
    print('Loading 5y data...')
    df=load_df()
    ny=(df.index[-1]-df.index[0]).days/365.25
    print(f'  {len(df)} bars, {ny:.1f} years')

    print('\n=== SHORT edge: ADX range sweep ===')
    print(f'  {"ADX range":>12} {"N":>4} {"/yr":>4} {"WR%":>5} {"PF":>5} {"Ret%":>5} {"DD%":>5} {"Yrs":>4}')
    for am,ax in [(50,65),(50,70),(48,68),(45,70),(43,70),(43,75),(40,70),(40,75)]:
        r=bt_short(df,am,ax,0.8,1.2)
        if r['n']==0: continue
        mark=' <-- current' if am==50 and ax==65 else ''
        print(f'  [{am:2d}-{ax:2d})       {r["n"]:>4} {r["n"]/ny:>4.1f} {r["wr"]:>5.1f} '
              f'{r["pf"]:>5.2f} {r["ret"]:>+5.1f} {r["dd"]:>5.1f} {r["yp"]}/{r["yt"]}{mark}')

    print('\n=== SHORT: body filter impact (ADX[45-70)) ===')
    for body in [0.8, 0.6, 0.5, 0.3]:
        r=bt_short(df,45,70,body,1.2)
        print(f'  body≥{body}: n={r["n"]} WR={r["wr"]:.1f}% PF={r["pf"]:.2f} DD={r["dd"]:.1f}%')

    print('\n=== SHORT: adx_rising filter (ADX[45-70)) ===')
    r_with=bt_short(df,45,70,0.8,1.2,adx_rising=True)
    r_without=bt_short(df,45,70,0.8,1.2,adx_rising=False)
    print(f'  With adx_rising:    n={r_with["n"]} WR={r_with["wr"]:.1f}% PF={r_with["pf"]:.2f} DD={r_with["dd"]:.1f}%')
    print(f'  Without adx_rising: n={r_without["n"]} WR={r_without["wr"]:.1f}% PF={r_without["pf"]:.2f} DD={r_without["dd"]:.1f}%')

    print('\n=== LONG edge: ADX/body sweep ===')
    print(f'  {"ADX/body":>18} {"N":>4} {"/yr":>4} {"WR%":>5} {"PF":>5} {"Ret%":>5} {"DD%":>5} {"Yrs":>4}')
    for am,ax,body in [(40,50,1.5),(40,55,1.5),(35,50,1.5),(35,55,1.5),
                       (35,55,1.2),(35,55,1.0),(30,55,1.0),(30,60,0.8)]:
        r=bt_long(df,am,ax,body,1.2)
        if r['n']==0: continue
        mark=' <-- current' if am==40 and ax==50 and body==1.5 else ''
        print(f'  [{am:2d}-{ax:2d}) body≥{body:.1f}  {r["n"]:>4} {r["n"]/ny:>4.1f} {r["wr"]:>5.1f} '
              f'{r["pf"]:>5.2f} {r["ret"]:>+5.1f} {r["dd"]:>5.1f} {r["yp"]}/{r["yt"]}{mark}')

    print('\n=== LONG: adx_rising filter (ADX[35-55) body≥1.0) ===')
    r_with=bt_long(df,35,55,1.0,1.2,adx_rising=True)
    r_without=bt_long(df,35,55,1.0,1.2,adx_rising=False)
    print(f'  With adx_rising:    n={r_with["n"]} WR={r_with["wr"]:.1f}% PF={r_with["pf"]:.2f} DD={r_with["dd"]:.1f}%')
    print(f'  Without adx_rising: n={r_without["n"]} WR={r_without["wr"]:.1f}% PF={r_without["pf"]:.2f} DD={r_without["dd"]:.1f}%')

    print('\n=== Combined frequency: best configs ===')
    scenarios = [
        ('CURRENT  S[50-65)+L[40-50)b1.5',
         dict(am=50,ax=65,bm=0.8,vm=1.2), dict(lam=40,lax=50,lbm=1.5,lvm=1.2)),
        ('OPT-A    S[45-70)+L[40-50)b1.5',
         dict(am=45,ax=70,bm=0.8,vm=1.2), dict(lam=40,lax=50,lbm=1.5,lvm=1.2)),
        ('OPT-B    S[45-70)+L[35-55)b1.5',
         dict(am=45,ax=70,bm=0.8,vm=1.2), dict(lam=35,lax=55,lbm=1.5,lvm=1.2)),
        ('OPT-C    S[45-70)+L[35-55)b1.0',
         dict(am=45,ax=70,bm=0.8,vm=1.2), dict(lam=35,lax=55,lbm=1.0,lvm=1.2)),
        ('OPT-D    S[43-70)+L[35-55)b1.0',
         dict(am=43,ax=70,bm=0.8,vm=1.2), dict(lam=35,lax=55,lbm=1.0,lvm=1.2)),
        ('OPT-E    S[43-75)+L[35-55)b1.0',
         dict(am=43,ax=75,bm=0.8,vm=1.2), dict(lam=35,lax=55,lbm=1.0,lvm=1.2)),
    ]
    print(f'  {"Scenario":40} {"S":>4} {"L":>4} {"TOT":>4} {"/yr":>4}')
    for name,sp,lp in scenarios:
        rs=bt_short(df,sp['am'],sp['ax'],sp['bm'],sp['vm'])
        rl=bt_long(df,lp['lam'],lp['lax'],lp['lbm'],lp['lvm'])
        tot=rs['n']+rl['n']
        print(f'  {name:40} {rs["n"]:>4} {rl["n"]:>4} {tot:>4} {tot/ny:>4.1f}')
        if rs['n']>0: print(f'    SHORT: WR={rs["wr"]:.1f}% PF={rs["pf"]:.2f} DD={rs["dd"]:.1f}% {rs["yp"]}/{rs["yt"]}y')
        if rl['n']>0: print(f'    LONG:  WR={rl["wr"]:.1f}% PF={rl["pf"]:.2f} DD={rl["dd"]:.1f}% {rl["yp"]}/{rl["yt"]}y')
