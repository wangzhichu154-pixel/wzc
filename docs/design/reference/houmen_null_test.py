#!/usr/bin/env python3
# Null-hypothesis Monte Carlo for the "Houmen" model (design doc, Appendix A.3).
# Question: how often does the 1%-tolerance c^-n "resonance pair" filter fire on pivots
# extracted from a pure random walk (prices with no structure at all)?
# Usage: python3 houmen_null_test.py
import random, math, statistics
def zigzag(prices, th=0.15):
    piv=[]; last=prices[0]; trend=0; ext=prices[0]
    for p in prices[1:]:
        if trend>=0:
            if p>ext: ext=p
            elif p<=ext*(1-th):
                piv.append(ext); trend=-1; ext=p
        if trend<0:
            if p<ext: ext=p
            elif p>=ext*(1+th):
                piv.append(ext); trend=1; ext=p
    return piv
def count_pairs(anchors, c, tol=0.01, nmax=4):
    a=sorted(set(round(x,2) for x in anchors)); hits=0
    for i in range(len(a)):
        for j in range(i+1,len(a)):
            lo,hi=a[i],a[j]
            for n in range(1,nmax+1):
                fwd=lo/c**n
                if abs(fwd-hi)/hi<=tol: hits+=1; break
    return hits, len(a)
random.seed(42)
res={c:[] for c in (0.60,0.618,0.65,0.70)}; nanch=[]
trials=2000
for t in range(trials):
    p=10.0; prices=[]
    for d in range(500):              # ~2 trading years
        p*=math.exp(random.gauss(0,0.03))  # 3% daily vol, zero drift
        prices.append(p)
    piv=zigzag(prices,0.15)[-15:]     # keep up to 15 most recent pivots
    if len(piv)<6: continue
    for c in res:
        h,n=count_pairs(piv,c); res[c].append(h)
    nanch.append(len(set(round(x,2) for x in piv)))
print("trials used:",len(nanch)," mean #anchors:",round(statistics.mean(nanch),1))
for c,v in res.items():
    print(f"c={c}: mean strong pairs={statistics.mean(v):.2f}  P(>=1 pair)={sum(1 for x in v if x>=1)/len(v):.2%}")
