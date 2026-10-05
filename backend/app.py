"""FastAPI + WebSocket. The only surface the frontend talks to.

    uvicorn backend.app:app --reload --port 8000

Commands arrive over the WebSocket or by POST; events go out over the WebSocket in the
shape frozen in core/events.py. CORS is wide open because this binds to localhost and
never leaves the machine.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import contextlib
import contextvars
import math
import re
from pathlib import Path

from fastapi import HTTPException, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from agents.llm import available, warm_up
from backend.bus import EventBus
from backend.intents import Intent, parse
from backend.router import route as route_input
from backend.pipeline import boot, emit_telemetry, handle
from analysis import attribution as attrib_mod
from analysis import factors as factors_mod
from analysis import rebalance as rebalance_mod
from analysis import stress as stress_mod
from analysis import tax as tax_mod
from analysis import xray as xray_mod
from backend import actions as actions_mod
from backend import ledger as ledger_mod
from backend import tts as tts_mod
from backend import guide as guide_mod
from backend import tours as tours_mod
from backend import understand as understand_mod
from backend import hindi_input
from backend import vernacular as vernacular_mod
from analysis import scanner as scanner_mod
from analysis import shield as shield_mod
from backend import drilldown as drilldown_mod
from backend import explain, portfolios
from backend import flows as flows_mod
from backend import palette as palette_mod
from backend import report as report_mod
from backend import sandbox as sandbox_mod
from backend import watchlist as watchlist_mod
from backend.calibration import ensure_sim_clock_column, score
from core import universe
from backend.recorder import RECORDINGS, Player, Recorder, list_takes
from backend.session import Session
from risk.engine import RiskEngine
from risk.policy import Policy
from risk.portfolio import Proposal
from voice import stt
from core.events import PROTOCOL_VERSION, EventType

class RoundedJSONResponse(JSONResponse):
    """Round every float on the way out, once, instead of at each call site.

    Binary floating point leaks its own representation through any arithmetic chain:
    a NAV that is exactly ten lakh arrives as 999999.9999999999, a sector weight as
    0.3975177000000001. Most panels hide it because they format through `pct()` or
    `rupees()`, so the leak surfaces only where a number is printed raw -- which is
    how "10.768937614291199 positions" reached a printable report page.

    Patching each field as it is noticed is a losing game; every new endpoint
    reintroduces it. Rounding at the serialisation boundary fixes the ones that exist
    and the ones not written yet. Six decimals is far finer than anything here means
    (a weight to 1e-6 is a ten-thousandth of a percent) and well inside the precision
    the arithmetic actually carries.
    """

    PLACES = 6

    @classmethod
    def _clean(cls, node):
        if isinstance(node, float):
            # NaN and infinities are not JSON anyway; let the encoder deal with them.
            return round(node, cls.PLACES) if math.isfinite(node) else node
        if isinstance(node, dict):
            return {k: cls._clean(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [cls._clean(v) for v in node]
        return node

    def render(self, content) -> bytes:  # noqa: ANN001
        return super().render(self._clean(content))


app = FastAPI(title="JARVIS // ALPHA OS", version="0.1",
              default_response_class=RoundedJSONResponse)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

bus = EventBus()
session: Session | None = None
recorder: Recorder | None = None
player: Player | None = None
_tasks: set[asyncio.Task] = set()
# What was just discussed, so "what does it move with" resolves without naming the
# company again. One conversation per backend, like everything else here.
convo = explain.Conversation()
# Hypothetical trades. Never touches the live book until they are committed.
staged = sandbox_mod.Staged()

SCRIPT = Path(__file__).resolve().parent.parent / "config" / "script.yaml"


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


@app.on_event("startup")
async def _startup() -> None:
    global session
    session = Session.create()
    session.reprice()
    ensure_sim_clock_column(session.conn)
    portfolios.ensure_schema(session.conn)
    watchlist_mod.ensure_schema(session.conn)
    ledger_mod.ensure(session.conn)
    _seed_presets()
    _land_on_default()
    _seed_sample_lots()
    _spawn(asyncio.to_thread(tts_mod.warm_up))
    # Load the weights now. A cold first desk costs ~10 extra seconds and it always
    # lands on the command the audience is watching.
    _spawn(warm_up())
    _spawn(_heartbeat())
    # Whisper's first load can take 30s on a cold weights cache, and it would otherwise
    # land on the first thing the presenter says. Pay it now, off the critical path.
    _spawn(asyncio.to_thread(stt.warm_up))


def _seed_presets() -> None:
    """Materialise the presets as real, loadable portfolios on first boot.

    Weights are converted to share counts at live prices, so a preset is a portfolio
    like any other rather than a special case the rest of the code has to know about.
    """
    for preset in portfolios.PRESETS:
        # The demo fund is an institutional book and the scripted walkthrough quotes
        # its numbers; the retail presets are a private investor's pot.
        nav = 10_000_000 if preset["key"] == "demo_fund" else portfolios.DEFAULT_NAV
        cash, positions = portfolios.preset_to_positions(preset, session.prices, nav)
        if positions:
            portfolios.save(session.conn, preset["name"], cash, positions,
                            portfolio_id=f"preset_{preset['key']}", is_preset=True)


def _land_on_default() -> None:
    """Open on a coherent pairing: a realistic retail portfolio under retail limits.

    The session used to start on the hardcoded institutional fund while the policy
    default was retail -- a ten-lakh-limit yardstick held against a crore-scale book.
    A first-time visitor should land on the case the product is actually about.
    """
    data = portfolios.load(session.conn, "preset_typical_retail")
    if data:
        session.set_portfolio(data["name"], data["cash"], data["positions"],
                              "preset_typical_retail")


async def _heartbeat() -> None:
    """Keeps the telemetry rail moving. Numbers that never stop are what make the
    thing read as a live system rather than a slide.

    Suspended during replay. The live session sits at today's clock, so a heartbeat
    mid-take overwrites the replayed sim_clock and the HUD ends up showing a 2026 date
    above a 2020 NAV — two panels contradicting each other on stage.
    """
    while True:
        await asyncio.sleep(2.0)
        if player and player.active:
            continue
        if session and bus.subscribers:
            emit_telemetry(session, bus)


class Command(BaseModel):
    text: str
    lang: str | None = None      # the language of the screen that asked (see _lang)
    cid: str | None = None       # which browser tab asked, so only it shows and speaks the reply


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "protocol": PROTOCOL_VERSION, "ollama": available(),
            "stt": stt.info(),
            "replay": bool(player and player.active),
            "replay_name": player.name if player and player.active else None,
            "recording": recorder.name if recorder else None,
            "subscribers": bus.subscribers, "dropped": bus.dropped}


@app.get("/state")
async def state() -> dict:
    # While replaying, serve the recorded snapshot. Answering from the live session
    # would put 2026 numbers under a 2020 clock and the HUD would contradict itself.
    if player and player.active and player.state:
        return {**player.state, "replay": True}
    return session.snapshot() if session else {}


@app.get("/policy")
async def policy() -> dict:
    return session.policy.model_dump() if session else {}


@app.get("/script")
async def script() -> dict:
    """The presenter's running order. Rendered as a teleprompter in the HUD."""
    import yaml
    if not SCRIPT.exists():
        return {"title": "", "steps": []}
    return yaml.safe_load(SCRIPT.read_text(encoding="utf-8"))


@app.get("/profiles")
async def policy_profiles() -> dict:
    cfg = Policy.profiles()
    return {"profiles": [{"key": k, "name": v["name"], "blurb": v["blurb"],
                          "limits": v["limits"]}
                         for k, v in cfg["profiles"].items()],
            "active": session.policy.profile if session else cfg["default"]}


@app.post("/profiles/{key}")
async def set_profile(key: str) -> dict:
    """Switch limits. Same engine and same maths -- different thresholds."""
    if key not in Policy.profiles()["profiles"]:
        return {"ok": False, "reason": "unknown profile"}
    session.policy = Policy.from_profile(key)
    session.engine = RiskEngine(session.policy)
    _spawn(_announce_portfolio())
    return {"ok": True, "profile": key, "describes": session.policy.describe()}


@app.get("/universe")
async def universe_catalogue() -> dict:
    """Everything the ticker picker needs, with live prices."""
    prices = session.prices if session else {}
    rows = universe.catalogue()
    for r in rows:
        r["price"] = prices.get(r["ticker"])
    return {"tickers": rows, "sectors": universe.sector_names(),
            "sector_labels": universe.SECTOR_LABELS,
            "benchmark": universe.benchmark_label()}


@app.get("/portfolios")
async def list_portfolios() -> dict:
    return {"portfolios": portfolios.list_all(session.conn),
            "active": session.portfolio_id,
            "presets": [{k: p[k] for k in ("key", "name", "blurb")}
                        for p in portfolios.PRESETS]}


class PortfolioIn(BaseModel):
    name: str = "My portfolio"
    cash: float = 0.0
    positions: dict[str, int] = {}


@app.post("/portfolios")
async def create_portfolio(body: PortfolioIn) -> dict:
    clean = {t: n for t, n in body.positions.items()
             if universe.resolve(t) and n > 0}
    resolved = {universe.resolve(t): n for t, n in clean.items()}
    pid = portfolios.save(session.conn, body.name, body.cash, resolved)
    session.set_portfolio(body.name, body.cash, resolved, pid)
    _spawn(_announce_portfolio())
    return {"ok": True, "id": pid, "holdings": len(resolved)}


@app.post("/portfolios/{portfolio_id}/activate")
async def activate_portfolio(portfolio_id: str) -> dict:
    data = portfolios.load(session.conn, portfolio_id)
    if not data:
        return {"ok": False, "reason": "not found"}
    session.set_portfolio(data["name"], data["cash"], data["positions"], portfolio_id)
    _spawn(_announce_portfolio())
    return {"ok": True, "name": data["name"], "holdings": len(data["positions"])}


@app.delete("/portfolios/{portfolio_id}")
async def remove_portfolio(portfolio_id: str) -> dict:
    return {"ok": portfolios.delete(session.conn, portfolio_id)}


class PasteIn(BaseModel):
    text: str


@app.post("/portfolios/parse")
async def parse_holdings(body: PasteIn) -> dict:
    """Read holdings out of a pasted broker export. Rejects are returned, not dropped."""
    positions, rejected = portfolios.parse_pasted(body.text)
    prices = session.prices if session else {}
    return {"positions": [{"ticker": t, "name": universe.name(t), "shares": n,
                           "price": prices.get(t), "value": (prices.get(t) or 0) * n,
                           "sector_label": universe.sector_label(universe.sector(t))}
                          for t, n in positions.items()],
            "rejected": rejected}


@app.get("/xray")
async def portfolio_xray(lang: str = "en") -> dict:
    if not session or not session.portfolio.positions:
        return {"empty": True}
    out = xray_mod.analyse(session.pit, session.portfolio, session.prices,
                           session.policy).as_dict()
    if lang == "hi" and out.get("findings"):
        # headline/detail in Hindi through the same exact-rule translator the answers use;
        # a line it cannot translate faithfully stays in English rather than risk a wrong figure
        fs = out["findings"]
        hs = await vernacular_mod.translate_many([f["headline"].rstrip(".") + "." for f in fs], "hi")   # the rule table keys on full sentences
        ds = await vernacular_mod.translate_many([f.get("detail") or "" for f in fs], "hi")
        for f, h, d in zip(fs, hs, ds):
            f["headline"], f["detail"] = h.rstrip("।."), d or f.get("detail")
    return out


@app.get("/stress")
async def stress_tests() -> dict:
    if not session or not session.portfolio.positions:
        return {"scenarios": [], "worst_case": None}
    return stress_mod.run_all(session.pit, session.portfolio, session.prices)


class AskIn(BaseModel):
    question: str
    level: str = "normal"        # normal | simple | maths
    lang: str | None = None
    cid: str | None = None
    route: str | None = None     # the page the person is on, so "this feature" means this page's tool


@app.post("/ask")
async def ask(body: AskIn) -> dict:
    """Plain-language Q&A. Numbers computed, words templated.

    `level` is the explain-back dial. The same question at "simple" returns the same
    truth with the qualifying clauses stripped; at "maths" it returns the workings.
    Re-asking at a different level is how somebody says "I did not follow that" without
    having to rephrase anything.
    """
    _req_lang.set(body.lang if body.lang in vernacular_mod.LANGS else None)
    understand_mod.REQ_ROUTE.set(body.route or "")
    question = body.question
    if hindi_input.has_devanagari(question):
        question = (await hindi_input.convert(question))[0] or question
    a = await _assist(question, body.level)
    loc = await _present(a)
    bus.emit(EventType.SPEECH, text=loc["spoken"], final=True, lang=_last_lang, answer=loc["answer"], cid=body.cid)
    return loc["answer"]


# --------------------------------------------------------------- the guidance layer
@app.get("/actions")
async def next_best_actions(limit: int = 6) -> dict:
    """What is worth doing next, ranked. The panel that means nobody has to be guided."""
    if not session:
        return {"actions": [], "count": 0}
    lots = portfolios.load_lots(session.conn, session.portfolio_id)
    return actions_mod.build(session.pit, session.portfolio, session.prices,
                             session.policy, lots, limit=limit)


@app.get("/flows")
async def list_flows(audience: str | None = None, lang: str = "en") -> dict:
    return {"flows": flows_mod.listing(audience, lang)}


@app.get("/flows/{flow_id}")
async def get_flow(flow_id: str, lang: str = "en") -> dict:
    flow = flows_mod.get(flow_id)
    return flows_mod.localize(flow.as_dict(), lang) if flow else {"error": f"no flow {flow_id!r}"}


@app.get("/palette")
async def palette_search(q: str = "", limit: int = 8) -> dict:
    """Every capability, searchable in the words a person would actually type."""
    return {"results": palette_mod.search(q, limit), "groups": palette_mod.groups()}


@app.get("/drilldown/{metric}")
async def drill(metric: str, key: str | None = None) -> dict:
    """What a number is made of, how it was worked out, and where it came from."""
    if not session or not session.portfolio.positions:
        return {"error": "no portfolio"}
    return drilldown_mod.explain_metric(metric, session.pit, session.portfolio,
                                        session.prices, session.policy, key)


@app.get("/report")
async def one_pager() -> dict:
    """The whole position on one flat page: printable, shareable, dateable."""
    if not session:
        return {"empty": True}
    lots = portfolios.load_lots(session.conn, session.portfolio_id)
    return report_mod.build(session.pit, session.portfolio, session.prices,
                            session.policy, session.portfolio_name, lots)


# --------------------------------------------------------------------- watchlist
class WatchIn(BaseModel):
    metric: str
    op: str = "above"
    threshold: float
    subject: str | None = None


@app.get("/watch")
async def get_watches() -> dict:
    if not session:
        return {"watches": [], "breached": [], "changed": []}
    return watchlist_mod.evaluate(session.conn, session.pit, session.portfolio,
                                  session.prices, session.policy,
                                  session.portfolio_id)


@app.get("/watch/metrics")
async def watch_metrics() -> dict:
    return {"metrics": watchlist_mod.METRICS, "ops": list(watchlist_mod.OPS)}


@app.post("/watch")
async def add_watch(body: WatchIn) -> dict:
    try:
        w = watchlist_mod.add(session.conn, session.portfolio_id, body.metric,
                              body.op, body.threshold, body.subject)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "watch": w.as_dict()}


@app.delete("/watch/{watch_id}")
async def delete_watch(watch_id: str) -> dict:
    return {"ok": watchlist_mod.remove(session.conn, watch_id)}


# ----------------------------------------------------------------------- sandbox
class StageIn(BaseModel):
    trades: list[dict] = []
    origin: str = "manual"
    note: str = ""


@app.get("/sandbox")
async def sandbox_state(stress: bool = True) -> dict:
    if not session:
        return {"staged": False, "trades": []}
    return sandbox_mod.preview(session.pit, session.portfolio, session.prices,
                               session.policy, staged, with_stress=stress)


@app.post("/sandbox/stage")
async def sandbox_stage(body: StageIn) -> dict:
    """Put trades into the staging layer and return the portfolio they would produce."""
    staged.trades = list(body.trades)
    staged.origin = body.origin
    staged.note = body.note
    result = sandbox_mod.preview(session.pit, session.portfolio, session.prices,
                                 session.policy, staged)
    bus.emit(EventType.SPEECH, text=sandbox_mod.spoken(result), final=True)
    return result


@app.post("/sandbox/from-rebalance")
async def sandbox_from_rebalance(deploy: bool = True) -> dict:
    """Stage the computed rebalance plan, so it can be inspected before committing."""
    if not session or not session.portfolio.positions:
        return {"staged": False, "trades": []}
    plan = rebalance_mod.plan(session.pit, session.portfolio, session.prices,
                              session.policy, deploy_cash=deploy).as_dict()
    staged.trades = [{"ticker": t["ticker"], "side": t["side"], "shares": t["shares"],
                      "reason": t.get("reason", "")} for t in plan["trades"]]
    staged.origin = "rebalance"
    staged.note = "The smallest set of trades that brings every limit back inside range."
    return sandbox_mod.preview(session.pit, session.portfolio, session.prices,
                               session.policy, staged)


@app.post("/sandbox/discard")
async def sandbox_discard() -> dict:
    staged.clear()
    return {"staged": False, "trades": []}


@app.post("/sandbox/commit")
async def sandbox_commit() -> dict:
    """Apply the staged trades for real -- through the same firewall a spoken order hits.

    A sandbox that could launder a breaching trade into the book would be a hole in the
    governance story rather than a feature, so each staged trade is re-tested here.
    """
    if not staged.trades:
        return {"ok": False, "reason": "nothing staged"}

    applied, refused = [], []
    for t in staged.trades:
        ticker, side = t.get("ticker"), str(t.get("side", "BUY")).upper()
        shares = int(t.get("shares") or 0)
        price = session.prices.get(ticker or "", 0.0)
        if not ticker or shares <= 0 or not price:
            refused.append({**t, "reason": "no price or zero size"})
            continue
        proposal = Proposal(ticker=ticker, side=side, shares=shares, price=price,
                            rationale=str(t.get("reason") or "staged in the sandbox"))
        decision = session.engine.evaluate(session.portfolio, proposal,
                                           session.prices, session.sectors)
        if not decision.approved:
            refused.append({**t, "reason": decision.violations[0].message
                            if decision.violations else "blocked by the risk firewall"})
            session.counters.violations_blocked += 1
            continue
        session.portfolio = session.portfolio.apply(
            ticker, side, shares, price, session.policy.execution.cost_bps)
        session.counters.trades_executed += 1
        applied.append({**t, "price": price, "value": shares * price})
        ledger_mod.record(session.conn, sim_clock=session.pit.clock_iso, kind="TRADE",
                          ticker=ticker, side=side, shares=shares, price=price,
                          cost=shares * price * session.policy.execution.cost,
                          nav_after=session.nav(), reason=str(t.get("reason") or "sandbox commit"),
                          policy=session.policy.profile)

    staged.clear()
    emit_telemetry(session, bus)
    _spawn(_announce_portfolio())
    return {"ok": True, "applied": applied, "refused": refused,
            "applied_count": len(applied), "refused_count": len(refused)}


@app.get("/glossary")
async def glossary(lang: str = "en") -> dict:
    if lang == "hi":
        from backend.glossary_hi import GLOSSARY_HI, TERM_NAME_HI
        return {"terms": {TERM_NAME_HI.get(k, k): GLOSSARY_HI.get(k, v) for k, v in explain.GLOSSARY.items()}}
    return {"terms": explain.GLOSSARY}


class TranslateIn(BaseModel):
    lines: list[str]


@app.post("/translate")
async def translate_lines(body: TranslateIn) -> dict:
    """Hindi for backend-written sentences shown on the pages. Exact rules first, then the
    guarded model fallback; a line that cannot be translated faithfully comes back unchanged."""
    lines = [str(x)[:600] for x in body.lines[:60]]
    names = {universe.name(t) for t in universe.tickers()}
    todo = [i for i, x in enumerate(lines) if x not in names]        # company names stay as written
    done = await vernacular_mod.translate_many([lines[i] for i in todo], "hi") if todo else []
    out = list(lines)
    for i, hi in zip(todo, done):
        out[i] = hi
    return {"hindi": out}


@app.get("/screen/{kind}")
async def screener(kind: str, limit: int = 8) -> dict:
    """Rank the universe. The analyst's screening desk, done in arithmetic."""
    if not session:
        return {"results": []}
    held = session.portfolio.weights(session.prices)
    return {"kind": kind, "label": factors_mod.SCREENS.get(kind, kind),
            "screens": factors_mod.SCREENS,
            "results": factors_mod.screen(session.pit, kind, held, limit)}


@app.get("/correlation")
async def correlation() -> dict:
    """Which of your holdings are secretly the same bet."""
    if not session or len(session.portfolio.positions) < 2:
        return {"pairs": [], "matrix": {}}
    held = list(session.portfolio.positions)
    matrix = factors_mod.correlation_matrix(session.pit, held)
    weights = session.portfolio.weights(session.prices)
    return {"matrix": matrix,
            "names": {t: universe.name(t) for t in held},
            "pairs": factors_mod.correlated_pairs(matrix, weights),
            "avg_correlation": factors_mod.diversification_ratio(matrix, weights)}


@app.get("/rebalance")
async def rebalance(deploy: bool = True) -> dict:
    """The portfolio manager's construction job: minimal trades to compliance.

    Annotated with the tax it would cost, where cost basis is known. A plan that costs
    more in tax than it saves in risk is a bad plan, and nobody finds that out until
    the bill arrives.
    """
    if not session or not session.portfolio.positions:
        return {"trades": [], "trade_count": 0}
    plan = rebalance_mod.plan(session.pit, session.portfolio, session.prices,
                              session.policy, deploy_cash=deploy).as_dict()
    lots = portfolios.load_lots(session.conn, session.portfolio_id)
    return tax_mod.annotate_plan(plan, lots)


class LotIn(BaseModel):
    ticker: str
    shares: int
    buy_price: float
    buy_date: str


@app.get("/lots")
async def get_lots() -> dict:
    lots = portfolios.load_lots(session.conn, session.portfolio_id)
    held = session.portfolio.positions
    return {"lots": [{"ticker": t, "name": universe.name(t), "shares": l.shares,
                      "buy_price": l.buy_price, "buy_date": l.buy_date}
                     for t, l in lots.items()],
            "missing": [{"ticker": t, "name": universe.name(t), "shares": n,
                         "price": session.prices.get(t)}
                        for t, n in held.items() if t not in lots]}


@app.post("/lots")
async def add_lot(body: LotIn) -> dict:
    ticker = universe.resolve(body.ticker)
    if not ticker or not session.portfolio_id:
        return {"ok": False, "reason": "unknown ticker or no active portfolio"}
    portfolios.save_lot(session.conn, session.portfolio_id, ticker,
                        body.shares, body.buy_price, body.buy_date)
    return {"ok": True}


@app.get("/history")
async def portfolio_history(days: int = 250) -> dict:
    """Daily value of the current basket, with the benchmark rebased alongside it.

    Share counts are held constant, so this is what the BASKET did -- not a record of
    what was earned. Rebasing the benchmark to the same starting value is the only way
    the two lines are comparable on one axis.
    """
    if not session or not session.portfolio.positions:
        return {"series": [], "benchmark": []}
    series = xray_mod.portfolio_series(session.pit, session.portfolio, days=days)
    if not series:
        return {"series": [], "benchmark": []}

    by_date = dict(series)
    bench_bars = [b for b in session.pit.prices(universe.benchmark(), limit=days + 5)
                  if b["close"] and b["date"] in by_date]
    bench = []
    if bench_bars:
        base_p = series[0][1]
        base_b = bench_bars[0]["close"]
        bench = [{"date": b["date"], "value": round(base_p * b["close"] / base_b, 2)}
                 for b in bench_bars]
    return {
        "series": [{"date": d, "value": round(v, 2)} for d, v in series],
        "benchmark": bench,
        "benchmark_label": universe.benchmark_label(),
        "caveat": "Today's share counts valued back through history — what this basket "
                  "would have done, not what you earned.",
    }


@app.post("/rebalance/apply")
async def rebalance_apply(deploy: bool = True) -> dict:
    """Book the whole plan to the paper ledger, re-gating every leg on the way.

    Human-in-the-loop is preserved: the UI only calls this after an explicit click,
    and each trade is still evaluated by the firewall rather than trusted from the plan.
    """
    from risk.portfolio import Proposal
    if session.policy.governance.broker_execution:
        return {"ok": False, "reason": "broker execution disabled"}
    p = rebalance_mod.plan(session.pit, session.portfolio, session.prices,
                           session.policy, deploy_cash=deploy)
    engine = RiskEngine(session.policy)
    booked, refused = [], []
    for t in p.trades:
        proposal = Proposal(ticker=t.ticker, side=t.side, shares=t.shares, price=t.price)
        decision = engine.evaluate(session.portfolio, proposal, session.prices,
                                   session.sectors)
        if decision.approved:
            session.portfolio = session.portfolio.apply(
                t.ticker, t.side, t.shares, t.price, session.policy.execution.cost_bps)
            booked.append(t.as_dict())
            ledger_mod.record(session.conn, sim_clock=session.pit.clock_iso, kind="TRADE",
                              ticker=t.ticker, side=t.side, shares=t.shares, price=t.price,
                              cost=t.shares * t.price * session.policy.execution.cost,
                              nav_after=session.nav(), reason="rebalance plan",
                              policy=session.policy.profile)
        else:
            refused.append({**t.as_dict(),
                            "why": decision.violations[0].code if decision.violations else "?"})
    session.counters.trades_executed += len(booked)
    bus.emit(EventType.EXECUTION, ticker="REBALANCE", side="PLAN",
             shares=len(booked), price=0.0, nav_after=session.nav(), paper=True)
    _spawn(_announce_portfolio())
    return {"ok": True, "booked": booked, "refused": refused,
            "nav_after": session.nav()}


@app.get("/attribution")
async def attribution(window: str = "3m") -> dict:
    """Where the money came from: per holding, per sector, allocation vs selection."""
    if not session or not session.portfolio.positions:
        return {"contributions": []}
    a = attrib_mod.analyse(session.pit, session.portfolio, session.prices, window)
    return {**a.as_dict(), "summary": attrib_mod.summary_line(a)}


@app.get("/calibration")
async def calibration() -> dict:
    """Per-desk hit rate and Brier score against realised forward returns."""
    if not session:
        return {"desks": [], "total_scored": 0}
    return score(session.conn)


@app.get("/recordings")
async def recordings() -> dict:
    return {"takes": list_takes(),
            "playing": player.name if player and player.active else None,
            "recording": recorder.name if recorder else None}


@app.post("/record/start")
async def record_start(name: str = "demo") -> dict:
    global recorder
    if recorder:
        recorder.stop()
    recorder = Recorder(bus=bus, name=name, session=session)
    recorder.start()
    return {"ok": True, "recording": name}


@app.post("/record/stop")
async def record_stop() -> dict:
    global recorder
    if not recorder:
        return {"ok": False, "reason": "not recording"}
    path = recorder.stop()
    name, rows = recorder.name, len(recorder.rows)
    recorder = None
    return {"ok": True, "name": name, "rows": rows, "path": str(path)}


@app.post("/replay/start")
async def replay_start(name: str = "demo", speed: float = 1.0) -> dict:
    """Play a recorded take. Needs no Ollama, no mic, and no snapshot database."""
    global player
    path = RECORDINGS / f"{name}.jsonl"
    if not path.exists():
        return {"ok": False, "reason": f"no take named {name}"}
    if player and player.active:
        player.stop()
    # Mirror recorded clock changes onto the live session so /prices follows the take,
    # and so the session is left where the recording ended rather than snapping back to
    # today the moment the heartbeat resumes.
    def on_clock(iso: str) -> None:
        if session:
            try:
                session.set_clock(iso)
            except Exception:  # noqa: BLE001
                pass

    player = Player(bus=bus, path=path, speed=speed, on_clock=on_clock)
    bus.emit(EventType.TELEMETRY, replay=True, replay_name=name, tick=0)

    def finished() -> None:
        bus.emit(EventType.TELEMETRY, replay=False, tick=0)
        # One authoritative telemetry tick so the HUD converges on the live session
        # instead of leaving the scrubber and the fund panel disagreeing.
        if session:
            emit_telemetry(session, bus)

    player.start(on_finish=finished)
    return {"ok": True, "replaying": name, "speed": speed}


@app.post("/replay/stop")
async def replay_stop() -> dict:
    global player
    if player:
        player.stop()
        bus.emit(EventType.TELEMETRY, replay=False, tick=0)
    player = None
    return {"ok": True}


@app.get("/prices/{ticker}")
async def prices(ticker: str, limit: int = 250) -> dict:
    """OHLC for the chart, point-in-time filtered like everything else."""
    if not session:
        return {"bars": []}
    return {"ticker": ticker, "sim_clock": session.pit.clock_iso,
            "bars": session.pit.prices(ticker, limit=limit)}


@app.get("/evidence/{ticker}")
async def evidence(ticker: str) -> dict:
    """The pack behind the provenance graph -- click a node, read the source."""
    from agents.evidence import build_pack
    if not session:
        return {"items": []}
    pack = build_pack(session.pit, ticker)
    return {"ticker": ticker, "sim_clock": pack.sim_clock,
            "items": [i.__dict__ for i in pack.items]}


_last_full = ""
_last_lang = "en"
_MORE = re.compile(r"^\s*(tell me more|more( detail)?|go on|continue|read (it|that) (all|out)|full answer)\W*$", re.I)


async def dispatch(text: str, lang: str | None = None, cid: str | None = None, route: str | None = None) -> dict:
    """The single entry point for anything typed or spoken.

    Decides command vs question ONCE, here, so the two paths cannot diverge. They used
    to: the command bar ran an imperative parser that turned "what should I sell" into
    a trade proposal and "what if the market drops 20%" into a policy change.
    """
    _req_lang.set(lang if lang in vernacular_mod.LANGS else None)
    understand_mod.REQ_ROUTE.set(route or "")
    orig, ckey = text, (cid or "default")
    # The guide goes first: an open question takes this line as its answer, and "open X" moves the page.
    if (ga := guide_mod.intercept(text, _lang(), ckey)) is not None:
        loc = await _present(ga)
        bus.emit(EventType.SPEECH, text=loc["spoken"], final=True, lang=_last_lang, answer=loc["answer"], cid=cid)
        return {"accepted": True, "kind": "guide", "answer": loc["answer"]}
    if hindi_input.has_devanagari(text):
        # Hindi in, by voice or keyboard: figures and intent are read in code (no free translation,
        # which used to invent words). Anything it cannot place is passed on as is, and the
        # assistant asks in Hindi.
        english, _how = await hindi_input.convert(text)
        if english:
            text = english
    if _MORE.match(text or "") and _last_full:
        # "tell me more": read the long version of the last answer. Answers are spoken
        # short by default; this is how the person asks for the rest.
        bus.emit(EventType.SPEECH, text=_last_full, final=True, lang=_last_lang, cid=cid)
        return {"accepted": True, "kind": "more"}
    decision = route_input(text)
    # "analyse TCS" in the chat is a request for the card (facts + chart), not for the desks to start running.
    if decision.kind == "command" and decision.intent and not understand_mod.wants_card(text):
        _spawn(handle(session, bus, decision.intent))
        return {"accepted": True, "kind": "command", "why": decision.why,
                "intent": decision.intent.as_payload()}

    # Questions are answered, never acted on. `convo` carries the last subject so a
    # follow-up like "and what does it move with" resolves without naming the company
    # again -- which is how people actually talk, and especially how they speak.
    answer = await _assist(text)
    if answer.kind in guide_mod.KIND_TO_TOOL and not answer.data.get("guide"):
        tool = guide_mod.KIND_TO_TOOL[answer.kind]
        # Read what the sentence gave; a tool that guessed defaults for the rest (a fee question with no
        # amount) still asks, because a guessed figure is not the person's figure.
        g = guide_mod.begin(tool, text, _lang(), ckey)
        if g["done"]:
            answer.data["guide"] = g                     # the tool had everything: open its page with the result showing
        else:
            answer = guide_mod.to_answer(g, _lang())     # ask for what is missing, one question at a time
    bus.emit(EventType.INTENT, verb="ask", ticker=answer.subject,
             args={"question": text}, via="router")
    loc = await _present(answer)
    bus.emit(EventType.SPEECH, text=loc["spoken"], final=True, lang=_last_lang,
             answer=loc["answer"], cid=cid)
    emit_telemetry(session, bus)
    return {"accepted": True, "kind": "question", "why": decision.why,
            "answer": loc["answer"]}


def _check_watches() -> None:
    """Re-evaluate standing rules and announce only the ones that just flipped.

    Announcing every breach on every tick trains people to ignore the channel, so only
    transitions reach the bus. Failures here are swallowed: a broken rule must never
    take down the command that triggered the check.
    """
    if not session or not session.portfolio.positions:
        return
    try:
        result = watchlist_mod.evaluate(session.conn, session.pit, session.portfolio,
                                        session.prices, session.policy,
                                        session.portfolio_id)
    except Exception:  # noqa: BLE001
        return
    if not result.get("changed"):
        return
    bus.emit(EventType.TELEMETRY, watches=result["watches"],
             breached=len(result["breached"]), tick=session.counters.tick)
    if (line := watchlist_mod.spoken(result["changed"])):
        bus.emit(EventType.SPEECH, text=line, final=True,
                 answer={"headline": line, "kind": "watch",
                         "bullets": [w["label"] for w in result["changed"][:2]],
                         "follow_ups": ["Why is my risk high?", "What should I sell?"]})


@app.post("/command")
async def command(cmd: Command) -> dict:
    if player and player.active:
        return {"accepted": False, "reason": "replay in progress"}
    return await dispatch(cmd.text, cmd.lang, cmd.cid)


@app.post("/stt")
async def speech_to_text(request: Request, lang: str = "en", raw: bool = False, cid: str | None = None,
                         understand: bool = False) -> dict:
    """Push-to-talk audio in, dispatched command out.

    The browser posts whatever MediaRecorder produced; faster-whisper decodes it
    locally. Nothing leaves the machine -- which is the entire reason we are not using
    the browser Web Speech API, which is free and easy and ships audio to Google.
    """
    audio = await request.body()
    if len(audio) < 2000:
        # A key-tap with no speech. Silently ignoring it beats transcribing noise
        # into a command that then executes.
        return {"ok": False, "reason": "too short", "bytes": len(audio)}

    if not stt.available():
        bus.emit(EventType.ERROR, where="stt", message="faster-whisper not installed")
        return {"ok": False, "reason": "stt unavailable"}

    try:
        result = await asyncio.to_thread(stt.transcribe, audio, lang)
    except Exception as exc:  # noqa: BLE001
        bus.emit(EventType.ERROR, where="stt", message=f"{type(exc).__name__}: {exc}"[:160])
        return {"ok": False, "reason": "transcription failed"}

    if raw:
        # Transcribe only: no translation, no command dispatch. Used where the words themselves
        # are the input (the scam-call rehearsal replies in the language that was spoken).
        text = result.text.strip()
        return {"ok": bool(text) and result.confidence >= 0.2, "transcript": text,
                "confidence": result.confidence, "reason": None if text else "no speech"}

    if lang != "en" and result.text.strip():
        # Spoken Hindi: keep exactly what was heard; dispatch() reads its meaning (hindi_input).
        result.raw = result.text
    if understand:
        # Say what was heard and understood BEFORE answering, so a mishearing can be corrected
        # instead of silently answered. The client sends it on (edited or not) when confirmed.
        text = result.text.strip()
        english, how = (await hindi_input.convert(text)) if hindi_input.has_devanagari(text) else (None, "english")
        info = hindi_input.describe(text, english) if english else {"intent": None, "label": None, "figures": []}
        return {"ok": bool(text), "confirm": True, "transcript": text, "understood": english, "how": how,
                "confidence": result.confidence, "model": result.model, **info}
    bus.emit(EventType.TRANSCRIPT, text=result.text, final=True, raw=result.raw,
             confidence=result.confidence, ms=result.duration_ms,
             repaired=result.repaired, model=result.model)

    # Below this, the decoder was guessing. Show what it heard and let the operator
    # retry rather than acting on it -- a misheard "sell" is not a recoverable mistake.
    #
    # The bar depends on what was heard. A COMMAND can propose a trade, so it keeps the
    # strict threshold. A QUESTION cannot do anything but answer, so a misheard one costs
    # a wrong answer the person can see and repeat -- rejecting it outright just made
    # short, correctly-transcribed questions ("analyse TCS" scored 0.57) feel broken.
    heard_kind = route_input(result.text).kind if result.text.strip() else "question"
    floor = 0.45 if heard_kind == "command" else 0.30
    if result.confidence < floor or not result.text.strip():
        bus.emit(EventType.ERROR, where="stt",
                 message=f"Low confidence ({result.confidence:.2f}) - not dispatched.")
        return {"ok": False, "reason": "low confidence", "transcript": result.text,
                "confidence": result.confidence}

    # Same router as typed input. Voice used to go through the imperative parser only,
    # so a perfectly transcribed question still produced the wrong action.
    out = await dispatch(result.text, lang, cid)
    return {"ok": True, "transcript": result.text, "raw": result.raw,
            "confidence": result.confidence, "ms": result.duration_ms,
            "repaired": result.repaired, **out}


async def _announce_portfolio() -> None:
    """Tell the HUD, in words, what just loaded and what is wrong with it."""
    from backend.pipeline import emit_telemetry, say
    report = xray_mod.analyse(session.pit, session.portfolio, session.prices,
                              session.policy)
    a = explain._xray_answer(report)
    a.follow_ups = explain.follow_ups_for(a, report)
    bus.emit(EventType.CLOCK, sim_clock=session.pit.clock_iso, live=False)
    bus.emit(EventType.TELEMETRY, xray=report.as_dict(), tick=0)
    say(bus, a.spoken())
    emit_telemetry(session, bus, orb="alert" if report.score < 55 else "idle")
    # A new book means new standing rules to test, and a different action list.
    _check_watches()


@app.post("/boot")
async def replay_boot() -> dict:
    _spawn(boot(session, bus))
    return {"ok": True}


def _seed_sample_lots() -> None:
    """Example purchase records for the bundled example portfolio ONLY.

    Cost basis is something the owner has to supply, and the product never guesses it for a
    real portfolio. The bundled "Typical Indian retail" book is itself an example, so it
    ships with example purchase dates -- spread so the tax page has short-term, near-the-
    threshold, long-term and loss cases to show. A lot the user already entered is kept.
    """
    pid = "preset_typical_retail"
    have = portfolios.load_lots(session.conn, pid)
    from datetime import datetime, timedelta
    today = datetime.fromisoformat(session.pit.clock_iso[:10])
    plan = {  # ticker: (days ago bought, price vs today)
        "TCS.NS": (345, 0.93), "WIPRO.NS": (351, 0.88), "SBIN.NS": (320, 1.05),
        "INFY.NS": (210, 0.97), "ICICIBANK.NS": (140, 1.08), "RELIANCE.NS": (640, 0.80),
        "ITC.NS": (900, 0.70), "HCLTECH.NS": (95, 0.99),
    }
    for t, (days, factor) in plan.items():
        px = session.prices.get(t)
        if t in have and have[t].buy_price not in (1500.0, 450.0) or not px:
            continue
        portfolios.save_lot(session.conn, pid, t, 0, round(px * factor, 2),
                            (today - timedelta(days=days)).strftime("%Y-%m-%d"))


class FirewallIn(BaseModel):
    ticker: str
    side: str = "BUY"
    shares: int


@app.post("/firewall/check")
async def firewall_check(body: FirewallIn) -> dict:
    """Run a proposed trade through the risk firewall WITHOUT touching the book."""
    ticker = universe.resolve(body.ticker) or body.ticker
    price = session.prices.get(ticker, 0.0)
    if not price:
        return {"ok": False, "reason": f"No price for {body.ticker}."}
    prop = Proposal(ticker=ticker, side=body.side.upper(), shares=max(0, int(body.shares)),
                    price=price, rationale="firewall check")
    d = session.engine.evaluate(session.portfolio, prop, session.prices, session.sectors)
    return {"ok": True, "ticker": ticker, "name": universe.name(ticker), "price": price,
            "side": prop.side, "shares": prop.shares, "value": prop.shares * price,
            "approved": d.approved, "violations": [v.model_dump() for v in d.violations],
            "remedy": d.remedy.model_dump() if d.remedy else None,
            "policy": session.policy.describe()}


LANG = "en"                      # last language a screen chose (used for announcements nobody asked for)
_req_lang: contextvars.ContextVar = contextvars.ContextVar("req_lang", default=None)


def _lang() -> str:
    """The language of the request being served. Each screen sends its own, so two tabs (or a
    tab and a phone) can use different languages without overriding each other."""
    return _req_lang.get() or LANG
HI_VOICE = "hi_IN-pratham-medium"


class LangIn(BaseModel):
    lang: str


@app.get("/language")
async def get_language() -> dict:
    return {"lang": LANG, "languages": vernacular_mod.LANGS,
            "hindi_voice": tts_mod.available(None, "hi")}


@app.post("/language")
async def set_language(body: LangIn) -> dict:
    """Switch the language answers are explained in. Numbers never come from the
    translation step -- see backend/vernacular.py."""
    global LANG
    if body.lang in vernacular_mod.LANGS:
        LANG = body.lang
    return {"lang": LANG}


def _guide_ctx(lang: str):
    from backend import assistant
    return assistant.Ctx(pit=session.pit, portfolio=session.portfolio, prices=session.prices,
                         policy=session.policy, conn=session.conn, convo=None, lang=lang)


guide_mod.ctx_factory = _guide_ctx
from backend import messaging as messaging_mod
messaging_mod.configure(_guide_ctx, lambda: session.conn)


async def _assist(question: str, level: str = "normal"):
    """Every question goes through the assistant: tool-backed answers for funds, fees,
    emergency cash, goals, scams and so on; the original explainer for portfolio questions."""
    from backend import assistant
    ctx = assistant.Ctx(pit=session.pit, portfolio=session.portfolio, prices=session.prices,
                        policy=session.policy, conn=session.conn, convo=convo, lang=_lang(), level=level)
    return await assistant.aanswer(question, ctx)


async def _present(answer):
    """The answer as it should be shown and spoken in the current language."""
    global _last_full, _last_lang
    loc = await vernacular_mod.localize_answer(answer, _lang())
    _last_full = loc["full"]
    _last_lang = "hi" if loc["translated"] else "en"
    return loc


def _speech_filter(payload: dict) -> dict | None:
    """Keep spoken language consistent with the chosen language.

    Lines that already carry a `lang` (answers localised in _present) pass straight
    through. Everything else -- portfolio announcements, trade-check messages -- is
    translated in Hindi mode: exact rules are instant; anything else goes to the model in
    the background and is spoken when it arrives (never as English over Hindi text).
    """
    if LANG != "hi" or payload.get("lang") or not payload.get("text"):
        return payload
    from backend.hindi_rules import exact
    text = str(payload["text"])
    sents = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]
    done = [exact(x) for x in sents]
    if sents and all(d is not None for d in done):
        return {**payload, "text": " ".join(done), "lang": "hi"}

    async def later() -> None:
        hi = (await vernacular_mod.translate_many([text], "hi"))[0]
        # A failed translation stays silent: English over Hindi text is the bug being fixed.
        if vernacular_mod.looks_hindi(hi):
            bus.emit(EventType.SPEECH, **{**payload, "text": hi, "lang": "hi"})
    try:
        _spawn(later())
    except RuntimeError:
        pass
    return None


bus.speech_filter = _speech_filter

class TTSIn(BaseModel):
    text: str
    voice: str | None = None
    lang: str = "en"


@app.post("/tts")
async def tts_speak(body: TTSIn) -> Response:
    """One chunk of speech in, one WAV out. The browser plays it through an <audio> element
    it controls, which is what makes interruption instant (see backend/tts.py)."""
    text = body.text.strip()[:700]
    if not text or not tts_mod.available(body.voice, body.lang):
        return Response(status_code=204)
    try:
        wav = await asyncio.to_thread(tts_mod.synthesize, text, body.voice, body.lang)
    except Exception:  # noqa: BLE001
        return Response(status_code=204)      # the browser falls back rather than going mute
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.get("/tts/status")
async def tts_status() -> dict:
    return {"available": tts_mod.available(), "defaults": tts_mod.DEFAULTS,
            "voices": tts_mod.catalogue()}


class ScanIn(BaseModel):
    text: str


@app.post("/scan")
async def scan_tip(body: ScanIn) -> dict:
    """Check a pasted stock tip against dated data on file. Deterministic -- no model."""
    from analysis import tipminds
    return await tipminds.analyse(session.pit, body.text)


@app.get("/tax/shield")
async def tax_shield() -> dict:
    from datetime import datetime
    if not session or not session.portfolio.positions:
        return {"rows": [], "total_saving": 0.0}
    lots = portfolios.load_lots(session.conn, session.portfolio_id)
    asof = datetime.fromisoformat(session.pit.clock_iso[:10]).date()
    out = shield_mod.shield(session.portfolio, session.prices, lots, asof)
    out["sample"] = bool(session.portfolio_id and session.portfolio_id.startswith("preset_"))
    return out


@app.get("/funds")
async def funds() -> dict:
    from backend import practice
    return {"funds": practice.fund_list()}


@app.get("/funds/overlap")
async def funds_overlap(a: str, b: str) -> dict:
    from backend import practice
    from core import universe
    if a not in practice.FUNDS or b not in practice.FUNDS:
        raise HTTPException(404, "unknown fund")
    own = {t: sh * session.prices.get(t, 0.0) for t, sh in session.portfolio.positions.items()}
    return practice.overlap(a, b, universe.name, own)


@app.get("/scamcall/scenarios")
async def scam_scenarios(lang: str = "en") -> dict:
    from backend import practice
    return {"scenarios": practice.scenario_list(lang)}


class ScamIn(BaseModel):
    scenario: str
    node: int = 0
    reply: str | None = None
    pressure: int = 0
    lang: str = "en"


@app.post("/scamcall/step")
async def scam_step(body: ScamIn) -> dict:
    from backend import practice
    if body.scenario not in practice.SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    return practice.scam_step(body.scenario, body.node, body.reply, body.pressure, body.lang)


@app.get("/feedrag")
async def feedrag(principal: float = 100000, monthly: float = 0, years: int = 20, gross: float = 12.0,
                  fee: float = 2.0, low: float = 0.2) -> dict:
    from analysis import tools
    return tools.fee_drag(max(0, principal), max(0, monthly), years, gross, fee, low)


@app.get("/emergency")
async def emergency(cash: float = 300000, expenses: float = 40000, invest: float = 0, income: float = 0,
                    haircut: float = 20) -> dict:
    from analysis import tools
    return tools.emergency(max(0, cash), expenses, max(0, invest), haircut, max(0, income))


@app.get("/digest")
async def weekly_digest(lang: str = "en") -> dict:
    import datetime
    from backend import digest
    return digest.build(session.pit, session.portfolio, session.prices, session.policy, lang,
                        datetime.date.today().isocalendar()[1])


class MyFundIn(BaseModel):
    fund_id: str
    on: bool = True


@app.get("/myfunds")
async def get_my_funds() -> dict:
    from backend import assistant
    return {"funds": assistant.my_funds(session.conn)}


@app.post("/myfunds")
async def set_my_funds(body: MyFundIn) -> dict:
    from backend import assistant, practice
    if body.fund_id not in practice.FUNDS:
        raise HTTPException(404, "unknown fund")
    return {"funds": assistant.set_my_fund(session.conn, body.fund_id, body.on)}


@app.get("/recovery/types")
async def recovery_types(lang: str = "en") -> dict:
    from backend import recovery
    return {"types": recovery.types(lang)}


@app.get("/recovery/plan")
async def recovery_plan(type: str = "upi_card", lang: str = "en") -> dict:
    from backend import recovery
    return recovery.plan(type, lang)


class DraftIn(BaseModel):
    type: str = "upi_card"
    lang: str = "en"
    amount: float | None = None
    when: str = ""
    txn_id: str = ""
    fraud_contact: str = ""
    bank: str = ""
    name: str = ""


@app.post("/recovery/draft")
async def recovery_draft(body: DraftIn) -> dict:
    from backend import recovery
    return recovery.drafts(body.type, body.lang, body.amount, body.when, body.txn_id,
                           body.fraud_contact, body.bank, body.name)


class LoanIn(BaseModel):
    principal: float
    rate: float
    unit: str = "per100_month"
    months: int = 12
    mode: str = "interest_only"
    lang: str = "en"


@app.post("/twilio/webhook")
async def twilio_webhook(request: Request, format: str = "xml"):
    """Twilio calls this for every incoming WhatsApp message or SMS. Point the number's webhook here.
    `?format=json` returns the replies as JSON (the browser simulator and tests use that)."""
    from fastapi.responses import JSONResponse, Response
    form = {k: str(v) for k, v in (await request.form()).items()}
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if os.environ.get("JARVIS_TWILIO_VALIDATE") == "1":
        base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        url = (base + request.url.path) if base else str(request.url)
        if not (token and messaging_mod.valid_signature(url, form, request.headers.get("X-Twilio-Signature", ""), token)):
            return Response("invalid signature", status_code=403)
    sender, body = form.get("From", ""), form.get("Body", "")
    audio = None
    if int(form.get("NumMedia", "0") or 0) > 0 and form.get("MediaContentType0", "").startswith("audio"):
        audio = await messaging_mod.fetch_media(form.get("MediaUrl0", ""))
        if audio is None and not body:
            body = ""
    if not sender:
        return Response("missing From", status_code=400)
    if format == "json" and request.query_params.get("base", "").startswith(("http://", "https://")):
        messaging_mod.BASE.set(request.query_params["base"])      # the in-app chat: links open its own pages
    replies = await messaging_mod.handle(sender, body, messaging_mod.channel_of(sender), audio)
    if format == "json":
        return JSONResponse({"messages": replies, "channel": messaging_mod.channel_of(sender)})
    return Response(messaging_mod.twiml(replies), media_type="application/xml")


@app.post("/twilio/status")
async def twilio_status() -> dict:
    return {"ok": True}                        # delivery receipts: nothing is stored


class BridgeIn(BaseModel):
    jid: str                   # the Baileys JID, e.g. "919876543210@s.whatsapp.net"
    body: str = ""
    audio_b64: str | None = None
    bridge_url: str | None = None   # this bridge instance's own base URL -- see bridge_whatsapp()


@app.post("/bridge/whatsapp")
async def bridge_whatsapp(payload: BridgeIn) -> dict:
    """Inbound from a local WhatsApp bridge (whatsapp-bridge/, Baileys) -- unlike /twilio/webhook
    this is never reachable from the public internet, only from bridge containers on the
    compose-internal network, so there is no signature check to do here. JSON in, JSON out.

    One backend can serve several linked numbers at once, each its own bridge instance/process.
    A message that needs the local model gets an immediate acknowledgement in the response; the
    real answer is posted back once it is ready to the bridge's own /send -- specifically the
    bridge named in `payload.bridge_url`, which each bridge sends on every request, not a single
    fixed address. Get this wrong and a deferred reply for number A gets delivered through
    number B's connection instead -- wrong sender, wrong contact list, silently broken."""
    import base64
    import httpx

    sender = "whatsapp:+" + payload.jid.split("@", 1)[0].lstrip("+")
    audio = base64.b64decode(payload.audio_b64) if payload.audio_b64 else None
    bridge_url = (payload.bridge_url or os.environ.get("WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge:3000")).rstrip("/")

    async def _defer(replies: list[str]) -> None:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                for text in replies:
                    await c.post(f"{bridge_url}/send", json={"jid": payload.jid, "text": text})
        except Exception as exc:  # noqa: BLE001
            bus.emit(EventType.ERROR, where="bridge_whatsapp", message=f"{type(exc).__name__}: {exc}"[:160])

    replies = await messaging_mod.handle(sender, payload.body, "whatsapp", audio, defer=_defer)
    return {"messages": replies}


_SMS_SEEN: dict[str, float] = {}       # messageId -> first-seen time, for webhook-retry dedupe
_SMS_SEEN_TTL = 300.0


@app.post("/bridge/sms")
async def bridge_sms(payload: dict) -> dict:
    """Inbound from a gateway phone running SMS Gateway for Android in local/private mode --
    same trust boundary as /bridge/whatsapp (LAN-only, no public exposure, no signature check).

    No Node process here, unlike the WhatsApp bridge: the gateway app's own webhook/REST API is
    already stateless, so this lives entirely in the backend. One real trade-off from that -- see
    the implementation plan -- is that if THIS process is down, nothing can send an apology the
    way the WhatsApp bridge's independent process can; the webhook POST just fails outright."""
    import time

    from backend import sms_gateway

    body = payload.get("payload", payload)            # tolerate the raw event or its payload directly
    message_id = body.get("messageId")
    sender = body.get("phoneNumber", "")
    text = body.get("message", "")
    if not sender:
        return {"ok": False, "reason": "missing phoneNumber"}

    now = time.time()
    for k in [k for k, t in _SMS_SEEN.items() if now - t > _SMS_SEEN_TTL]:
        _SMS_SEEN.pop(k, None)
    if message_id:
        if message_id in _SMS_SEEN:
            return {"ok": True, "deduped": True}       # a webhook retry of one we already answered
        _SMS_SEEN[message_id] = now

    async def _defer(replies: list[str]) -> None:
        try:
            for reply in replies:
                result = await sms_gateway.send(sender, reply)
                if not result.get("ok"):
                    # send() reports failure (unconfigured, unreachable, bad creds) by returning
                    # {"ok": False, ...} rather than raising -- catching only exceptions here
                    # would miss exactly the failure modes most likely in practice.
                    bus.emit(EventType.ERROR, where="bridge_sms", message=f"send failed: {result}"[:160])
        except Exception as exc:  # noqa: BLE001
            bus.emit(EventType.ERROR, where="bridge_sms", message=f"{type(exc).__name__}: {exc}"[:160])

    replies = await messaging_mod.handle(sender, text, "sms", defer=_defer)
    return {"ok": True, "messages": replies}


@app.get("/messaging/status")
async def messaging_status() -> dict:
    return messaging_mod.status()


@app.post("/messaging/sim/voice")
async def messaging_sim_voice(request: Request, sender: str = "whatsapp:+910000000000", base: str = "") -> dict:
    """A voice note from the in-app chat: same path as a real WhatsApp voice note (transcribed locally,
    shown back as "I heard ..." before it is answered)."""
    audio = await request.body()
    if len(audio) < 2000:
        return {"messages": ["That voice note was too short. Hold the mic a little longer."]}
    if base.startswith(("http://", "https://")):
        messaging_mod.BASE.set(base)
    return {"messages": await messaging_mod.handle(sender, "", messaging_mod.channel_of(sender), audio)}


@app.get("/messaging/sim")
async def messaging_sim():
    from fastapi.responses import HTMLResponse
    from backend.messaging_sim import PAGE
    return HTMLResponse(PAGE)


class KioskResetIn(BaseModel):
    cid: str | None = None


@app.post("/kiosk/reset")
async def kiosk_reset(b: KioskResetIn) -> dict:
    """Shared device, next person: forget the guide's open question and the previous person's results."""
    global convo
    guide_mod.forget(b.cid or "default")
    convo = explain.Conversation()
    return {"ok": True}


class PolicyIn(BaseModel):
    premium: float
    pay_years: int
    term_years: int
    maturity: float
    sum_assured: float | None = None
    term_quote: float | None = None
    text: str = ""
    lang: str = "en"


@app.post("/rural/policy")
async def rural_policy(b: PolicyIn) -> dict:
    from backend import rural
    return rural.policy_check(b.premium, b.pay_years, b.term_years, b.maturity, b.sum_assured, b.term_quote, b.text, b.lang)


class UpiIn(BaseModel):
    text: str = ""
    lang: str = "en"


@app.post("/rural/upi")
async def rural_upi(b: UpiIn) -> dict:
    from backend import rural
    return rural.upi_check(b.text, b.lang)


@app.get("/rural/upi/drills")
async def rural_upi_drills(lang: str = "en") -> dict:
    from backend import rural
    return rural.upi_drills(lang)


class DbtIn(BaseModel):
    scheme: str = "other"
    status: str = "no_status"
    linked: str = "unsure"
    name_same: str = "unsure"
    merged: str = "unsure"
    last_used: str = "recent"
    aadhaar_mobile: str = "unsure"
    text: str = ""
    name: str = ""
    village: str = ""
    block: str = ""
    bank: str = ""
    lang: str = "en"


@app.post("/rural/dbt")
async def rural_dbt(b: DbtIn) -> dict:
    from backend import rural
    return rural.dbt_full(b.scheme, b.status, b.linked, b.name_same, b.merged, b.last_used, b.aadhaar_mobile, b.text, b.name, b.village, b.block, b.bank, b.lang)


class HoldIn(BaseModel):
    qty: float
    price_now: float
    months: int
    price_later: float | None = None
    storage: float = 0
    shrink: float = 0
    handling: float = 0
    rate: float = 7
    history: str = ""
    from_month: int | None = None
    lang: str = "en"


@app.post("/rural/hold")
async def rural_hold(b: HoldIn) -> dict:
    from backend import rural
    return rural.hold_or_sell(b.qty, b.price_now, b.months, b.price_later, b.storage, b.shrink, b.handling, b.rate, b.history, b.from_month, b.lang)


class SaveIn(BaseModel):
    goal: str = "other"
    target: float | None = None
    months: int | None = None
    daily: float | None = None
    daily_wage: float | None = None
    days_per_month: int = 26
    rate: float = 6.7
    lang: str = "en"


@app.post("/rural/saving")
async def rural_saving(b: SaveIn) -> dict:
    from backend import rural
    return rural.daily_saving(b.goal, b.target, b.months, b.daily, b.daily_wage, b.days_per_month, b.rate, 6.0, b.lang)


class ShgIn(BaseModel):
    ledger: dict
    as_of: str | None = None
    lang: str = "en"


class ShgLendIn(ShgIn):
    member: str
    amount: float


@app.post("/rural/shg")
async def rural_shg(b: ShgIn) -> dict:
    """Stateless: the group's ledger lives on the leader's device and is only calculated here, never stored."""
    from backend import rural
    return rural.shg_summary(b.ledger, b.as_of, b.lang)


@app.post("/rural/shg/lend")
async def rural_shg_lend(b: ShgLendIn) -> dict:
    from backend import rural
    return rural.shg_can_lend(b.ledger, b.member, b.amount, b.as_of, b.lang)


class CreditIn(BaseModel):
    has_credit: str = "none"
    missed: str = "never"
    serious: str = "none"
    utilization: str = "na"
    enquiries: str = "0-1"
    age: str = "na"
    informal_only: bool = False
    name: str = ""
    lender: str = ""
    wrong: str = ""
    amount: float = 100000
    years: float = 3
    good_rate: float = 11
    poor_rate: float = 16
    lang: str = "en"


@app.post("/rural/credit")
async def rural_credit(b: CreditIn) -> dict:
    from backend import rural
    return rural.credit_full(b.has_credit, b.missed, b.serious, b.utilization, b.enquiries, b.age, b.informal_only, b.name, b.lender, b.wrong,
                             b.amount, b.years, b.good_rate, b.poor_rate, b.lang)


@app.get("/tours")
async def tours_list(lang: str = "en") -> dict:
    """Every feature that has a guided tour, for the Assistant page's picker."""
    return {"tours": tours_mod.catalogue(lang)}


@app.get("/tours/{tour_id}")
async def tour_steps(tour_id: str, lang: str = "en") -> dict:
    """One tour's steps in one language: the page plays them (open the page, spotlight a part, read the caption)."""
    t = tours_mod.BY_ID.get(tour_id)
    if t is None:
        raise HTTPException(404, "no such tour")
    return tours_mod.public(t, lang)


@app.get("/research/summary")
async def research_summary(ticker: str = "TCS.NS", lang: str = "en") -> dict:
    """Plain pros and cons for one company (no buy/sell signal), set beside what the person already owns."""
    from analysis import proscons
    from core import universe
    ticker = ticker.strip().upper()
    nav = session.portfolio.nav(session.prices) or 0.0
    price = session.prices.get(ticker) or session.pit.last_close(ticker) or 0.0
    sector = universe.sector(ticker)
    holding = None
    if nav:
        holding = {"weight": session.portfolio.positions.get(ticker, 0) * price / nav,
                   "sector_weight": session.portfolio.sector_value(sector, session.prices, universe.sectors()) / nav,
                   "sector_limit": session.policy.limits.max_sector_pct}
    # The summary describes what we hold today, so it reads at the real clock: company data refreshed after the last
    # price bar is included, and the freshness panel says how old each kind of data is.
    from datetime import datetime, timezone
    from core.pit import PointInTimeStore
    today = PointInTimeStore(session.conn, datetime.now(timezone.utc))
    return proscons.pros_cons(today, ticker, lang if lang in ("en", "hi") else "en", holding)


@app.get("/offline/assets")
async def offline_assets() -> dict:
    """Every file the app needs to open with no network, so the browser can keep a copy."""
    files, h = [], hashlib.sha1()
    for p in sorted(FRONTEND.rglob("*")):
        if p.is_file() and p.name != "sw.js" and p.suffix in {".js", ".css", ".html", ".svg", ".png", ".jpg", ".ico", ".json", ".woff2", ".webmanifest"}:
            rel = "/" + p.relative_to(FRONTEND).as_posix()
            files.append({"url": rel, "bytes": p.stat().st_size}); h.update(rel.encode()); h.update(str(p.stat().st_size).encode())
    files.append({"url": "/offline/rural.py", "bytes": (Path(__file__).parent / "rural.py").stat().st_size})
    return {"version": h.hexdigest()[:12], "files": files, "bytes": sum(f["bytes"] for f in files)}


@app.get("/offline/rural.py")
async def offline_rural_source():
    """The calculators' own source: the browser runs this very file (via Pyodide) when offline, so
    online and offline answers cannot disagree."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse((Path(__file__).parent / "rural.py").read_text(encoding="utf-8"))


@app.get("/rural/schemes")
async def rural_schemes(lang: str = "en") -> dict:
    from backend import rural
    return {"schemes": [{"id": x["id"], "name": x["hi"] if lang == "hi" else x["en"]} for x in rural.SCHEMES]}


@app.post("/rural/loan")
async def rural_loan(b: LoanIn) -> dict:
    from backend import rural
    return rural.loan_cost(b.principal, b.rate, b.unit, b.months, b.mode, b.lang)


class OfferIn(BaseModel):
    text: str = ""
    put: float | None = None
    get: float | None = None
    months: float | None = None
    lang: str = "en"


@app.post("/rural/scheme")
async def rural_scheme(b: OfferIn) -> dict:
    from backend import rural
    return rural.scheme_check(b.text, b.put, b.get, b.months, b.lang)


class ProfileIn(BaseModel):
    profile: dict = {}
    lang: str = "en"


@app.post("/rural/entitlements")
async def rural_entitlements(b: ProfileIn) -> dict:
    from backend import rural
    return rural.entitlements(b.profile, b.lang)


class ReadyIn(BaseModel):
    schemes: list[str] = []
    have: list[str] = []
    lang: str = "en"


@app.post("/rural/readiness")
async def rural_readiness(b: ReadyIn) -> dict:
    from backend import rural
    return rural.readiness(b.schemes, b.have, b.lang)


class PlanIn(BaseModel):
    income: list[dict] = []
    monthly_cost: float = 0
    one_offs: list[dict] = []
    savings: float = 0
    borrow_rate: float = 36
    lang: str = "en"


@app.post("/rural/income")
async def rural_income(b: PlanIn) -> dict:
    from backend import rural
    return rural.income_plan(b.income, b.monthly_cost, b.one_offs, b.savings, b.borrow_rate, b.lang)


@app.get("/stocks/search")
async def stocks_search(q: str = "", limit: int = 8) -> dict:
    """Type-ahead over every listed NSE company (plus yfinance as a fallback)."""
    from analysis import stocksearch
    return {"results": await asyncio.to_thread(stocksearch.search, q, min(20, max(1, limit)))}


class StockIn(BaseModel):
    symbol: str
    name: str | None = None


@app.post("/stocks/add")
async def stocks_add(body: StockIn) -> dict:
    """Fetch a searched-for stock's history so the research desks can analyse it."""
    from ingest import ondemand
    if body.symbol in universe.tickers() or body.symbol in universe.extras():
        return {"ok": True, "symbol": body.symbol, "already": True}
    return await asyncio.to_thread(ondemand.add_stock, body.symbol, body.name)


@app.get("/goal")
async def goal(monthly: float = 10000, years: int = 10, target: float = 5_000_000,
               haircut: float = 0.0) -> dict:
    from analysis import goal as goal_mod
    years = max(1, min(30, years))
    return goal_mod.fan(session.pit, session.portfolio, session.prices, max(0, monthly), years, target,
                        max(0.0, min(0.6, haircut)))


@app.get("/panic")
async def panic(key: str = "covid") -> dict:
    from analysis import panic as panic_mod
    return panic_mod.replay(session.pit, session.portfolio, session.prices, key)


class TamperIn(BaseModel):
    id: int
    field: str = "price"
    value: float | str = 1.0
    rehash: bool = False


@app.post("/ledger/demo-seed")
async def ledger_demo_seed() -> dict:
    return {"added": ledger_mod.seed_demo(session.conn, session.pit.clock_iso)}


@app.post("/ledger/tamper")
async def ledger_tamper(body: TamperIn) -> dict:
    return ledger_mod.simulate_tamper(session.conn, body.id, body.field, body.value, body.rehash)


@app.post("/ledger/tamper/real")
async def ledger_tamper_real(body: TamperIn) -> dict:
    return ledger_mod.attempt_real_edit(session.conn, body.id)


@app.get("/ledger")
async def get_ledger(limit: int = 100) -> dict:
    return {"entries": ledger_mod.entries(session.conn, limit),
            "chain": ledger_mod.verify(session.conn)}


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    """One client. Two directions. Either one dying takes the whole connection down.

    The previous shape awaited `receive_json()` in this coroutine and ran the outbound
    pump as a detached task. That is fine when a client closes politely, and a leak when
    it does not: a browser that navigates away, sleeps, or is killed never sends a close
    frame, so `receive_json()` blocked here forever and the subscription was never
    released. Meanwhile the pump's `send_text` failed on the dead socket, raised inside a
    task nobody awaits, and vanished silently.

    The result was a bus that only ever grew — three connections opened and none closed
    across a single session of reloading — with every event being queued into dead
    queues until each hit its cap and started counting drops on the HUD.

    Now both directions are tasks and the first one to finish tears down the other. A
    send failure is as good a disconnect signal as a receive failure, which is what
    catches the clients that leave without saying goodbye.
    """
    await socket.accept()
    queue = bus.subscribe()
    reader: asyncio.Task | None = None
    writer: asyncio.Task | None = None
    try:
        # Catch a reconnecting client up rather than leaving it on a blank screen.
        #
        # Flag it as replay. Without this the client SPOKE the entire back-catalogue on
        # every connect -- twelve past answers read aloud one after another, which is
        # what made the voice seem to repeat itself and made Stop useless, because the
        # next replayed line started the moment you silenced the current one.
        for event in bus.history():
            payload = {**event.payload, "_replay": True}
            await socket.send_text(
                event.model_copy(update={"payload": payload}).dumps())
        if session:
            emit_telemetry(session, bus)

        reader = asyncio.create_task(_reader(socket))
        writer = asyncio.create_task(_pump(socket, queue))
        done, pending = await asyncio.wait({reader, writer},
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        # Surface a genuine fault; a disconnect is not one.
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                bus.emit(EventType.ERROR, where="ws", message=str(exc)[:160])
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        bus.emit(EventType.ERROR, where="ws", message=str(exc)[:160])
    finally:
        bus.unsubscribe(queue)
        for task in (reader, writer):
            if task and not task.done():
                task.cancel()
        with contextlib.suppress(Exception):
            await socket.close()


async def _reader(socket: WebSocket) -> None:
    """Inbound commands. Returns (rather than raising) when the client goes away."""
    while True:
        message = await socket.receive_json()
        if message.get("type") == "command":
            if player and player.active:
                continue   # a live command mid-replay would fight the recording
            _spawn(dispatch(str(message.get("text", "")), message.get("lang"), message.get("cid"), message.get("route")))
        elif message.get("type") == "boot":
            _spawn(boot(session, bus))
        elif message.get("type") == "transcript":
            bus.emit(EventType.TRANSCRIPT, text=message.get("text", ""),
                     final=bool(message.get("final")))


async def _pump(socket: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        event = await queue.get()
        await socket.send_text(event.dumps())


@app.middleware("http")
async def _no_cache_entrypoint(request: Request, call_next):
    """Never cache index.html.

    Asset filenames are content-hashed, so they are safe to cache forever -- but the
    HTML that POINTS at them must not be, or a browser keeps loading yesterday's
    bundle against today's API and the fixes look like they did not land. This cost
    real debugging time: the page was running a two-builds-old script while the
    server served the current one.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="ui")
