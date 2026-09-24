"""FastAPI local API + lightweight auto-refreshing HTML dashboard."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.connection import get_session
from app.database.models import (
    Chain, LargeTransaction, ScannerState, Token, TokenPair, TokenTransfer,
)
from app.tokens.classifier import CATEGORIES as CLASSIFIER_CATEGORIES
from config import settings

log = logging.getLogger("api")

app = FastAPI(title="Crypto Intelligence Scanner", version="1.0")

# populated by main.py at startup so endpoints can read live scanner state
SCANNER: dict = {"collectors": {}, "discovery": None, "started_at": None,
                 "mock_mode": settings.mock_mode}

CATEGORIES = ["Watched"] + CLASSIFIER_CATEGORIES


def _chain_map(s: Session) -> dict[int, Chain]:
    return {c.id: c for c in s.query(Chain).all()}


def _ser_large(r: LargeTransaction, chains: dict) -> dict:
    def num(v):
        return float(v) if v is not None else None
    return {
        "id": r.id,
        "chain": chains[r.chain_pk].name if r.chain_pk in chains else str(r.chain_pk),
        "chain_key": chains[r.chain_pk].key if r.chain_pk in chains else None,
        "tx_hash": r.tx_hash, "token_address": r.token_address,
        "token_symbol": r.token_symbol or r.token_address[:10],
        "from": r.from_address, "to": r.to_address,
        "amount": num(r.amount), "usd_value": num(r.usd_value),
        "relative_size": num(r.relative_size), "volume_ratio": num(r.volume_ratio),
        "liquidity_ratio": num(r.liquidity_ratio), "percentile": num(r.percentile),
        "anomaly_score": num(r.anomaly_score), "flow": r.flow,
        "transaction_type": r.transaction_type,
        "timestamp": r.timestamp.isoformat() if r.timestamp else None,
    }


def query_large(s: Session, chain: Optional[str], flow: Optional[str],
                category: Optional[str], min_usd: float, min_score: float,
                sort: str, limit: int) -> list[dict]:
    q = s.query(LargeTransaction)
    if chain and chain != "all":
        q = q.join(Chain, Chain.id == LargeTransaction.chain_pk).filter(Chain.key == chain)
    if flow and flow != "all":
        q = q.filter(LargeTransaction.flow == flow.upper())
    if category and category != "all":
        sub = s.query(Token.address).join(Chain, Chain.id == Token.chain_pk)
        if chain and chain != "all":
            sub = sub.filter(Chain.key == chain)
        sub = sub.filter(Token.category == category)
        q = q.filter(LargeTransaction.token_address.in_(sub))
    if min_usd > 0:
        q = q.filter(LargeTransaction.usd_value >= min_usd)
    if min_score > 0:
        q = q.filter(LargeTransaction.anomaly_score >= min_score)
    order = {
        "score": LargeTransaction.anomaly_score.desc(),
        "usd": LargeTransaction.usd_value.desc(),
        "relative": LargeTransaction.relative_size.desc(),
        "newest": LargeTransaction.timestamp.desc(),
        "liquidity": LargeTransaction.liquidity_ratio.desc(),
    }.get(sort, LargeTransaction.anomaly_score.desc())
    rows = q.order_by(order.nullslast() if hasattr(order, "nullslast") else order).limit(limit).all()
    chains = _chain_map(s)
    return [_ser_large(r, chains) for r in rows]


@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.get("/api/status")
def status(s: Session = Depends(get_session)):
    chains_out = {}
    for key, col in SCANNER["collectors"].items():
        st = s.query(ScannerState).filter_by(chain_pk=col.chain_pk).first() if col.chain_pk else None
        chains_out[key] = {
            "connected": bool(col.running and not col.last_error),
            "running": col.running,
            "head": col.head,
            "last_processed_block": st.last_processed_block if st else 0,
            "transfers_processed": col.transfers.processed_count,
            "last_error": col.last_error,
        }
    with_s = s.query(func.count(LargeTransaction.id)).scalar() or 0
    tokens_n = s.query(func.count(Token.id)).scalar() or 0
    pairs_n = s.query(func.count(TokenPair.id)).scalar() or 0
    transfers_n = s.query(func.count(TokenTransfer.id)).scalar() or 0
    return {
        "scanner_running": bool(SCANNER["collectors"]),
        "mock_mode": SCANNER.get("mock_mode", False),
        "started_at": SCANNER.get("started_at"),
        "chains": chains_out,
        "counts": {"tokens": tokens_n, "pairs": pairs_n,
                   "transfers": transfers_n, "large_transactions": with_s},
        "thresholds": {"min_usd_alert": settings.min_usd_alert,
                       "min_anomaly_score": settings.min_anomaly_score},
    }


@app.get("/api/chains")
def chains(s: Session = Depends(get_session)):
    out = []
    for c in s.query(Chain).all():
        st = s.get(ScannerState, c.id)
        col = SCANNER["collectors"].get(c.key)
        out.append({"key": c.key, "name": c.name, "chain_id": c.chain_id,
                    "native_symbol": c.native_symbol, "head_block": c.head_block,
                    "last_processed_block": st.last_processed_block if st else 0,
                    "connected": bool(col and col.running and not col.last_error)})
    return out


@app.get("/api/tokens")
def tokens(chain: Optional[str] = None, category: Optional[str] = None,
           limit: int = Query(50, le=500), offset: int = 0,
           s: Session = Depends(get_session)):
    q = s.query(Token)
    if chain and chain != "all":
        q = q.join(Chain, Chain.id == Token.chain_pk).filter(Chain.key == chain)
    if category and category != "all":
        q = q.filter(Token.category == category)
    rows = q.order_by(Token.last_seen.desc()).offset(offset).limit(limit).all()
    ch = _chain_map(s)
    return [{"address": t.address, "chain": ch[t.chain_pk].key if t.chain_pk in ch else None,
             "symbol": t.symbol, "name": t.name, "decimals": t.decimals,
             "category": t.category, "discovery_source": t.discovery_source,
             "transfer_count": t.transfer_count, "active": t.active,
             "has_metadata": t.has_metadata,
             "price_usd": float(t.price_usd) if t.price_usd is not None else None,
             "price_source": t.price_source,
             "liquidity_usd": float(t.liquidity_usd) if t.liquidity_usd is not None else None,
             "first_seen": t.first_seen.isoformat() if t.first_seen else None,
             "last_seen": t.last_seen.isoformat() if t.last_seen else None} for t in rows]


@app.get("/api/tokens/recent")
def recent_tokens(hours: float = Query(48, gt=0), limit: int = Query(30, le=200),
                  s: Session = Depends(get_session)):
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = (s.query(Token).filter(Token.first_seen >= since)
            .order_by(Token.first_seen.desc()).limit(limit).all())
    ch = _chain_map(s)
    return [{"address": t.address, "chain": ch[t.chain_pk].key if t.chain_pk in ch else None,
             "symbol": t.symbol, "name": t.name, "category": t.category,
             "discovery_source": t.discovery_source,
             "first_seen": t.first_seen.isoformat() if t.first_seen else None,
             "first_transfer_block": t.first_transfer_block} for t in rows]


@app.get("/api/transactions/large")
def large(chain: Optional[str] = None, category: Optional[str] = None,
          min_usd: float = Query(settings.min_usd_alert),
          min_score: float = Query(0), sort: str = Query("score"),
          limit: int = Query(50, le=500), s: Session = Depends(get_session)):
    return query_large(s, chain, None, category, min_usd, min_score, sort, limit)


@app.get("/api/transactions/in")
def big_in(chain: Optional[str] = None, category: Optional[str] = None,
           min_usd: float = Query(settings.min_usd_alert), min_score: float = 0,
           sort: str = Query("score"), limit: int = Query(30, le=500),
           s: Session = Depends(get_session)):
    return query_large(s, chain, "IN", category, min_usd, min_score, sort, limit)


@app.get("/api/transactions/out")
def big_out(chain: Optional[str] = None, category: Optional[str] = None,
            min_usd: float = Query(settings.min_usd_alert), min_score: float = 0,
            sort: str = Query("score"), limit: int = Query(30, le=500),
            s: Session = Depends(get_session)):
    return query_large(s, chain, "OUT", category, min_usd, min_score, sort, limit)


# ---- coin-address watch list (big-tx capture for specific tokens) --------
def _discovery():
    d = SCANNER.get("discovery")
    if d is None:
        raise HTTPException(503, "scanner not started yet")
    return d


@app.get("/api/watch")
def watch_list(s: Session = Depends(get_session)):
    ch = _chain_map(s)
    rows = (s.query(Token).filter_by(discovery_source="manual_watch")
            .order_by(Token.first_seen.desc()).all())
    return [{"address": t.address,
             "chain": ch[t.chain_pk].key if t.chain_pk in ch else None,
             "symbol": t.symbol, "name": t.name,
             "transfer_count": t.transfer_count,
             "first_seen": t.first_seen.isoformat() if t.first_seen else None}
            for t in rows]


@app.post("/api/watch")
async def watch_add(payload: dict = Body(...)):
    chain_key = (payload.get("chain") or "").strip().lower()
    address = (payload.get("address") or "").strip()
    if not chain_key or not address:
        raise HTTPException(422, "body must contain 'chain' and 'address'")
    try:
        added = await asyncio.to_thread(_discovery().add_watch_token,
                                        chain_key, address)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "added": added,
            "message": "watch token added" if added
                       else "token already tracked on this chain"}


@app.delete("/api/watch")
async def watch_remove(payload: dict = Body(...)):
    chain_key = (payload.get("chain") or "").strip().lower()
    address = (payload.get("address") or "").strip()
    if not chain_key or not address:
        raise HTTPException(422, "body must contain 'chain' and 'address'")
    removed = await asyncio.to_thread(_discovery().remove_watch_token,
                                      chain_key, address)
    return {"ok": True, "removed": bool(removed)}


@app.get("/api/categories")
def categories():
    return CATEGORIES


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Crypto Intelligence Scanner</title>
<style>
 body{background:#0d1117;color:#e6edf3;font-family:Segoe UI,Arial,sans-serif;margin:0;padding:16px}
 h1{font-size:20px} h2{font-size:15px;color:#8b949e;border-bottom:1px solid #21262d;padding-bottom:4px}
 table{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:18px}
 th,td{padding:4px 8px;border-bottom:1px solid #21262d;text-align:left;white-space:nowrap}
 th{color:#8b949e} .in{color:#3fb950}.out{color:#f85149}.score{color:#d29922;font-weight:bold}
 .dot{font-size:14px} select,input{background:#161b22;color:#e6edf3;border:1px solid #30363d;padding:4px;border-radius:4px}
 .filters{margin-bottom:14px;display:flex;gap:10px;flex-wrap:wrap}
 a{color:#58a6ff;text-decoration:none}
</style></head><body>
<h1>🛰️ CRYPTO INTELLIGENCE SCANNER <span id="mode"></span></h1>
<div id="status">Scanner Status: <span class="dot">⚪</span> starting…</div>
<div id="chains" style="margin:6px 0 14px"></div>
<div class="filters">
 Chain:<select id="fChain"><option value="all">ALL</option></select>
 Flow:<select id="fFlow"><option value="all">ALL</option><option>IN</option><option>OUT</option></select>
 Category:<select id="fCat"><option value="all">ALL</option></select>
 Min USD:<input id="fUsd" type="number" value="10000" style="width:100px">
 Min Score:<input id="fScore" type="number" value="0" style="width:60px">
 Sort:<select id="fSort"><option value="score">Anomaly score</option><option value="usd">USD value</option><option value="relative">Relative size</option><option value="newest">Newest</option><option value="liquidity">Liquidity impact</option></select>
</div>
<h2>LIVE LARGE TRANSACTIONS</h2><table id="tLarge"></table>
<h2>⬆️ BIG IN</h2><table id="tIn"></table>
<h2>⬇️ BIG OUT</h2><table id="tOut"></table>
<h2>🆕 RECENTLY DISCOVERED TOKENS</h2><table id="tTokens"></table>
<h2>👁️ WATCHED COIN ADDRESSES (big-tx capture)</h2>
<div class="filters">
 Chain:<select id="wChain"></select>
 Address:<input id="wAddr" placeholder="0x… token contract address" style="width:340px">
 <button id="wAdd" onclick="addWatch()">➕ Add to Watch</button>
</div>
<table id="tWatch"></table>
<script>

const fmtUsd=v=>v==null?null:'$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:0});
const short=a=>a?a.slice(0,6)+'…'+a.slice(-4):'';
function rows(list){
 if(!list.length)return '<tr><td colspan="8">no data yet…</td></tr>';
 let h='<tr><th>TOKEN</th><th>CHAIN</th><th>FLOW</th><th>VALUE</th><th>FROM</th><th>TO</th><th>RELATIVE</th><th>SCORE</th></tr>';
 for(const r of list){
  const fl=r.flow==='IN'?'<span class="in">IN</span>':(r.flow==='OUT'?'<span class="out">OUT</span>':r.flow);
  h+=`<tr><td>${r.token_symbol||short(r.token_address)}</td><td>${r.chain}</td><td>${fl}</td><td>${fmtUsd(r.usd_value)||'—'}</td><td><a target=_blank href=#>${short(r.from)}</a></td><td><a target=_blank href=#>${short(r.to)}</a></td><td>${r.relative_size?r.relative_size+'x':'—'}</td><td class=score>${Math.round(r.anomaly_score)}</td></tr>`;
 } return h;}
async function j(u){const r=await fetch(u);return r.json();}
async function jpost(u,body){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return {status:r.status,data:await r.json().catch(()=>({}))};}
let INITED=false;
async function initFilters(){
 if(INITED)return; INITED=true;
 const cats=await j('/api/categories');
 document.getElementById('fCat').insertAdjacentHTML('beforeend',cats.map(c=>`<option>${c}</option>`).join(''));
 const ch=await j('/api/chains');
 const opts=ch.map(c=>`<option value="${c.key}">${c.name}</option>`).join('');
 document.getElementById('fChain').insertAdjacentHTML('beforeend',opts);
 document.getElementById('wChain').innerHTML=opts;
}
async function addWatch(){
 const addr=document.getElementById('wAddr').value.trim();
 const chain=document.getElementById('wChain').value;
 if(!addr){alert('enter a token contract address (0x…)');return;}
 const {status,data}=await jpost('/api/watch',{chain,address:addr});
 if(status===200){document.getElementById('wAddr').value='';refresh();}
 else alert(data.detail||JSON.stringify(data));
}
async function delWatch(chain,addr){
 await fetch('/api/watch',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({chain,address:addr})});
 refresh();
}
function params(extra=''){const p=new URLSearchParams({chain:fChain.value,category:fCat.value,min_usd:fUsd.value,min_score:fScore.value,...(extra?{flow:extra}:{})});return p;}
async function refresh(){
 try{
  await initFilters();
  const st=await j('/api/status');
  document.getElementById('mode').textContent=st.mock_mode?'[MOCK DEV MODE]':'';
  const ok=Object.values(st.chains).every(c=>c.connected)&&st.scanner_running;
  document.getElementById('status').innerHTML='Scanner Status: <span class="dot">'+(ok?'🟢 RUNNING':'🟡 STARTING')+'</span> &nbsp; tokens:'+st.counts.tokens+' transfers:'+st.counts.transfers+' large:'+st.counts.large_transactions;
  const ch=await j('/api/chains');
  document.getElementById('chains').innerHTML=ch.map(c=>`${c.name} ${c.connected?'🟢':'🔴'} (block ${(c.last_processed_block||0).toLocaleString()})`).join('&nbsp;&nbsp; ');
  document.getElementById('tLarge').innerHTML=rows(await j('/api/transactions/large?'+params()+'&sort='+fSort.value+'&limit=40'));
  document.getElementById('tIn').innerHTML=rows(await j('/api/transactions/in?'+params('IN')+'&limit=15'));
  document.getElementById('tOut').innerHTML=rows(await j('/api/transactions/out?'+params('OUT')+'&limit=15'));
  const rt=await j('/api/tokens/recent?limit=15');
  let th='<tr><th>ADDRESS</th><th>CHAIN</th><th>SYMBOL</th><th>NAME</th><th>CATEGORY</th><th>SOURCE</th><th>FIRST SEEN</th></tr>';
  for(const t of rt) th+=`<tr><td>${short(t.address)}</td><td>${t.chain}</td><td>${t.symbol||'—'}</td><td>${t.name||'—'}</td><td>${t.category}</td><td>${t.discovery_source}</td><td>${t.first_seen?t.first_seen.slice(0,19)+'Z':'—'}</td></tr>`;
  document.getElementById('tTokens').innerHTML=rt.length?th:'<tr><td>none yet…</td></tr>';
  const wl=await j('/api/watch');
  let wh='<tr><th>ADDRESS</th><th>CHAIN</th><th>SYMBOL</th><th>TRANSFERS</th><th>SINCE</th><th></th></tr>';
  for(const t of wl) wh+=`<tr><td>${short(t.address)}</td><td>${t.chain}</td><td>${t.symbol||'—'}</td><td>${t.transfer_count||0}</td><td>${t.first_seen?t.first_seen.slice(0,19)+'Z':'—'}</td><td><a href="#" onclick="delWatch('${t.chain}','${t.address}');return false">remove</a></td></tr>`;
  document.getElementById('tWatch').innerHTML=wl.length?wh:'<tr><td>no watched addresses — add a coin contract above to capture every large transaction on it</td></tr>';
 }catch(e){console.error(e);}
}
refresh(); setInterval(refresh,5000);
</script></body></html>"""
