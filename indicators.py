"""Pure-Python indicators for the 15m bot. No pandas/ta dependencies."""
import math


def ema(data, n):
    k = 2/(n+1); e = data[0]
    for p in data[1:]: e = p*k + e*(1-k)
    return e

def ema_arr(data, n):
    k = 2/(n+1); out = [data[0]]
    for p in data[1:]: out.append(p*k + out[-1]*(1-k))
    return out

def rsi(closes, n=14):
    if len(closes) < n+1: return 50.0
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-n:])/n
    al = sum(losses[-n:])/n
    return 100 - (100/(1+ag/al)) if al > 0 else 100

def atr(highs, lows, closes, n=14):
    if len(highs) < n+1: return 0.0
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(highs))]
    return sum(trs[-n:]) / n

def adx(highs, lows, closes, n=14):
    """Wilder ADX."""
    if len(highs) < n*2: return 0.0
    pdm, ndm, trs = [], [], []
    for i in range(1, len(highs)):
        up = highs[i]-highs[i-1]; dn = lows[i-1]-lows[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    atr_s = sum(trs[:n])/n; pdi_s = sum(pdm[:n])/n; ndi_s = sum(ndm[:n])/n
    dxs = []
    for i in range(n, len(trs)):
        atr_s = (atr_s*(n-1)+trs[i])/n
        pdi_s = (pdi_s*(n-1)+pdm[i])/n
        ndi_s = (ndi_s*(n-1)+ndm[i])/n
        pdi = 100*pdi_s/atr_s if atr_s else 0
        ndi = 100*ndi_s/atr_s if atr_s else 0
        dxs.append(100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi) else 0)
    return sum(dxs[-n:])/min(len(dxs), n) if dxs else 0
