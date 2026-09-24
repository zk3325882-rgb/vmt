"""Phase 2 — Wallet Intelligence Engine.

Sits directly behind the Phase 1 transfer pipeline:

    NEW BLOCK -> TRANSFERS -> TOKEN DISCOVERY -> LARGE TX DETECTION
            -> WALLET UPDATE -> CLASSIFICATION -> WHALE SCORE
            -> BEHAVIOR -> CLUSTERING -> WHALE EVENTS -> DB

Design rules:
* Incremental only: per-block deltas are applied to wallet rows; we never
  rescan history (8 GB RAM friendly).
* Observation vs interpretation kept separate: classifications/flags are
  behavior-based, never identity or price claims.
* Bounded work per call (max_wallet_analysis_per_block) so one huge block
  cannot stall the scanner.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.database.models import (
    AddressLabel, DexInteraction, Token, TokenTransfer, Wallet, WalletCluster,
    WalletHolding, WalletSnapshot, WhaleEvent,
)
from app.wallets.labels import LabelCache
from config import wallet_settings as WS

log = logging.getLogger("wallets")

ZERO_ADDR = "0x" + "0" * 40


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# whale scoring (dynamic, multi-signal, explainable)
# ---------------------------------------------------------------------------

@dataclass
class WhaleScoreResult:
    whale_score: float
    absolute_score: float
    relative_score: float
    liquidity_impact_score: float
    activity_score: float
    is_whale_event: bool
    reasons: list[str] = field(default_factory=list)


def _log_scale(value: float, floor: float, ceil_: float) -> float:
    """0..100 log mapping between floor and ceil."""
    if value <= 0:
        return 0.0
    v = max(value, floor)
    if v >= ceil_:
        return 100.0
    return (math.log10(v / floor) / math.log10(ceil_ / floor)) * 100.0


def dynamic_floor_usd(chain_key: str, token_liquidity_usd: float | None) -> float:
    """Dynamic threshold: a $100k move is noise on a deep pool but enormous
    on a thin one. Floor scales with token liquidity when known."""
    base = WS.whale_floor_usd.get(chain_key, 25000.0)
    if token_liquidity_usd and token_liquidity_usd > 0:
        # require at least 0.5% of pool liquidity to count as 'big' here
        return max(base * 0.2, min(base * 20, token_liquidity_usd * 0.005))
    return base


def compute_whale_score(usd_value: float | None,
                        chain_key: str,
                        relative_size: float | None = None,
                        liquidity_usd: float | None = None,
                        tx_count: int = 0,
                        portfolio_usd: float | None = None) -> WhaleScoreResult:
    """Combine absolute / relative / liquidity-impact / activity components
    into a 0-100 event-importance score. Weights come from config."""
    usd = usd_value or 0.0
    floor = dynamic_floor_usd(chain_key, liquidity_usd)

    absolute_score = _log_scale(usd, floor, floor * 2000) if usd > 0 else 0.0
    relative_score = min(100.0, (relative_size / 50.0) * 100.0) if relative_size and relative_size > 0 else 0.0
    if liquidity_usd and liquidity_usd > 0 and usd > 0:
        impact = usd / liquidity_usd          # 5% of pool -> 100
        liquidity_impact_score = min(100.0, impact * 2000)
    else:
        liquidity_impact_score = 0.0
    activity_score = _log_scale(max(tx_count, 0), 10, 10000)
    if portfolio_usd and portfolio_usd > 0:
        activity_score = max(activity_score, _log_scale(portfolio_usd, 1e4, 1e8) * 0.9)

    total = (WS.w_absolute * absolute_score
             + WS.w_relative * relative_score
             + WS.w_liquidity * liquidity_impact_score
             + WS.w_activity * activity_score)
    total = round(min(100.0, max(0.0, total)), 2)

    reasons = []
    if usd >= floor and usd > 0:
        reasons.append(f"${usd:,.0f} >= dynamic floor ${floor:,.0f}")
    if relative_size and relative_size >= 3:
        reasons.append(f"{relative_size:.1f}x normal size")
    if liquidity_impact_score >= 30:
        reasons.append("high liquidity impact")
    if activity_score >= 50:
        reasons.append("historically high activity/portfolio")
    return WhaleScoreResult(total, round(absolute_score, 2), round(relative_score, 2),
                            round(liquidity_impact_score, 2), round(activity_score, 2),
                            total >= WS.whale_min_score, reasons)


# ---------------------------------------------------------------------------
# behavior scoring (accumulation-like / distribution-like / activity)
# ---------------------------------------------------------------------------

@dataclass
class BehaviorResult:
    accumulation_score: float
    distribution_score: float
    activity_score: float
    flags: list[str]
    reasons: str


def compute_behavior(inflow_usd: float, outflow_usd: float,
                     exchange_in_usd: float, exchange_out_usd: float,
                     dex_volume_usd: float, tx_count: int,
                     large_in: int = 0, large_out: int = 0,
                     window_days: int | None = None) -> BehaviorResult:
    """Explainable 0-100 scores describing observed flow patterns.
    'Accumulation-like' / 'distribution-like' — never claims of buying/selling."""
    window_days = window_days or WS.behavior_window_days
    total = inflow_usd + outflow_usd
    reasons: list[str] = []
    acc = dist = 0.0
    if total > 0:
        net_ratio = (inflow_usd - outflow_usd) / total      # -1 .. +1
        acc = max(0.0, net_ratio) * 60
        dist = max(0.0, -net_ratio) * 60
        if net_ratio > 0.2:
            reasons.append(f"net inflow {net_ratio*100:.0f}% (${inflow_usd-outflow_usd:,.0f})")
        elif net_ratio < -0.2:
            reasons.append(f"net outflow {-net_ratio*100:.0f}% (${outflow_usd-inflow_usd:,.0f})")
    if inflow_usd > 0 and exchange_in_usd / max(inflow_usd, 1) > 0.3:
        acc += min(25, 25 * exchange_in_usd / max(inflow_usd, 1))
        reasons.append("exchange->wallet inflows")
    if outflow_usd > 0 and exchange_out_usd / max(outflow_usd, 1) > 0.3:
        dist += min(25, 25 * exchange_out_usd / max(outflow_usd, 1))
        reasons.append("wallet->exchange outflows")
    if large_in:
        acc += min(10, large_in * 2)
        reasons.append(f"{large_in} large inbound")
    if large_out:
        dist += min(10, large_out * 2)
        reasons.append(f"{large_out} large outbound")
    if dex_volume_usd > 0 and total > 0:
        share = dex_volume_usd / total
        bonus = min(5, share * 5)
        if inflow_usd >= outflow_usd:
            acc += bonus
        else:
            dist += bonus
        reasons.append("DEX venue activity")

    daily = tx_count / max(window_days, 1)
    activity = min(100.0, _log_scale(max(daily, 0.001), 0.1, 200))

    flags: list[str] = []
    if acc >= 70:
        flags.append("UNUSUAL_ACCUMULATION")
    if dist >= 70:
        flags.append("UNUSUAL_DISTRIBUTION")
    if daily >= 50:
        flags.append("HIGH_FREQUENCY")
        reasons.append(f"{daily:.0f} tx/day")
    return BehaviorResult(round(min(100.0, acc), 2), round(min(100.0, dist), 2),
                          round(activity, 2), flags, "; ".join(reasons)[:500])


# ---------------------------------------------------------------------------
# classification helpers
# ---------------------------------------------------------------------------

ENTITY_TO_TYPE = {"exchange": "EXCHANGE", "dex": "DEX", "bridge": "BRIDGE",
                  "treasury": "TREASURY", "burn": "BURN_ADDRESS",
                  "protocol": "CONTRACT", "contract": "CONTRACT"}


def classify_wallet(address: str, label: AddressLabel | None,
                    is_contract: bool, is_pair: bool, is_token: bool,
                    in_count: int, out_count: int, distinct_peers: int,
                    dex_share: float, tx_per_day: float) -> tuple[str, float]:
    """Return (wallet_type, confidence). Labelled entities win; otherwise
    observable behavior decides. Never claims ownership identity."""
    if address == ZERO_ADDR:
        return "BURN_ADDRESS", 1.0
    if label is not None:
        et = ENTITY_TO_TYPE.get(label.entity_type, "UNKNOWN")
        conf = float(label.confidence or 0.9)
        if et != "UNKNOWN":
            return et, conf
    if is_token:
        return "TOKEN_CONTRACT", 0.99
    if is_pair:
        return "LIQUIDITY_POOL", 0.95
    if is_contract:
        if dex_share > 0.5:
            return "DEX", 0.6
        return "CONTRACT", 0.7
    # EOA heuristics (behavioral, low-confidence)
    if distinct_peers >= 20 and in_count >= 15 and in_count > out_count * 3:
        return "TREASURY", 0.45          # receives from many addresses
    if distinct_peers >= 20 and out_count >= 15 and out_count > in_count * 3:
        return "BOT_LIKE", 0.4           # distributes to many addresses
    if tx_per_day >= 100:
        return "BOT_LIKE", 0.5
    return "NORMAL_WALLET", 0.5


def tier_for(portfolio_usd: float | None, tx_count: int) -> int:
    p = portfolio_usd or 0
    if p >= WS.tier1_min_usd:
        return 1
    if p >= WS.tier2_min_usd or tx_count >= WS.tier2_min_txs:
        return 2
    return 3


def _upsert(session: Session, model, values: dict, conflict_cols: list[str],
            update_cols: dict):
    dialect = session.bind.dialect.name if session.bind else "sqlite"
    ins = (pg_insert if dialect == "postgresql" else sqlite_insert)(model)
    session.execute(ins.values(**values).on_conflict_do_update(
        index_elements=conflict_cols, set_=update_cols))


class WalletIntelligenceService:
    """Incremental wallet updater invoked once per processed block range by
    the Phase 1 TransferCollector (single shared scanner — no second loop)."""

    def __init__(self, session_factory, chain_key: str, discovery,
                 label_cache: LabelCache | None = None):
        self.session_factory = session_factory
        self.chain_key = chain_key
        self.discovery = discovery
        self.labels = label_cache or LabelCache(session_factory)
        self.last_snapshot: dict[int, datetime] = {}

    # ---------------- main entry ----------------
    def process_transfers(self, chain_pk: int, transfers: list[dict]) -> list[dict]:
        """transfers: dicts produced by the Phase 1 pipeline containing
        tx_hash, token_address, from_address, to_address, normalized_dec,
        usd_value, timestamp, token_symbol, flow, transaction_type,
        relative_size. Returns whale-event summaries. Never raises: a bad
        wallet must not stop the scanner."""
        if not transfers:
            return []
        events: list[dict] = []
        try:
            with self.session_factory() as s:
                pairs = self.discovery.known_pairs(chain_pk)
                tok_rows = s.query(Token).filter_by(chain_pk=chain_pk).all()
                tokens = {t.address for t in tok_rows}
                token_liq = {t.address: float(t.liquidity_usd) if t.liquidity_usd else None
                             for t in tok_rows}
                touched: dict[int, dict] = {}
                for tr in transfers[:5000]:     # bounded per call
                    try:
                        self._apply_transfer(s, chain_pk, tr, pairs, tokens,
                                             token_liq, touched, events)
                    except Exception as e:
                        log.debug("wallet update skipped %s: %s",
                                  str(tr.get("tx_hash", "?"))[:12], str(e)[:120])
                self._finalize(s, chain_pk, touched, pairs, tokens)
                s.commit()
        except Exception as e:
            log.warning("[%s] wallet intelligence error (non-fatal): %s",
                        self.chain_key, str(e)[:200])
        return events

    # ---------------- per-transfer application ----------------
    def _apply_transfer(self, s: Session, chain_pk: int, tr: dict,
                        pairs: set, tokens: set,
                        token_liq: dict, touched: dict, events: list) -> None:
        frm, to = tr["from_address"].lower(), tr["to_address"].lower()
        usd = float(tr["usd_value"]) if tr.get("usd_value") is not None else None
        ts = tr.get("timestamp") or utcnow()
        liq = token_liq.get(tr["token_address"].lower())
        norm = tr.get("normalized_dec") or 0

        w_from = self._get_or_create_wallet(s, chain_pk, frm)
        w_to = self._get_or_create_wallet(s, chain_pk, to)

        lbl_to = self.labels.get(chain_pk, to)
        lbl_frm = self.labels.get(chain_pk, frm)

        d = touched.setdefault(w_from.id, self._blank_delta())
        d["peers"].add(to)
        d["out"] += 1
        if usd:
            d["outflow"] += usd
        if lbl_to is not None and lbl_to.entity_type == "exchange" and usd:
            d["exch_out"] += usd
        if to in pairs or (lbl_to is not None and lbl_to.entity_type == "dex"):
            d["dex_vol"] += usd or 0
            self._record_dex_interaction(s, chain_pk, frm, to, tr, usd, ts)
        self._update_holding(s, w_from, tr, -norm, usd, ts)

        d2 = touched.setdefault(w_to.id, self._blank_delta())
        d2["peers"].add(frm)
        d2["in"] += 1
        if usd:
            d2["inflow"] += usd
        if lbl_frm is not None and lbl_frm.entity_type == "exchange" and usd:
            d2["exch_in"] += usd
        if frm in pairs or (lbl_frm is not None and lbl_frm.entity_type == "dex"):
            d2["dex_vol"] += usd or 0
            self._record_dex_interaction(s, chain_pk, to, frm, tr, usd, ts)
        self._update_holding(s, w_to, tr, norm, usd, ts)

        # ---- whale event generation (dynamic thresholds) ----
        if usd is not None:
            rel = tr.get("relative_size")
            ws_res = compute_whale_score(
                usd, self.chain_key, rel, liq,
                (w_from.transaction_count or 0) + (w_to.transaction_count or 0),
                float(w_to.estimated_portfolio_value or 0))
            if ws_res.is_whale_event:
                subj_addr = frm if tr.get("flow") == "OUT" else to
                subj = w_from if tr.get("flow") == "OUT" else w_to
                evt_type = "LARGE_OUTFLOW" if tr.get("flow") == "OUT" else "LARGE_INFLOW"
                explanation = ("Observed: $%s moved %s->%s (%s). Interpretation: %s"
                               % (f"{usd:,.0f}", frm[:10], to[:10],
                                  tr.get("transaction_type", "TRANSFER"),
                                  "; ".join(ws_res.reasons[:3]) or "large valued transfer"))
                ev = self._add_whale_event(s, chain_pk, subj.id, subj_addr, tr,
                                           evt_type, usd, ws_res, [], explanation, ts)
                if ev:
                    events.append(ev)
                if ws_res.whale_score >= 75:
                    pat_type = ("ACCUMULATION_LIKE" if evt_type == "LARGE_INFLOW"
                                else "DISTRIBUTION_LIKE")
                    self._add_whale_event(
                        s, chain_pk, subj.id, subj_addr, tr, pat_type, usd, ws_res, [],
                        f"Large repeated {evt_type.lower()} observed "
                        f"({pat_type.lower().replace('_like','')}-like behavior; "
                        "not a buy/sell claim).", ts)
                # tag wallet type overlay for very high scores
                if ws_res.absolute_score >= 80 and subj.wallet_type == "NORMAL_WALLET":
                    subj.wallet_type = "WHALE"
                    subj.classification_confidence = 0.5

        # ---- clustering signals (cheap, incremental) ----
        self._cluster_signals(s, chain_pk, w_from, w_to, tr, ts)

    @staticmethod
    def _blank_delta() -> dict:
        return {"in": 0, "out": 0, "inflow": 0.0, "outflow": 0.0,
                "exch_in": 0.0, "exch_out": 0.0, "dex_vol": 0.0,
                "peers": set()}

    # ---------------- wallet rows ----------------
    def _get_or_create_wallet(self, s: Session, chain_pk: int,
                              address: str) -> Wallet:
        w = s.query(Wallet).filter_by(chain_pk=chain_pk, address=address).first()
        if w is None:
            vals = dict(chain_pk=chain_pk, address=address, wallet_type="UNKNOWN",
                        first_seen=utcnow(), last_seen=utcnow(), tier=3)
            _upsert(s, Wallet, vals, ["chain_pk", "address"], {"last_seen": utcnow()})
            s.expire_all()
            w = s.query(Wallet).filter_by(chain_pk=chain_pk, address=address).first()
            if w is None:
                raise RuntimeError("wallet upsert failed")
        return w

    def _record_dex_interaction(self, s: Session, chain_pk: int, wallet_addr: str,
                                venue_addr: str, tr: dict, usd: float | None,
                                ts: datetime) -> None:
        vals = dict(chain_pk=chain_pk, wallet_address=wallet_addr,
                    dex_name="pool", pool_address=venue_addr,
                    token_address=tr["token_address"].lower(),
                    interaction_type=("SWAP" if tr.get("transaction_type") == "DEX_ACTIVITY"
                                      else "UNKNOWN"),
                    tx_hash=tr["tx_hash"], usd_value=usd, timestamp=ts)
        dialect = s.bind.dialect.name if s.bind else "sqlite"
        ins = (pg_insert if dialect == "postgresql" else sqlite_insert)(DexInteraction)
        s.execute(ins.values(**vals).on_conflict_do_nothing(
            index_elements=["chain_pk", "tx_hash", "wallet_address", "dex_name"]))

    def _update_holding(self, s: Session, wallet: Wallet, tr: dict,
                        delta: float, usd: float | None, ts: datetime) -> None:
        if not delta:
            return
        token_addr = tr["token_address"].lower()
        h = s.query(WalletHolding).filter_by(wallet_id=wallet.id,
                                             token_address=token_addr).first()
        if h is None:
            h = WalletHolding(wallet_id=wallet.id, chain_pk=wallet.chain_pk,
                              wallet_address=wallet.address, token_address=token_addr,
                              amount=0, usd_value=None, last_updated=ts)
            s.add(h)
            s.flush()
        h.amount = float(h.amount or 0) + delta
        if usd is not None:
            h.usd_value = float(h.usd_value or 0) + usd   # NULL stays NULL if no price
        h.last_updated = ts

    # ---------------- finalize aggregates per block range ----------------
    def _finalize(self, s: Session, chain_pk: int, touched: dict,
                  pairs: set, tokens: set) -> None:
        now = utcnow()
        limit = WS.max_wallet_analysis_per_block
        for i, (wid, d) in enumerate(touched.items()):
            if i >= limit:
                break
            w = s.get(Wallet, wid)
            if w is None:
                continue
            label = self.labels.get(chain_pk, w.address)
            w.transaction_count = (w.transaction_count or 0) + d["in"] + d["out"]
            w.in_count = (w.in_count or 0) + d["in"]
            w.out_count = (w.out_count or 0) + d["out"]
            w.exchange_inflow_usd = float(w.exchange_inflow_usd or 0) + d["exch_in"]
            w.exchange_outflow_usd = float(w.exchange_outflow_usd or 0) + d["exch_out"]
            w.dex_volume_usd = float(w.dex_volume_usd or 0) + d["dex_vol"]
            est = float(w.estimated_portfolio_value or 0) + d["inflow"] - d["outflow"]
            w.estimated_portfolio_value = max(est, 0)
            w.estimated_activity_volume = (float(w.estimated_activity_volume or 0)
                                           + d["inflow"] + d["outflow"])
            w.token_count = (s.query(func.count(WalletHolding.id))
                             .filter_by(wallet_id=w.id).scalar() or 0)
            w.last_seen = now
            days_active = max((now - w.first_seen).total_seconds() / 86400, 1)
            act_vol = float(w.estimated_activity_volume or 1)
            wtype, conf = classify_wallet(
                w.address, label, bool(w.is_contract),
                w.address in pairs, w.address in tokens,
                w.in_count, w.out_count, len(d["peers"]),
                float(w.dex_volume_usd) / max(act_vol, 1),
                w.transaction_count / days_active)
            ws_res = compute_whale_score(None, self.chain_key, None, None,
                                         w.transaction_count,
                                         float(w.estimated_portfolio_value or 0))
            if ws_res.activity_score >= WS.whale_min_score and wtype == "NORMAL_WALLET":
                wtype, conf = "WHALE", round(ws_res.activity_score / 100, 3)
            w.wallet_type = wtype
            w.classification_confidence = conf
            if label:
                w.label = label.label
            beh = compute_behavior(d["inflow"], d["outflow"], d["exch_in"],
                                   d["exch_out"], d["dex_vol"], d["in"] + d["out"])
            alpha = 0.3   # EMA blend so single blocks don't dominate
            w.accumulation_score = round((float(w.accumulation_score or 0)) * (1 - alpha)
                                         + beh.accumulation_score * alpha, 2)
            w.distribution_score = round((float(w.distribution_score or 0)) * (1 - alpha)
                                         + beh.distribution_score * alpha, 2)
            w.activity_score = round((float(w.activity_score or 0)) * (1 - alpha)
                                     + beh.activity_score * alpha, 2)
            flags = set((w.behavior_flags or "").split(",")) - {""}
            flags |= set(beh.flags)
            w.behavior_flags = ",".join(sorted(flags))[:250] or None
            w.behavior_reasons = beh.reasons or w.behavior_reasons
            w.whale_score = round(max(float(w.whale_score or 0) * 0.95,
                                      ws_res.activity_score), 2)
            w.risk_score = round(min(100.0, len(flags) * 20.0), 2)
            w.tier = tier_for(float(w.estimated_portfolio_value or 0),
                              w.transaction_count)
            if w.tier <= 2:
                self._snapshot(s, w, d, now)

    def _snapshot(self, s: Session, w: Wallet, d: dict, now: datetime) -> None:
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        dialect = s.bind.dialect.name if s.bind else "sqlite"
        ins = (pg_insert if dialect == "postgresql" else sqlite_insert)(WalletSnapshot)
        stmt = ins.values(
            wallet_id=w.id, date=day, tx_count=d["in"] + d["out"],
            in_count=d["in"], out_count=d["out"], inflow_usd=d["inflow"],
            outflow_usd=d["outflow"], net_flow_usd=d["inflow"] - d["outflow"],
            dex_volume_usd=d["dex_vol"], exchange_inflow_usd=d["exch_in"],
            exchange_outflow_usd=d["exch_out"], liquidity_activity_usd=0,
            portfolio_usd=float(w.estimated_portfolio_value or 0))
        upd = {c: getattr(WalletSnapshot, c) + getattr(stmt.excluded, c)
               for c in ("tx_count", "in_count", "out_count", "inflow_usd",
                         "outflow_usd", "net_flow_usd", "dex_volume_usd",
                         "exchange_inflow_usd", "exchange_outflow_usd")}
        upd["portfolio_usd"] = stmt.excluded.portfolio_usd
        s.execute(stmt.on_conflict_do_update(index_elements=["wallet_id", "date"],
                                             set_=upd))

    # ---------------- whale events ----------------
    def _add_whale_event(self, s: Session, chain_pk: int, wallet_id: int,
                         wallet_addr: str, tr: dict, event_type: str,
                         usd: float, ws_res: WhaleScoreResult,
                         flags: list[str], explanation: str,
                         ts: datetime) -> dict | None:
        vals = dict(chain_pk=chain_pk, wallet_id=wallet_id,
                    wallet_address=wallet_addr,
                    token_address=tr["token_address"].lower(),
                    token_symbol=tr.get("token_symbol"),
                    tx_hash=tr["tx_hash"], event_type=event_type,
                    usd_value=usd, whale_score=ws_res.whale_score,
                    accumulation_score=0, distribution_score=0,
                    behavior_flags=",".join(flags) or None,
                    explanation=explanation[:500], timestamp=ts)
        dialect = s.bind.dialect.name if s.bind else "sqlite"
        ins = (pg_insert if dialect == "postgresql" else sqlite_insert)(WhaleEvent)
        result = s.execute(ins.values(**vals).on_conflict_do_nothing(
            index_elements=["tx_hash", "wallet_address", "event_type"]))
        if getattr(result, "rowcount", 1):
            return {"event_type": event_type, "wallet": wallet_addr,
                    "usd": usd, "score": ws_res.whale_score,
                    "explanation": explanation}
        return None

    # ---------------- clustering (incremental behavioral signals) ----------
    def _cluster_signals(self, s: Session, chain_pk: int, w_from: Wallet,
                         w_to: Wallet, tr: dict, ts: datetime) -> None:
        """FREQUENT_COUNTERPARTY: repeated direct transfers between two EOAs.
        Stored as *possible* behavioral relationships only."""
        a, b = w_from.address, w_to.address
        if a == b or w_from.is_contract or w_to.is_contract:
            return
        key = hashlib.sha1(f"{a}|{b}".encode()).hexdigest()[:16]
        existing = s.query(WalletCluster).filter_by(cluster_id=key).first()
        if existing:
            existing.relationship_score = min(100.0, float(existing.relationship_score) + 15)
            existing.last_seen = ts
            if existing.relationship_score >= WS.cluster_min_score:
                existing.relationship_type = "FREQUENT_COUNTERPARTY"
        else:
            s.add(WalletCluster(cluster_id=key, chain_pk=chain_pk,
                                wallet_address=a, related_address=b,
                                relationship_score=15,
                                relationship_type="POSSIBLE_CLUSTER",
                                first_seen=ts, last_seen=ts))

    # ---------------- periodic sweeps (maintenance loop, not per-tx) -------
    def detect_common_funders(self) -> int:
        """Wallets that funded >= common_funder_min_wallets distinct
        counterparties within recent observed transfers."""
        added = 0
        with self.session_factory() as s:
            rows = (s.query(TokenTransfer.from_address,
                            func.count(func.distinct(TokenTransfer.to_address)),
                            func.min(TokenTransfer.chain_pk))
                    .group_by(TokenTransfer.from_address)
                    .having(func.count(func.distinct(TokenTransfer.to_address))
                            >= WS.common_funder_min_wallets).limit(500).all())
            for addr, n, chain_pk in rows:
                key = hashlib.sha1(f"funder:{addr}".encode()).hexdigest()[:16]
                exists = s.query(WalletCluster).filter_by(cluster_id=key).first()
                if not exists:
                    s.add(WalletCluster(cluster_id=key, chain_pk=chain_pk,
                                        wallet_address=addr, related_address=None,
                                        relationship_score=min(100.0, n * 10.0),
                                        relationship_type="COMMON_FUNDER"))
                    added += 1
            s.commit()
        return added

    def detect_circular_flows(self) -> list[dict]:
        """Detect A->B->A and A->B->C->A cycles among recent transfers.
        Flags CIRCULAR_FLOW on involved wallets — requires interpretation,
        never auto-labels as fraud."""
        cycles: list[dict] = []
        with self.session_factory() as s:
            recent = (s.query(TokenTransfer.from_address, TokenTransfer.to_address)
                      .order_by(TokenTransfer.id.desc()).limit(20000).all())
            edges: set[tuple[str, str]] = set()
            out_map: dict[str, set[str]] = {}
            for f, t in recent:
                if f != t:
                    edges.add((f, t))
                    out_map.setdefault(f, set()).add(t)
            flagged: set[str] = set()
            for (a, b) in edges:
                if (b, a) in edges:
                    cycles.append({"path": f"{a} <-> {b}", "length": 2})
                    flagged.update([a, b])
                for c in out_map.get(b, ()):
                    if c != a and c in out_map and a in out_map[c]:
                        cycles.append({"path": f"{a} -> {b} -> {c} -> {a}", "length": 3})
                        flagged.update([a, b, c])
                if len(cycles) >= 50:
                    break
            for addr in list(flagged)[:200]:
                w = s.query(Wallet).filter_by(address=addr).first()
                if w:
                    flags = set((w.behavior_flags or "").split(",")) - {""}
                    flags.add("CIRCULAR_FLOW")
                    w.behavior_flags = ",".join(sorted(flags))[:250]
            s.commit()
        return cycles[:50]
