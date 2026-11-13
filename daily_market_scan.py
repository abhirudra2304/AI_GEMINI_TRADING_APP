import pandas as pd,numpy as np,glob,os,warnings,sys
warnings.filterwarnings("ignore")
sys.path.insert(0,".")
H="historical_data/"
from datetime import date as _date
try:
    import config;U=set(config.Universe.TARGET_UNIVERSE)
except Exception as e:U=set()
def ld(n):
    d=pd.read_parquet(f"{H}{n}_ONE_DAY_400.parquet").sort_values("Timestamp").reset_index(drop=True);d["dt"]=d.Timestamp.dt.strftime("%Y-%m-%d");return d
def rsi(c,n=14):
    dl=c.diff();g=dl.clip(lower=0).ewm(alpha=1/n,adjust=False).mean();l=(-dl.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean();return 100-100/(1+g/l)
D=ld("Nifty 50").dt.iloc[-1];DK=D.replace("-","");TODAY=_date.today().isoformat()
out=[]
def P(s=""):out.append(s)
P(f"# Daily Market Scan - settled close {D} (generated {TODAY}; run after the 20:15 nightly so closes are official)\n")
print("UNIVERSE size",len(U),file=sys.stderr)
idx={"Nifty 50":"Nifty 50","Nifty Bank":"NIFTY BANK","Midcap 100":"NIFTY MIDCAP 100","Smallcap 100":"NIFTY SMALLCAP 100","India VIX":"India VIX"}
irows=[]
for k,n in idx.items():
    d=ld(n);c=d.Close
    if d.dt.iloc[-1]!=D:irows.append(f"| {k} | STALE ({d.dt.iloc[-1]}) |||||||");continue
    e=lambda s:c.ewm(span=s,adjust=False).mean().iloc[-1]
    irows.append(f"| {k} | {c.iloc[-1]:,.1f} | {(c.iloc[-1]/c.iloc[-2]-1)*100:+.2f}% | {(c.iloc[-1]/c.iloc[-6]-1)*100:+.1f}% | {e(20):,.0f} | {e(50):,.0f} | {e(200):,.0f} | {rsi(c).iloc[-1]:.0f} |")
syms=[os.path.basename(f).split("_ONE_DAY")[0] for f in glob.glob(H+"*_ONE_DAY_400.parquet")]
syms=[s for s in syms if not s.upper().startswith(("NIFTY","INDIA VIX"))]
data={}
for s in syms:
    try:
        d=ld(s)
        if len(d)>=210 and d.dt.iloc[-1]==D and d.Close.iloc[-1]>0:data[s]=d
    except:pass
nf=ld("Nifty 50")
nr20=nf.Close.iloc[-1]/nf.Close.iloc[-21]-1
rows=[]
for s,d in data.items():
    c,h,l,v=d.Close,d.High,d.Low,d.Volume
    a20,a50,a200=c.rolling(20).mean(),c.rolling(50).mean(),c.rolling(200).mean()
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    r=rsi(c)
    dn=c.diff().tail(3)<0
    rows.append(dict(s=s,px=c.iloc[-1],chg=(c.iloc[-1]/c.iloc[-2]-1)*100,a20=a20.iloc[-1],a50=a50.iloc[-1],a200=a200.iloc[-1],
      r14=r.iloc[-1],rs=(c.iloc[-1]/c.iloc[-21]-1)-nr20,hi60=h.tail(60).max(),
      pb=(c.iloc[-1]/h.tail(60).max()-1)*100,hi20p=h.iloc[-21:-1].max(),lo10=l.tail(10).min(),
      vx=v.iloc[-1]/v.iloc[-21:-1].mean(),atr10=tr.tail(10).mean(),atr50=tr.tail(50).mean(),atr14=tr.tail(14).mean(),
      dnvx=(v.tail(3)[dn].mean()/v.iloc[-21:-1].mean()) if dn.any() else np.nan,
      rlow=r.tail(5).min(),plow=l.tail(5).min(),rlow_p=r.iloc[-20:-5].min(),plow_p=l.iloc[-20:-5].min(),
      lo20=l.tail(20).min()))
R=pd.DataFrame(rows).set_index("s")
adv=(R.chg>0).sum();dec=(R.chg<0).sum()
b50=(R.px>R.a50).mean()*100;b200=(R.px>R.a200).mean()*100;b20=(R.px>R.a20).mean()*100
n=ld("Nifty 50").Close;e20,e50,e200=[n.ewm(span=s,adjust=False).mean().iloc[-1] for s in(20,50,200)]
reg=("Bullish Expansion" if n.iloc[-1]>e20>e50>e200 and b50>60 else "Pullback within uptrend" if n.iloc[-1]>e50 and n.iloc[-1]<e20 else "Distribution" if n.iloc[-1]>e50 and b50<50 else "Bearish" if n.iloc[-1]<e200 else "Mixed/Recovery")
P("## 1. Market regime\n");P("| Index | Close | 1D | 5D | EMA20 | EMA50 | EMA200 | RSI14 |\n|---|---|---|---|---|---|---|---|");out.extend(irows)
P(f"\nBreadth (cached universe, {len(R)} names): advancers {adv} / decliners {dec} (A/D {adv/max(dec,1):.2f}); >20DMA {b20:.0f}%; >50DMA {b50:.0f}%; >200DMA {b200:.0f}%.")
P(f"\n**Regime: {reg}** (rule: Nifty vs EMA20/50/200 plus %>50DMA).\n")
sec={"Auto":"NIFTY AUTO","Bank":"NIFTY BANK","PSU Bank":"NIFTY PSU BANK","IT":"NIFTY IT","Pharma":"NIFTY PHARMA","FMCG":"NIFTY FMCG","Metal":"NIFTY METAL","Energy":"NIFTY ENERGY","Realty":"NIFTY REALTY","Commodities":"NIFTY COMMODITIES"}
sr=[]
for k,nm in sec.items():
    try:
        d=ld(nm)
        if d.dt.iloc[-1]!=D:continue
        c=d.Close;sr.append((k,(c.iloc[-1]/c.iloc[-2]-1)*100,((c.iloc[-1]/c.iloc[-21]-1)-nr20)*100))
    except:pass
sr.sort(key=lambda x:-x[2])
P("Sector indices, 20D return relative to Nifty (leading to lagging):\n\n| Sector | 1D | 20D RS vs Nifty (pp) |\n|---|---|---|")
for k,a,b in sr:P(f"| {k} | {a:+.2f}% | {b:+.1f} |")
files=sorted(glob.glob("delivery_cache/2026*.parquet"))
fl=[f for f in files if os.path.basename(f)[:8]<=DK][-21:]
dl=pd.concat([pd.read_parquet(f).assign(d=os.path.basename(f)[:8]) for f in fl]);dl=dl[dl.SERIES=="EQ"]
today=dl[dl.d==DK].set_index("SYMBOL");hist=dl[dl.d!=DK].groupby("SYMBOL").agg(dq=("DELIV_QTY","mean"),dp=("DELIV_PER","mean"),tq=("TTL_TRD_QNTY","mean"),n=("d","count"))
T=today.join(hist,how="inner");T=T[T.n>=15]
T["dqx"]=T.DELIV_QTY/T.dq;T["dpx"]=T.DELIV_PER/T.dp;T["chg"]=(T.CLOSE_PRICE/T.PREV_CLOSE-1)*100;T["tqx"]=T.TTL_TRD_QNTY/T.tq;T["uni"]=T.index.isin(U)
liq=T[(T.TURNOVER_LACS>=2000)]
P(f"\nDelivery data: {len(fl)} bhavcopy files, latest {DK}, {len(T)} EQ symbols with >=15 days history.\n")
def dpstr(s):
    if s in T.index:
        t=T.loc[s];return f"{t.DELIV_PER:.0f}% ({t.dpx:.1f}x)"
    return "n/a"
P("\n## 2. Average-down candidates and avoid list\n")
HOLD=["DATAPATTNS","DIXON","SOLARINDS","TCS","BEL","BDL","HAL","KAYNES","CONCOR","GODFRYPHLP","NETWEB","MCX"]
cost={}
try:
    _he=pd.read_csv("holdings_entries.csv")
    cost={r.symbol:float(r.avg_buy_price) for r in _he.itertuples() if pd.notna(r.avg_buy_price)}
except Exception as _e:
    print("holdings_entries.csv unreadable:",_e,file=sys.stderr)
P("Entry prices come from holdings_entries.csv; names with a blank avg_buy_price skip the below-cost test (structure/pullback tests only). No entry on file: "+(", ".join(x for x in ["DATAPATTNS","DIXON","SOLARINDS","TCS","BEL","BDL","HAL","KAYNES","CONCOR","GODFRYPHLP"] if x not in cost) or "none")+".\n")
cand=[];avoid=[]
for s,r in R.iterrows():
    near50=abs(r.px/r.a50-1)<0.03;near200=abs(r.px/r.a200-1)<0.03
    swsup=abs(r.px/r.lo20-1)<0.03
    cond_pb=-15<=r.pb<=-5
    contract=(not np.isnan(r.dnvx)) and r.dnvx<1.0
    div=bool((r.plow<r.plow_p) and (r.rlow>r.rlow_p))
    if r.px<r.a200 and r.vx>1.5 and r.chg<-1:avoid.append((s,r))
    if cond_pb and (near50 or near200 or swsup) and (contract or div) and r.px>r.a200*0.97 and (s in HOLD or s in U):
        cand.append((s,r,near50,near200,contract,div))
def lvl(r):
    ps=[]
    if abs(r.px/r.a50-1)<0.03:ps.append(f"50DMA {r.a50:,.0f}")
    if abs(r.px/r.a200-1)<0.03:ps.append(f"200DMA {r.a200:,.0f}")
    ps.append(f"20d low {r.lo20:,.0f}");return "; ".join(ps)
P("**Candidates** (universe/holdings; pullback -5..-15% from 60d high; at 50/200DMA or 20d low; down-day volume contracting or bullish RSI divergence; not below 200DMA):\n")
if cand:
    P("| Ticker | CMP | Vol vs 20D | Delivery % (vs avg) | Key technical level | Verdict |\n|---|---|---|---|---|---|")
    for s,r,n5,n2,ct,dv in sorted(cand,key=lambda x:x[1].pb):
        why=("contracting down-vol" if ct else "")+(" + " if ct and dv else "")+("RSI bull-div" if dv else "")
        h=" (HOLDING)" if s in HOLD else ""
        P(f"| {s}{h} | {r.px:,.1f} | {r.vx:.1f}x | {dpstr(s)} | {lvl(r)}; pullback {r.pb:.0f}% | Add-zone watch: {why} |")
else:P("None pass all filters.")
P("\n**Holdings status:**\n\n| Ticker | CMP | vs cost | Pullback from 60d high | vs 50DMA | vs 200DMA | Down-day vol | Verdict |\n|---|---|---|---|---|---|---|---|")
for s in HOLD:
    if s not in R.index:continue
    r=R.loc[s];cs=f"{(r.px/cost[s]-1)*100:+.1f}%" if s in cost else "n/a"
    v="AVOID: below 200DMA" if r.px<r.a200 else ("Hold/watch" if r.pb>-5 else "In pullback zone" if r.pb>=-15 else "Deep drawdown - no add without base")
    if r.px<r.a200 and r.vx>1.5 and r.chg<-1:v="AVOID: breakdown on volume"
    dv="n/a" if np.isnan(r.dnvx) else f"{r.dnvx:.1f}x"
    P(f"| {s} | {r.px:,.1f} | {cs} | {r.pb:.0f}% | {(r.px/r.a50-1)*100:+.1f}% | {(r.px/r.a200-1)*100:+.1f}% | {dv} | {v} |")
P("\n**AVOID averaging down** (universe names with close < 200DMA, volume >1.5x, down >1% today):\n")
av=[(s,r) for s,r in avoid if s in U]
P(", ".join(f"{s} ({r.chg:+.1f}%, {r.vx:.1f}x, 200DMA {r.a200:,.0f})" for s,r in av) if av else "None today.")
P("\n## 3. Fresh breakout / VCP candidates\n")
P("Filters: close above prior-20d high, volume >=1.5x, 20d RS vs Nifty >0, close above 50DMA and 200DMA. VCP tag if ATR10/ATR50 <0.85.")
P("Signal: **BUY_CANDIDATE** only if RSI14 <=70 AND delivery >=1.0x its 20d avg (or delivery unknown). Otherwise **WATCH_FOR_PULLBACK** (EXTENDED and/or weak delivery): wait for a pullback to the 10-EMA / prior 20d high.")
P("Stop (all): tightest valid of {5-session swing low, 10-EMA, close-1.5*ATR14}, i.e. the highest of those below price (no wide -15% default; -15% is only a hard cap). WATCH rows: entry = pullback zone (10-EMA to prior-20d-high retest), stop below the swing low / 10-EMA, risk measured from the zone midpoint. Targets 2R / 3R.\n")
bo=R[(R.px>R.hi20p)&(R.vx>=1.5)&(R.rs>0)&(R.px>R.a50)&(R.px>R.a200)&(R.index.isin(U))]
P("| Ticker | Signal | CMP | Vol vs 20D | Delivery % (vs avg) | Entry zone | Stop | Target 2R / 3R | Note |\n|---|---|---|---|---|---|---|---|---|")
nb=nw=0
for s,r in bo.sort_values("vx",ascending=False).head(15).iterrows():
    dx=T.loc[s].dpx if s in T.index else np.nan
    ext=r.r14>70;weak=(not np.isnan(dx)) and dx<1.0
    watch=ext or weak
    e10=data[s].Close.ewm(span=10,adjust=False).mean().iloc[-1];sw5=data[s].Low.tail(5).min()
    if watch:
        z_lo=min(e10,r.hi20p);z_hi=max(e10,r.hi20p)
        if z_hi>=r.px:z_hi=r.px*0.98;z_lo=min(z_lo,z_hi)
        ent=(z_lo+z_hi)/2
        _c=[v for v in (sw5,ent-1.5*r.atr14,z_lo-0.5*r.atr14) if v<ent*0.995];stop=max(_c) if _c else ent*0.9
        stop=max(stop,ent*0.85)
        zone=f"{z_lo:,.0f}-{z_hi:,.0f} (pullback)";nw+=1
    else:
        ent=r.px;zone=f"{r.hi20p:,.0f}-{r.px*1.01:,.0f}"
        cands=[v for v in (sw5,e10,r.px-1.5*r.atr14) if v<r.px*0.995]
        stop=max(max(cands) if cands else r.px*0.85,r.px*0.85);nb+=1
    risk=ent-stop
    why=", ".join(x for x,f in (("RSI "+str(int(r.r14))+">70",ext),("delivery "+(f"{dx:.1f}x" if not np.isnan(dx) else "n/a")+"<1.0x",weak)) if f)
    vcp="VCP; " if r.atr10/r.atr50<0.85 else ""
    P(f"| {s} | {'WATCH_FOR_PULLBACK' if watch else 'BUY_CANDIDATE'} | {r.px:,.1f} | {r.vx:.1f}x | {dpstr(s)} | {zone} | {stop:,.0f} ({-risk/ent*100:.1f}%) | {ent+2*risk:,.0f} / {ent+3*risk:,.0f} | {vcp}RS {r.rs*100:+.0f}pp, chg {r.chg:+.1f}%{('; '+why) if why else ''} |")
if len(bo)==0:P("| none | | | | | | | | |")
P(f"\n{len(bo)} universe names pass the breakout filter: {nb} BUY_CANDIDATE, {nw} WATCH_FOR_PULLBACK (of the top 15 shown). Validation note: our walk-forward tests found no bias-free daily-entry edge after costs; treat as a watchlist, not a signal.")
P("\n## 4. FII/DII positioning\n")
fh=pd.read_csv("fii_dii_history.csv");print(fh.columns.tolist(),file=sys.stderr)
fh.columns=[c.lower() for c in fh.columns]
nc=[c for c in fh.columns if "net" in c][0];cc="category"
pv=fh.pivot_table(index="date",columns=cc,values=nc).tail(8)
P("Cash market net (Rs cr):\n\n| Date | FII/FPI | DII |\n|---|---|---|")
for dte,r in pv.iterrows():P(f"| {dte} | {r.get('FII/FPI',np.nan):,.0f} | {r.get('DII',np.nan):,.0f} |")
strk=int((pv["FII/FPI"]>0)[::-1].cumprod().sum())
P(f"\nFII net buy streak: {strk} sessions; last-8-session FII total Rs {pv['FII/FPI'].sum():,.0f} cr vs DII Rs {pv['DII'].sum():,.0f} cr (FII buying absorbed by DII selling).")
sm=pd.read_csv("journal/smart_money.csv");sm=sm[sm.date==D]
P(f"\nSmart-money journal {D}: {len(sm)} entries ({sm.type.value_counts().to_dict()}).")
try:
    import fao_participant_oi as _fo
    _h=_fo.load();_sm=_fo.summary()
    if _sm is None or _sm["date"]!=D:
        P(f"\n**Derivatives (FII participant OI): STALE/MISSING.** Latest in fao_participant_oi_history.csv is {_sm['date'] if _sm else 'none'}, need {D}. Run `python fao_participant_oi.py` (NSE publishes after the close); the nightly step may have failed. Nothing estimated.")
    else:
        _f=_h[_h.client=="FII"].sort_values("date").tail(6)
        P("\n**FII derivatives (NSE participant-wise OI, contracts):**\n\n| Date | Idx Fut Long | Idx Fut Short | L/S ratio | Idx Fut net | Stk Fut net | Idx Opt bias* |\n|---|---|---|---|---|---|---|")
        for _,r in _f.iterrows():
            P(f"| {r['date']} | {r.fut_idx_long:,.0f} | {r.fut_idx_short:,.0f} | {r.fut_idx_long/r.fut_idx_short:.2f} | {r.fut_idx_long-r.fut_idx_short:+,.0f} | {r.fut_stk_long-r.fut_stk_short:+,.0f} | {(r.opt_idx_call_long+r.opt_idx_put_short)-(r.opt_idx_call_short+r.opt_idx_put_long):+,.0f} |")
        P("\n*Idx Opt bias = long calls + short puts - short calls - long puts (net bullish index-option exposure).")
        _c=_sm["idx_fut_net_chg"]
        P(f"\nFII index futures: {_sm['idx_fut_long_pct']}% long (L/S {_sm['idx_fut_ls_ratio']}), net {_sm['idx_fut_net']:+,} contracts, day change {_c:+,} ({'covering shorts' if _c and _c>0 else 'adding shorts' if _c and _c<0 else 'flat'}). FIIs are net short index futures while buying cash: a hedged-long signature, not by itself bearish; a rising short count into a rally with cash buying fading is the trap warning.")
except Exception as _e:
    P(f"\n**Derivatives (FII participant OI): ERROR {type(_e).__name__}: {_e}.** Fix fao_participant_oi.py. Nothing estimated.")
P("\n**Possible retail-bait / weak-hand rallies** (price +3%+, volume >1.5x avg, delivery % below 0.9x its 20d avg, turnover >= Rs 20 cr):\n")
tr=T[(T.chg>3)&(T.tqx>1.5)&(T.dpx<0.9)&(T.TURNOVER_LACS>=2000)].sort_values("chg",ascending=False).head(10)
P("| Ticker | CMP | Vol vs 20D | Delivery % (vs avg) | Chg | Verdict |\n|---|---|---|---|---|---|")
for s,t in tr.iterrows():P(f"| {s}{'' if t.uni else ' (non-uni)'} | {t.CLOSE_PRICE:,.1f} | {t.tqx:.1f}x | {t.DELIV_PER:.0f}% ({t.dpx:.1f}x) | {t.chg:+.1f}% | Low-delivery spike: caution, unconfirmed |")
if len(tr)==0:P("| none | | | | | |")
P("\n## 5. Delivery footprint spikes (delivery qty >=1.5x 20d avg and delivery % >=1.1x; turnover >= Rs 20 cr)\n")
sp=liq[(liq.dqx>=1.5)&(liq.dpx>=1.1)]
H5="| Ticker | CMP | Vol vs 20D | Delivery % (vs avg) | Key level | Chg / verdict |\n|---|---|---|---|---|---|"
def sec5(df,title,verd):
    P(f"**{title}** ({len(df)} names; top 12 by delivery-qty multiple)\n");P(H5)
    for s,t in df.sort_values("dqx",ascending=False).head(12).iterrows():
        lv=f"20d hi {R.loc[s].hi20p:,.0f}; 50DMA {R.loc[s].a50:,.0f}" if s in R.index else "no tech cache"
        P(f"| {s}{'' if t.uni else ' (non-uni)'} | {t.CLOSE_PRICE:,.1f} | {t.tqx:.1f}x | {t.DELIV_PER:.0f}% ({t.dpx:.1f}x; qty {t.dqx:.1f}x) | {lv} | {t.chg:+.1f}% - {verd} |")
    P()
sec5(sp[(sp.chg>=0)&(sp.chg<=2)],"a) Silent accumulation (chg 0 to +2%)","watch for base/breakout")
sec5(sp[sp.chg>3],"b) Momentum ignition (chg >3%)","already extended, chase risk")
sec5(sp[sp.chg<-2],"c) Heavy delivery on a down day (distribution risk)","not accumulation")
P("\n## Missing data check\n")
P(f"- OK: daily bars 11-12 for {len(R)} symbols; NSE delivery bhavcopy {DK}; FII/DII cash {D}; sector indices that show above (any stale index is flagged STALE in the table).")
P("- Derivatives: see section 4; flagged STALE/ERROR there if fao_participant_oi_history.csv lacks the latest date.")
P("- Entry prices: from holdings_entries.csv; blanks listed in section 2 cannot be tested against cost.")
open("reports/daily_market_scan_"+TODAY.replace("-","_")+".md","w",encoding="utf-8").write("\n".join(out))
print("\n".join(out))
