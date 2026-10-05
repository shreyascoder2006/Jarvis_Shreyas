"""The chat assistant's brain: routes a question to the right tool and writes a structured,
verifiable answer.

Design rules (the point is to be right, not to sound clever):
  * Intents are decided by precise rules first, then a small trained nearest-neighbour model
    for paraphrases the rules miss, and if neither is confident the assistant ASKS rather
    than guessing. The old behaviour -- falling back to a portfolio summary for anything
    unrecognised -- answered the wrong question confidently.
  * Every figure comes from the same calculators the Practice page uses (analysis/tools.py,
    backend/practice.py, analysis/panic.py, analysis/goal.py), never from a language model.
  * Text is written in the requested language directly from templates, so numbers cannot be
    changed by a translator.
  * Tool-backed answers carry structure (facts, a table, an "open the visual" action) so the
    UI can show them well, and say plainly what is assumed or illustrative.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from analysis import goal as goal_mod
from analysis import panic as panic_mod
from analysis import tools
from backend import digest as digest_mod
from backend import understand
from backend import explain, ledger as ledger_mod, practice
from backend.explain import Answer
from backend.glossary_hi import CLAIM_STATUS_HI, FLAG_LABEL_HI, GLOSSARY_HI
from core import universe


@dataclass
class Ctx:
    pit: Any
    portfolio: Any
    prices: dict
    policy: Any
    conn: sqlite3.Connection
    convo: Any = None
    lang: str = "en"
    level: str = "normal"


# =================================================================== saved funds
def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS my_funds (fund_id TEXT PRIMARY KEY, added TEXT)")
    conn.commit()


def my_funds(conn: sqlite3.Connection) -> list[str]:
    _ensure(conn)
    return [r[0] for r in conn.execute("SELECT fund_id FROM my_funds ORDER BY added, fund_id")]


def set_my_fund(conn: sqlite3.Connection, fund_id: str, on: bool) -> list[str]:
    _ensure(conn)
    if on:
        conn.execute("INSERT OR IGNORE INTO my_funds VALUES (?, datetime('now'))", (fund_id,))
    else:
        conn.execute("DELETE FROM my_funds WHERE fund_id = ?", (fund_id,))
    conn.commit()
    return my_funds(conn)


# =================================================================== fund name matching
# (alias, strong). A weak alias ("tech", "banking") is only a fund when the sentence also says
# "fund"/"mutual"/"mf", so "is TCS a tech stock" never becomes a fund question.
_A = {
    "nifty_index": [("nifty 50 index fund", 1), ("nifty index fund", 1), ("nifty 50 index", 1), ("nifty index", 1),
                    ("index fund", 1), ("nifty 50", 0), ("nifty", 0)],
    "largecap_a": [("sample large cap fund a", 1), ("large cap fund a", 1), ("largecap fund a", 1), ("large cap a", 1),
                   ("large cap fund", 1), ("largecap fund", 1), ("large cap", 0), ("largecap", 0)],
    "bluechip_b": [("sample bluechip fund b", 1), ("bluechip fund b", 1), ("blue chip fund b", 1), ("bluechip fund", 1),
                   ("blue chip fund", 1), ("bluechip", 1), ("blue chip", 1), ("blue-chip", 1)],
    "flexicap_c": [("sample flexi cap fund c", 1), ("flexi cap fund c", 1), ("flexicap fund", 1), ("flexi cap fund", 1),
                   ("flexi-cap", 1), ("flexicap", 1), ("flexi cap", 1), ("flexi", 1)],
    "it_fund": [("technology fund", 1), ("tech fund", 1), ("it sector fund", 1), ("it fund", 1),
                ("technology", 0), ("tech", 0)],
    "bank_fund": [("banking fund", 1), ("bank fund", 1), ("banking sector fund", 1), ("banking", 0), ("bank", 0)],
    "pharma_fund": [("healthcare fund", 1), ("pharma fund", 1), ("health fund", 1), ("healthcare", 0), ("pharma", 0)],
    "consumption": [("consumption fund", 1), ("consumer fund", 1), ("consumption", 0)],
}
_ALIAS_LIST = sorted(((a, fid, strong) for fid, items in _A.items() for a, strong in items),
                     key=lambda x: -len(x[0]))
_FUND_WORD = re.compile(r"\b(funds?|mutual|mfs?|schemes?)\b", re.I)
_LETTER = re.compile(r"\bfund\s+([abc])\b", re.I)


def find_funds(text: str) -> list[str]:
    low = " " + re.sub(r"[^a-z0-9 \-]", " ", text.lower()) + " "
    has_word = bool(_FUND_WORD.search(text))
    hits: list[tuple[int, str]] = []
    for alias, fid, strong in _ALIAS_LIST:
        if not strong and not has_word:
            continue
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", low):
            if all(not (s <= m.start() < e) for s, e, _ in [(h[0], h[0] + 1, 0) for h in hits]):
                hits.append((m.start(), fid))
                low = low[:m.start()] + " " * len(alias) + low[m.end():]
    for m in _LETTER.finditer(text):                       # "fund A and fund B"
        fid = {"a": "largecap_a", "b": "bluechip_b", "c": "flexicap_c"}[m.group(1).lower()]
        hits.append((m.start() + 10_000, fid))
    out: list[str] = []
    for _, fid in sorted(hits):
        if fid not in out:
            out.append(fid)
    return out


# =================================================================== intent rules
def _r(p: str) -> re.Pattern:
    return re.compile(p, re.I | re.S)


_FUNDISH = _r(r"\b(funds?|mutual|mfs?|schemes?|sips?)\b")
_RULES: list[tuple[str, re.Pattern]] = [
    ("digest", _r(r"\brundown\b|\bweekly (money|portfolio)\b|\bone[- ]minute (update|brief\w*|summary|catch-?up)\b|\b(over|in|during) the (past|last) week\b|\b(past|last) (week|7 days|seven days)\b|\b(summary|summari[sz]e|update|brief\w*|roundup|recap)\b.{0,40}\b(this|the|last|past) week\b|\bfor (this|the) week\b|\b(weekly|week'?s|this week'?s?)\b.{0,25}\b(digest|summary|update|report|recap|brief|roundup)\b|\bdigest\b|"
                  r"\b(brief|update|summari[sz]e|catch) me\b.{0,45}\bweek\b|\bhow (was|did|is) my week\b|\bmy week\b|"
                  r"\bsunday (summary|update|brief)\b")),
    ("ledger", _r(r"\b(transaction|trade|order) history\b.{0,30}\b(edit|alter|chang|tamper|modif)\w*|\b(change|alter|edit|modify|rewrite|tamper|fake|backdate)\w*\b.{0,30}\b(past|old|previous|earlier|my)\b.{0,15}\b(trades?|entries|records?|ledger|history)\b|\b(ledger|audit (trail|log)|tamper\w*|hash[- ]?chain|trade history|trade log|trail of (my )?trades|"
                  r"record of (my )?(trades|decisions)|records? (been )?(changed|altered|edited))\b")),
    ("upi_check", _r(r"@@b(collect request|payment request|money request)@@b|@@b(upi|phone ?pe|gpay|google pay|paytm|bhim)@@b.{0,70}@@b(safe|trick|refund|request|qr|scan|pin|link|stuck|mistake|cashback|customer care|anydesk|screen)@@b|@@b(qr code|scan)@@b.{0,50}@@b(receive|get paid|buyer|olx|pin|money)@@b|@@bpin@@b.{0,40}@@b(to receive|receiving|to get)@@b|@@b(sent|transferred) (me )?(money|rs|rupees|payment)? ?(by mistake|wrongly)@@b|@@bby mistake@@b.{0,40}@@b(send|return|back)@@b".replace("@@", chr(92)))),
    ("policy_check", _r(r"@@b(endowment|money ?back|ulip|surrender value|bima agent)@@b|@@b(lic|insurance|policy|bima)@@b.{0,50}@@b(premium|maturity|agent|returns?|worth|good|bonus|surrender|lapse|stop paying|sold|savings?|invest@@w*)@@b|@@b(premium|maturity)@@b.{0,50}@@b(policy|insurance|lic)@@b|@@bagent@@b.{0,40}@@b(policy|insurance|lic)@@b".replace("@@", chr(92)))),
    ("moneylender", _r(r"@@b(money ?lender|sahukar|sahukaar|saahukar|arhtiya|arthiya|adhatiya|loan shark|local lender|private lender)@@b|@@b(byaj|vyaj|sood)@@b|@@b(rupees?|rs|₹)@@s*@@d*@@s*(per|a|for every|on every|in every)@@s+(hundred|100)@@b|@@b(per|a|on every)@@s+(hundred|100)@@s+(rupees?|rs)@@b|@@b(sainkda|saikda|sekda)@@b|@@breal (rate|interest)@@b.{0,25}@@b(loan|lender|borrow)@@b|@@bhow much (interest|byaj) (am i|do i|will i)@@b|@@bcost of (my |this )?(loan|borrowing)@@b|@@binterest (rate )?of (@@d+) ?(rupees?|rs)@@b".replace("@@", chr(92)))),
    ("scheme_check", _r(r"@@b(double|triple|multiply) (my|the|your) money@@b|@@bmoney (will )?(double|triple)@@b|@@b(chit ?fund|ponzi|pyramid|mlm|network marketing|kameti|committee scheme)@@b|@@b(guaranteed|assured|fixed) (monthly|daily|weekly|high) (returns?|income|profit)@@b|@@b(is|are) (this|that|the) (scheme|offer|company|plan|app|business|investment|girvi|group)@@b.{0,25}@@b(real|genuine|legit|legitimate|fake|safe|a scam|true|trustworthy)@@b|@@b(scheme|offer|plan|company)@@b.{0,40}@@b(genuine|legit|fake|scam|fraud)@@b|@@bpay@@b.{0,25}@@bget@@b.{0,40}@@b(months?|weeks?|days?|years?)@@b|@@bjoining fee@@b|@@bbring (your )?(friends|members|people)@@b|@@brefer@@b.{0,15}@@bearn@@b|@@bmoney back in@@b.{0,15}@@b(months?|weeks?|days?)@@b".replace("@@", chr(92)))),
    ("credit_score", _r(r"@@b(cibil|credit (score|report|history|rating|bureau)|no credit history|score repair|crif|experian|equifax)@@b|@@b(loan|credit card)@@b.{0,25}@@b(rejected|declined|denied|not approved|refused)@@b|@@bwhy (was|is|did|has) (my )?(loan|application|card)@@b.{0,25}@@b(rejected|declined|denied|refused)@@b|@@bfirst (loan|credit card)@@b|@@bsimple score@@b".replace("@@", chr(92)))),
    ("shg_ledger", _r(r"@@b(shg|self.?help group|savings group|mahila mandal|bachat gat|group savings)@@b.{0,60}@@b(ledger|record|records|account|accounts|book|register|hisab|track|keep|maintain|manage|interest|loan|loans|collect)@@b|@@b(ledger|register|hisab|accounts?)@@b.{0,40}@@b(shg|self.?help group|savings group|mahila mandal)@@b|@@bgroup ledger@@b".replace("@@", chr(92)))),
    ("saving_goal", _r(r"@@b(want|need|require|planning|plan)@@b.{0,40}@@bfor (my |our )?(daughter|son|beti|beta).s? (marriage|wedding|education|shaadi|studies)@@b|@@b(save|saving|savings|put aside|set aside|keep aside|bachat)@@b.{0,35}@@b(a day|per day|every day|daily|each day|a week|weekly|roz)@@b|@@b(rs|rupees?|₹)@@s?@@d+@@s?(a|per|each|every) (day|week)@@b|@@b(save|saving|fund|bachat)@@b.{0,30}@@b(daughter|son|beti|beta)@@b.{0,25}@@b(marriage|wedding|education|shaadi|studies|school)@@b|@@b(daughter|son|beti|beta).s? (marriage|wedding|education|shaadi|studies)@@b.{0,40}@@b(save|saving|fund|how much)@@b|@@bsave (up )?for (my |a |the )?(marriage|wedding|house repair|roof|medical|cow|buffalo|goat|cart|tools|festival|old age)@@b|@@bhow much (should|do|can) i (save|put aside|set aside) (every|each|a|per) (day|week|month)@@b".replace("@@", chr(92)))),
    ("hold_sell", _r(r"@@bquintals?@@b.{0,90}@@b(wait|hold|sell|store|worth)@@b|@@b(wait|hold)@@b.{0,25}@@b@@d+ ?months?@@b.{0,40}@@b(price|rate|for @@d)@@b|@@b(sell|selling)@@b.{0,25}@@b(now|today|immediately)@@b.{0,30}@@b(or|vs)@@b.{0,25}@@b(wait|hold|store|keep|later)@@b|@@b(wait|hold|store|keep)@@b.{0,30}@@b(my )?(crop|harvest|wheat|rice|paddy|soybean|soyabean|cotton|maize|mustard|gram|onion|potato|grain|produce)@@b|@@b(better|higher) (price|rate|bhav)@@b.{0,30}@@b(later|wait|after|next (month|season))@@b|@@bwhen (should|to) (i )?sell (my )?(crop|wheat|rice|paddy|soybean|cotton|maize|mustard|gram|onion|potato|grain|produce)@@b|@@b(post.?harvest|harvest) (price|dip|glut)@@b|@@bmandi (price|rate|bhav)@@b".replace("@@", chr(92)))),
    ("dbt_trace", _r(r"@@b(payment|subsidy|instal?lment|pension|scholarship|wages?|dbt|benefit|refund)@@b.{0,30}@@b(not (come|came|arrive@@w*|receiv@@w*|credited|reach@@w*)|stuck|stopped|pending|rejected|failed|has not|hasn.t|never)@@b|@@bwhy (did|has|have|is|was) (my )?(payment|subsidy|money|instal?lment|pension|scholarship|wages?)@@b|@@b(pm.?kisan|kisan)@@b.{0,30}@@b(not|stuck|pending|rejected|status|instal?lment)@@b|@@bdbt@@b".replace("@@", chr(92)))),
    ("entitlements", _r(r"@@b(government|govt|sarkari|central|state) (schemes?|yojana|benefits?|subsid@@w+)@@b|@@bschemes?@@b.{0,35}@@b(eligible|entitled|qualify|for me|can i get|available|am i missing|i can)@@b|@@bwhat (benefits|schemes|subsid@@w+|help|yojanas?)@@b.{0,30}@@b(can i|do i|am i|for)@@b|@@b(eligible|entitled) (for|to)@@b|@@bpm[- ]?kisan@@b|@@bayushman@@b|@@bujjwala@@b|@@bm?gnrega@@b|@@bnrega@@b|@@bpm[- ]?awas@@b|@@bpension (scheme|yojana)@@b|@@bam i (missing|not getting)@@b|@@bmoney (i am|i.m|we are) owed@@b|@@bbenefits? (i|we) (can|should|could) (get|claim)@@b".replace("@@", chr(92)))),
    ("docs_ready", _r(r"@@b(documents?|papers?|dastavez|kagaz|paperwork)@@b.{0,35}@@b(need|needed|required|ready|missing|checklist|do i have)@@b|@@baadhaar@@b.{0,35}@@b(link@@w*|seed@@w*|not (linked|working))@@b.{0,40}@@b(bank|account|dbt|subsid@@w*|pension|payment)@@b|@@b(bank|account)@@b.{0,35}@@b(link@@w*|seed@@w*)@@w*@@b.{0,20}@@baadhaar@@b|@@b(link|seed)@@w*@@b.{0,25}@@baadhaar@@b.{0,20}@@b(to|with)@@b.{0,12}@@b(bank|account)@@b|@@bready to apply@@b".replace("@@", chr(92)))),
    ("income_plan", _r(r"@@b(harvest|crop|seasonal?|lumpy|irregular|daily[- ]wage|wage work|casual work)@@b.{0,55}@@b(income|money|earn@@w*|plan|budget|save|saving|manage)@@b|@@b(income|money|pay|payment) (comes|arrives|is received) (only )?(once|in lumps|after (the )?harvest|seasonally|twice)@@b|@@bmanage.{0,30}@@bbetween (harvests?|seasons?|crops?)@@b|@@bplan (my )?(year|season|harvest)@@b|@@blean (months?|season|period)@@b|@@brun(ning)? out of money (before|until)@@b|@@bmoney (finishes|ends|runs out) (before|until)@@b".replace("@@", chr(92)))),
    ("my_funds_remove", _r(r"\b(remove|delete|drop|forget|clear)\b.{0,40}\b(funds?|mutual)\b|\bno longer (own|hold)\b.{0,40}\bfund|"
                           r"\b(sold|exited|redeemed)\b.{0,30}\b(my )?\w*\s?fund\b")),
    ("my_funds_add", _r(r"\b(i|we)\s+(?:\w+\s+){0,2}?(own|hold|have|invest(ed)? in|bought|am invested in|put money in|started)\b.{0,100}\b(funds?|mutual|index|bluechip|flexi|large cap)|"
                        r"\badd\b.{0,60}\bto my (funds|mutual funds|list)\b|\bmy (mutual )?funds? (are|is)\b|\bsave (these|those|my) funds\b")),
    ("my_funds_show", _r(r"\b(what|which)\s+(mutual )?funds\s+(do|have)\s+i\b|\bshow (me )?my (saved )?(mutual )?funds\b|"
                         r"^\s*(what are |list |display )?my (saved )?(mutual )?funds\s*\??\s*$|\bwhich funds have i (saved|added)\b")),
    ("fund_vs_direct", _r(r"\bdirectly\b|\bmy own (stocks|shares)\b|\bmy direct (stocks|shares|holdings)\b|\b(my|the)\s+(direct\s+)?(stocks|shares|holdings)\b.{0,70}\b(in|inside|through|via|within)\b.{0,25}\b(funds?|mutual)\b|"
                          r"\balready own\b.{0,40}\bfunds?\b|\b(funds?|mutual)\b.{0,50}\b(my|the)\s+(direct\s+)?(stocks|shares|holdings)\b|"
                          r"\bdo my (stocks|shares|holdings)\b.{0,30}\b(appear|show up|repeat)\b.{0,30}\bfunds?\b")),
    ("fund_overlap", _r(r"\b(overlap\w*|same stocks?|same shares|common (stocks|holdings|shares)|duplicat\w*|similar funds?|"
                        r"hold the same|holding the same|paying twice|pay twice|twice for the same|redundant|same thing|same as|clash(es)?|conflict|doubling up|double up|alike|most similar|how similar|crossover|cross-over|same job|twins?|clones?|copies|carbon cop(y|ies))\b")),
    ("fund_list", _r(r"\bwhich (mutual )?funds do you (track|analy[sz]e|have|cover|support|know)\b|\bwhat (mutual )?funds do you (cover|have|support|know|offer)\b|\b(name|list) the funds\b|\b(what|which|list|show|name|available)\b.{0,25}\b(mutual )?funds?\b.{0,30}\b(have|available|compare|cover|support|know|there|list|offer)\b|"
                      r"\blist (of )?(all )?(the )?(mutual )?funds\b|\bshow me (all )?(the )?(mutual )?funds\b|\bwhich funds (can|do) (i|you)\b")),
    ("fund_info", _r(r"\b(biggest|largest|top|main|major) (holdings|positions|stocks)\b.{0,30}\bfund\b|\bstocks?\b.{0,40}\bfund\b.{0,15}\b(invested in|holds?|owns?)\b|\bfund\b.{0,12}\binvested in\b|\bwhat (stocks|shares|companies)\b.{0,30}\b(does|do|are|is)\b.{0,40}\bfund\b|\b(top )?holdings? (of|in)\b|\bwhat('?s| is| does)\b.{0,25}\b(inside|in|hold|holds|own)\b.{0,30}\bfund\b|"
                      r"\bwhat does\b.{0,40}\bhold\b|\bwhat stocks (are )?(in|inside)\b|\bwhat('?s| is) in (the )?\w+\s?\w*\s?fund\b")),
    ("fee_drag", _r(r"\bexpense ratios?\b|\b(management fees?|manager fees?|fund manager|fees?|expense ratio|ter|brokerage|commission|charges?|direct plans?|regular plans?|expense)\b")),
    ("emergency", _r(r"\bbuffer\b.{0,30}\b(job|income|salary)\b|\b(cushion|income stopped|lose my job|lost my job|how many months do i get)\b|\b(savings?|cash|money)\b.{0,30}\b(cover|last|carry)\b.{0,30}\b(without|if|no)\b.{0,20}\b(income|salary|job|work)\b|\bwithout (any )?(income|salary|a job)\b|\bhow long (can|will|could|would) (i|my|it|that|we)\b.{0,40}\b(live|last|survive|manage|cope|get by)\b|\b(emergency fund|emergency savings|emergency money|rainy day|runway|safety net|"
                      r"(how long|how many months).{0,45}\b(last|survive|manage|cover|go on|stay afloat|get by)\b|"
                      r"(lose|lost|losing|quit|laid off|layoffs?|job loss|no job|without (a )?(job|salary|income))\b.{0,30}\b(job|income|salary|work)?\b.{0,30}\b(months?|how long|last|survive|savings)\b|"
                      r"if my (salary|income|job) (stops|ends|goes|disappears)|months? of (expenses|living costs|savings)|"
                      r"enough (cash|savings|money) (for|to cover)|cash cushion)\b")),
    ("goal", _r(r"\b(likelihood|odds|chances?|probabilit\w+)\b.{0,40}\b(crore|lakh|million|target|goal)\b|\bmy (target|goal) is\b.{0,60}\b(years?|yrs?)\b|\bcan i make it\b|\bwhat do i need to invest\b|\bcrorepati\b|\b(sip|investment|portfolio|savings?)\b.{0,30}\b(build|reach|become|make|create)s?\b.{0,20}\b(a |an )?(crore|lakh|million)\b|\b(saving|investing|putting away) enough\b|\b(reach|hit|get to|achieve|make|build|plan|path to|odds)\b.{0,35}\b\d[\d.,]*\s?(lakh|lakhs|crore|crores|cr|lac|lacs)\b.{0,40}\b(years?|yrs?|retire|sip|monthly)\b|\bhow much (would|will|could|can) i (have|get|end up with|accumulate)\b.{0,40}\b(in|after|over|by)\b|\b(sip|invest\w*|portfolio)\b.{0,40}\b(turn into|become|grow to|be worth|worth|end up)\b|\b(retire\w*|financial goal|my goal|corpus|target amount|nest egg|"
                 r"(will|can|could|should) i (reach|get to|have|hit|accumulate|build|make|become)\b.{0,50}\b(lakh|crore|lac|\d+\s?(k|cr|l)\b)|"
                 r"how (long|many years) (will|would|does|to|until)\b.{0,30}\b(take|reach|get|build|hit)|"
                 r"(reach|build|save|accumulate|become|get)\b.{0,30}\d.{0,12}\b(lakh|crore|lac|cr|k)\b.{0,40}\b(years?|yrs?|sip|monthly)|"
                 r"(sip|invest\w*)\b.{0,40}\b(for|over|in)\b.{0,10}\d+\s?(years?|yrs?)\b.{0,50}\b(grow|become|worth|reach|get me|end up|amount to)|"
                 r"will my (sip|investment|portfolio)\b.{0,30}\b(reach|be enough|grow|get me))\b")),
    ("panic", _r(r"\b(wanting|want|tempted|urge|itching|feel like) to sell\b|\bsell when (the )?markets? (drops?|falls?|crash\w*)\b|\b(sold|sell\w*|panic\w*|exit\w*|bail\w*|dump\w*|got out)\b.{0,45}\b(covid|crash|2020|2022|2023|bottom|sell-?off|adani|rate shock|fall)\b|"
                  r"\b(covid|crash|2020|2022|2023|bottom|sell-?off|adani)\b.{0,35}\b(sold|sell|panic\w*|dump\w*|bail\w*)\b|"
                  r"\bpanic[- ]?sell\w*|\bif i (had )?(sold|exited|panicked)\b|\bwhat (if|would have happened).{0,25}\b(i )?(sold|exit)\b")),
    ("tip_scan", _r(r"\b(telegram|whatsapp|instagram|youtube|facebook) (channel|group|page|account)\b.{0,60}\b(tip|tips|guarantee\w*|trust|signal|sure)\b|\b(tip|tips|signal)\b.{0,40}\b(for|costs?) \d+\b.{0,60}\b(guarantee\w*|trust)\b|\b(is|are)\s+(this|that|these)\b.{0,25}\b(tip|tips|message|post|forward\w*|group|channel|advice|signal)\b.{0,25}\b(legit|real|fake|scam|safe|genuine|true|trustworthy|reliable)\b|"
                     r"\b(check|scan|verify|analy[sz]e|screen|vet)\b.{0,15}\b(this|that|the)\b.{0,10}\b(tip|message|post|forward\w*|text)\b|"
                     r"\b(tip|tips|message|group|advice|signal)\b.{0,40}\b(on|from|in)\b.{0,12}\b(telegram|whatsapp|instagram|youtube|facebook|twitter)\b.{0,40}\b(legit|real|fake|scam|safe|true|trust\w*|check)\b|"
                     r"\b(sure[- ]?shot|100% (return|profit|guaranteed)|guaranteed (returns?|profit|(\d+x)|doubl\w+)|double your money|multibagger tip)\b")),
    ("scam_recovery", _r(r"\b(anydesk|quicksupport|teamviewer|remote (access|app))\b.{0,60}\b(took|taken|withdrew|stole|debited|deducted|lost|money|cleared)\b|"
                          r"\b(lost|losing|gave|paid|sent|transferred|debited|deducted|stolen|took|taken|withdrew|withdrawn|cheated|scammed|defrauded|duped|conned|tricked|robbed|fell for)\b.{0,70}\b(fraud\w*|scam\w*|fake|cheat\w*|cyber|phishing|stranger|unknown (caller|number|person)|caller|officer|anydesk|quicksupport|teamviewer|otp)\b|"
                          r"\b(i|we|my (mother|father|parents?|wife|husband|son|daughter|friend|brother|sister))\b.{0,20}\b(was|were|got|been|am|is|have been|had been)\b.{0,12}\b(scammed|cheated|defrauded|duped|conned|tricked|robbed|hacked)\b|"
                          r"\b(i|we)\b.{0,15}\b(shared|told|gave|sent)\b.{0,25}\b(otp|pin|cvv|password|code)\b|"
                          r"\bunauthori[sz]ed (transaction|debit|payment|withdrawal)\b|\bmoney (got |was |has been |is )?(debited|deducted|stolen|withdrawn|missing)\b.{0,30}\b(without|fraud|fake|scam|cyber|unknown)\b|"
                          r"\breport (a |an |this )?(cyber ?)?(fraud|scam|crime)\b|\bhow (do|can|to|should) (i )?(report|complain about) .{0,25}(fraud|scam|cyber)|"
                          r"\b(lost|losing|lose)\b.{0,40}\b(on|in|to|through|via|because of)\b.{0,25}\b(whatsapp|telegram|instagram|youtube|facebook|tips?|group|channel|trading app|fake app|crypto)\b|"
                          r"\b(get|recover) my money back\b|\brecover (my |the )?(lost )?money\b|"
                          r"\bwhat (do|should) i do (after|if|when)\b.{0,30}\b(scam\w*|fraud\w*|cheat\w*|hack\w*)\b")),
    ("scam_help", _r(r"\bincome tax (department|officer)\b|\bsim (card )?(will be |is )?(blocked|deactivated|disconnected)\b|\b(claim|won) (a |your )?(prize|lottery|reward|gift)\b|\bclick (on )?(a|the|this) link\b|\bpolice\b.{0,25}\barrest\b.{0,20}\b(video|phone|call|online)\b|\bpenalty\b.{0,30}\bupi\b|\bshare the (code|otp)\b|\b(parcel|courier|customs)\b.{0,40}\b(illegal|drugs|contraband|seized|held|in my name|arrest)\b|\bcaller says\b|\b(transfer|send|move|pay)\b.{0,25}\bmoney\b.{0,40}\b(police|officer|rbi|bank|account|unblock|safe account|verification)\b|\b(screen[- ]?shar\w*|download|install)\b.{0,40}\bapps?\b.{0,40}\b(account|unblock|bank|kyc|fix)\b|\b(call|caller|sms|email|link|officer|message)\b.{0,30}\b(real|genuine|fake|legit|safe|authentic)\b|\blink\b.{0,40}\bkyc\b|\bkyc\b.{0,40}\blink\b|\b(otp|cvv|kyc (call|expired|update|pending)|digital arrest|scam\w*|fraud\w*|cheated|phishing|fake (call|caller|link|app|website|officer)|"
                      r"suspicious (call|message|link|sms|email)|someone (called|rang|messaged)|(got|received|had) (a|an) (call|message|sms|email)|anydesk|quicksupport|teamviewer|"
                      r"remote (access|control|app)|upi (fraud|scam|request)|collect request|1930|cyber ?crime|asked (me )?for my (pin|otp|password)|"
                      r"lost money (to|in) (a )?(scam|fraud)|blackmail\w*|loan app|parcel (with|has|held)|customs (officer|call))\b")),
    ("predict", _r(r"\b(penny stocks?|explode|rocket|ten[- ]?bagger|10 ?x)\b|\bwhich (small|mid|large)[- ]?cap\b|\bmake me rich\b|\bwill (bitcoin|btc|crypto|gold|silver|nifty|sensex|the market|[a-z]+)\b.{0,15}\b(cross|hit|touch|reach|breach)\b|\b(target price|price target)\b|\b(will|going to)\b.{0,25}\b(give|make|return|get)\b.{0,15}\b\d+ ?x\b|\b\d+ ?x\b.{0,12}\b(return|returns|in a year|in a month)\b|\b(will|going to|gonna)\b.{0,25}\b(double|triple|go up|rise|moon|crash|fall|rally|jump|shoot up|touch)\b|"
                    r"\b(price target|multibagger|which stocks? (to buy|will|should i buy|is best)|best stocks? to buy|stocks? to buy (now|today|tomorrow)|"
                    r"hot stocks?|next (big )?winner|will .{0,20} (be|hit) (₹|rs)?\s?\d)\b|\bpredict\w*\b|\bforecast\b|\btomorrow'?s? (price|market)\b|\bwhere will (the )?(market|nifty|sensex)\b")),
    ("help", _r(r"\bhow can you help\b|\bwhat should i ask\b|\bwhat (are )?(the )?(things|stuff|questions|topics) (i|we) can (ask|do|say)\b|^\s*(help|what can you do|what do you do|what can i (ask|do)|how (do|can) i use (this|you)|who are you|what are you|features|capabilities|show (me )?what you can do|menu)\b")),
    ("stress", _r(r"\b(tanks?|plunges?|collapses?|crashes|slumps?|tumbles?)\b.{0,20}\b\d+\s?(%|percent)\b")),
    ("diversification", _r(r"\b(same|one|single) sector\b|@@b(too heavily|overexposed|over-exposed|eggs in one basket|too tilted|too much (of my money )?(is )?(riding )?in)@@b.{0,30}@@b(one|single|same|a)@@b|@@btoo heavily invested@@b".replace("@@", chr(92)))),
    ("correlation", _r(r"\blockstep\b|\bin sync\b|\b(fall|rise|move|go up|go down|drop) together\b|\btend to (fall|rise|move|drop)\b|@@b(behave|act|move|trade)@@w*@@b.{0,15}@@b(the same|alike|similarly|in sync|together)@@b|@@bany of my (shares|stocks|holdings)@@b.{0,30}@@b(same|alike|together)@@b".replace("@@", chr(92)))),
    ("should_buy", _r(r"@@b(good|great|wise|smart|sensible|safe|right)@@s+(addition|buy|pick|idea to buy|time to buy|stock to buy)@@b|@@b(should|can|could|may|shall) i (get(?! rid)|pick up|add|buy|invest in|purchase|take)@@b|@@b(tell me|let me know|advise me|help me decide|suggest)@@b.{0,30}@@b(if|whether)@@b.{0,25}@@b(buy|invest in|purchase|get|add)@@b|@@bis (it|[a-z .&]{2,25}) (ok|okay|safe|worth|good|wise|fine)@@b.{0,12}@@b(to )?(buy|buying|invest|investing)@@b|@@bworth (buying|investing)@@b|@@b(is|would) (it|[a-z .&]{2,25}) (a )?(good|safe|wise) (buy|stock|investment|addition)@@b|@@bshould i (still )?(buy|invest)@@b".replace("@@", chr(92)))),
    ("fix", _r(r"\bwhich of my (stocks|shares|holdings)\b.{0,25}\b(sell|drop|exit|get rid of|dump|cut|trim|reduce)\b|\bget rid of\b|@@bwhat should i (cut|drop|reduce|lower|offload|get rid of|trim)@@b|@@bto be safer@@b|@@bhow (can|do) i (de-?risk|reduce my risk|make it safer)@@b".replace("@@", chr(92)))),
    ("why", _r(r"\b(weakest|weak) (part|spot|point|link)\b|@@b(main|biggest|top|key|major|worst)@@s+(risks?|problems?|weak(ness)?es)@@b|@@brisks? in my@@b".replace("@@", chr(92)))),
    ("xray", _r(r"\bhow is everything\b|\b(big picture|bird.s eye|how my (investments|money|portfolio) (are|is) doing)\b|\b(check-?up|health ?check|overview|status)\b.{0,25}\b(my|of my)\b.{0,15}\b(investments?|portfolio|money|holdings)\b|@@b(in good shape|doing well|state of my (money|portfolio|investments)|my overall (position|picture)|how (am|are) (i|you) doing with)@@b".replace("@@", chr(92)))),
    ("chitchat", _r(r"^\s*(hi|hello|hey|namaste|namaskar|good (morning|afternoon|evening)|thanks?( you)?|thank you|ok(ay)?|cool|great|nice|bye|goodbye|see you)\W*$")),
]

LEGACY = {"define", "stress", "fix", "why", "xray", "correlation", "diversification", "simplify"}
NEW_KINDS = {"digest", "ledger", "my_funds_add", "my_funds_remove", "my_funds_show", "fund_vs_direct", "fund_overlap",
             "fund_list", "fund_info", "fee_drag", "emergency", "goal", "panic", "tip_scan", "scam_help", "predict",
             "help", "chitchat", "clarify", "out_of_scope", "scam_recovery"}


_OWNERSHIP = _r(r"\b(i|we)\s+(?:\w+\s+){0,2}?(own|hold|have|invest(ed)?|bought|put|started|am invested)\b|\badd\b.{0,60}\bto my\b|\bmy funds? (are|is)\b")
_NOT_OVERLAP = _r(r"\b(remove|delete|drop|forget|clear|sold|redeemed|fees?|expense|cost|charges?|top holdings|holdings of|"
                  r"what (is|does|are)\b.{0,25}\b(in|inside|hold)|inside the)\b")
_OFFTOPIC = _r(r"\b(how (tall|far|old|big|deep|long ago)|everest|bedtime|who (invented|discovered|wrote)|when was (the )?\w+ (born|built)|weather|rain|temperature|joke|funny|cricket|ipl|football|soccer|movie|film|song|music|poem|story|recipe|cook|bake|"
               r"cake|biryani|pizza|restaurant|coffee|hotel|flight|nearby|holiday|vacation|travel|usa|capital of|prime minister|president|who won|world cup|translate|homework|girlfriend|boyfriend|dating|"
               r"politic\w*|election|celebrity|what time is it|riddle|sing|game)\b")
_ONTOPIC = _r(r"\b(fund|funds|stock|stocks|share|shares|portfolio|invest\w*|sip|savings?|loan|tax|fee|fees|bank|nifty|sensex|market|"
              r"holdings?|crore|lakh|rupees?|emi|insurance|scam|otp|digest|ledger|mutual)\b|₹")
_DEF_Q = _r(r"^\s*(what('?s| is| are| does| do)|whats|explain|define|meaning of|tell me about|what do you mean by)\b")
_PERSONAL = _r(r"\b(my|mine|i|we|our)\b|\d")


def rule_intent(text: str) -> str | None:
    low = text.strip()
    funds_early = find_funds(low)
    # A plain "what is X?" about a term we have a definition for is a definition, even when X
    # is also a trigger word for a tool ("expense ratio", "overlap", "otp").
    if _DEF_Q.search(low) and not _PERSONAL.search(low) and explain.define(low) and not (
            funds_early and re.search(r"\b(holdings?|positions|inside|contain\w*|stocks|biggest|largest|top|own)\b", low, re.I)):
        return "define"
    funds = find_funds(low)
    # A named fund plus a comparison word is an overlap question even without the word
    # "overlap": "compare large cap fund A and bluechip fund B".
    if (_OFFTOPIC.search(low) and not _ONTOPIC.search(low)):
        return "out_of_scope"
    if len(funds) >= 2 and not _OWNERSHIP.search(low) and not _NOT_OVERLAP.search(low):
        return "fund_overlap"
    for kind, rx in _RULES:
        if rx.search(low):
            if kind == "fee_drag" and not _fee_context_ok(low):
                continue
            if kind == "fund_overlap" and not (funds or _FUNDISH.search(low)):
                continue       # "do my stocks overlap" is the older correlation question
            if kind in ("my_funds_add",) and not funds:
                continue
            if kind == "my_funds_remove" and not (funds or re.search(r"\b(all|everything|my funds)\b", low, re.I)):
                continue
            if kind == "fund_vs_direct" and not (funds or _FUNDISH.search(low)):
                continue
            if kind == "fund_info" and not funds:
                continue
            return kind
    return None


def _fee_context_ok(low: str) -> bool:
    """'fee' alone is a weak signal ("tax fee on selling"?). It needs a fund/investing context, a
    figure, or cost words, so only real fee-drag questions land here."""
    if re.search(r"\b(tax|stt|capital gains?|ltcg|stcg)\b", low, re.I) and not _FUNDISH.search(low):
        return False
    if re.search(r"\bexpense ratios?\b", low):
        return True
    return bool(_FUNDISH.search(low) or re.search(r"\d|\b(direct|regular) plans?\b", low) or
                re.search(r"\b(cost|costs|eat|eats|drag|impact|compound|affect|matter|worth|hurt|pay|paying|high|low|cheap|expensive)\b", low, re.I))


# =================================================================== trained fallback
_KNN = None


def _knn():
    global _KNN
    if _KNN is None:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from scipy.sparse import hstack
        from backend.intent_data import EXAMPLES
        texts, labels = [], []
        for label, items in EXAMPLES.items():
            for it in items:
                texts.append(_mask(it))
                labels.append(label)
        v1 = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True)
        v2 = TfidfVectorizer(analyzer="word", ngram_range=(1, 2), sublinear_tf=True)
        X = hstack([v1.fit_transform(texts), v2.fit_transform(texts)]).tocsr()
        _KNN = (v1, v2, X, labels, texts)
    return _KNN


_NAME_RX = None
_ALIAS_RX = None


def _mask(t: str) -> str:
    """Hide the entities (fund names, company names, numbers) so the model learns the shape of
    the request, not which fund or figure happened to be in the examples."""
    global _NAME_RX, _ALIAS_RX
    if _NAME_RX is None:
        names = sorted({universe.name(x).lower() for x in universe.tickers()} |
                       {x.split(".")[0].lower() for x in universe.tickers()}, key=len, reverse=True)
        _NAME_RX = re.compile("(?<![a-z])(" + "|".join(re.escape(n) for n in names) + ")(?![a-z])")
        _ALIAS_RX = re.compile("(?<![a-z0-9])(" + "|".join(re.escape(a) for a, _, _ in _ALIAS_LIST) + ")(?![a-z0-9])")
    t = t.lower()
    t = _ALIAS_RX.sub(" fundname ", t)
    t = _NAME_RX.sub(" companyname ", t)
    t = re.sub(r"\d[\d,]*(\.\d+)?", " num ", t)
    t = re.sub(r"\bfund\s+[abc]\b", " fundname ", t)
    return re.sub(r"\s+", " ", t).strip()


def knn_rank(text: str, exclude: str | None = None) -> list[tuple[str, float]]:
    """Intents ranked by the similarity of their closest training example."""
    from scipy.sparse import hstack
    from sklearn.preprocessing import normalize
    v1, v2, X, labels, texts = _knn()
    q = hstack([v1.transform([_mask(text)]), v2.transform([_mask(text)])]).tocsr()
    sims = (normalize(X) @ normalize(q).T).toarray().ravel()
    if exclude is not None:                                    # leave-one-out for evaluation
        for i, tx in enumerate(texts):
            if tx == _mask(exclude):
                sims[i] = -1
    best: dict[str, float] = {}
    for sim, lab in zip(sims, labels):
        if lab == "chitchat":          # greetings are matched by rule only: too short to trust a similarity score
            continue
        best[lab] = max(best.get(lab, 0.0), float(sim))
    return sorted(best.items(), key=lambda kv: -kv[1])


def knn_intent(text: str, exclude: str | None = None) -> tuple[str | None, float, float]:
    """(intent, best similarity, margin over the runner-up intent)."""
    ranked = knn_rank(text, exclude)
    if not ranked:
        return None, 0.0, 0.0
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    return ranked[0][0], ranked[0][1], ranked[0][1] - second


def detect(text: str) -> tuple[str, str]:
    """(intent, how). Precise rules, then the original explainer's rules, otherwise "clarify".
    The similarity model is not allowed to decide: measured leave-one-out it was right about
    half the time, and a confident wrong answer is worse than a question. It suggests instead
    (see h_clarify), and an optional local-LLM classifier (aanswer) handles odd phrasings."""
    k = rule_intent(text)
    if k:
        return k, "rule"
    legacy = explain.classify_strict(text)
    if legacy:
        return legacy, "legacy"
    return "clarify", "none"


# =================================================================== answer helpers
def _t(lang: str):
    return (lambda en, hi: hi if lang == "hi" else en)


def _fact(label: str, value: str, tone: str = "") -> dict[str, str]:
    return {"label": label, "value": value, "tone": tone}


def _done(a: Answer, ctx: Ctx, chips: list[tuple[str, str]], kind: str) -> Answer:
    """Attach follow-ups (English question, Hindi label), apply the explain-back level."""
    a.kind = kind
    a.lang = ctx.lang
    a.follow_ups = [q for q, _ in chips]
    if ctx.lang == "hi":
        a.follow_ups_hi = [h for _, h in chips]
    if ctx.lang == "hi":                       # keep the Indian number words in the language of the text
        def hi(x: str) -> str:
            return x.replace(" crore", " करोड़").replace(" lakh", " लाख")
        for f in a.facts:
            f["value"] = hi(f["value"])
        if a.table:
            a.table["rows"] = [[hi(c) for c in row] for row in a.table["rows"]]
    if ctx.level == "simple":
        a.bullets = a.bullets[:1]
        a.table = None
        a.detail = None
    elif ctx.level == "maths" and a.detail:
        a.bullets = [*a.bullets, a.detail]
    if ctx.convo is not None:
        ctx.convo.remember(a)
    return a


_FU = {
    "overlap": ("Which funds overlap the most?", "कौन से फ़ंड सबसे ज़्यादा मिलते-जुलते हैं?"),
    "fee": ("What does a 2% fee cost over 20 years?", "2% फ़ीस बीस साल में कितनी पड़ती है?"),
    "emerg": ("How long will 3 lakh last if I spend 40000 a month?", "3 लाख रुपये 40000 महीने के ख़र्च पर कितना चलेंगे?"),
    "goal": ("Will 10000 a month reach 50 lakh in 15 years?", "10000 महीने की SIP से 15 साल में 50 लाख बनेंगे?"),
    "panic": ("What if I had sold during the Covid crash?", "कोविड गिरावट में बेच देता तो क्या होता?"),
    "digest": ("Give me my weekly digest", "मेरा साप्ताहिक सार सुनाइए"),
    "scam": ("Someone asked for my OTP on a call", "किसी ने फ़ोन पर मेरा OTP माँगा"),
    "list": ("Which mutual funds do you have?", "आपके पास कौन से म्यूचुअल फ़ंड हैं?"),
    "help": ("What can you do?", "आप क्या-क्या कर सकते हैं?"),
    "xray": ("How am I doing?", "मेरा पोर्टफ़ोलियो कैसा चल रहा है?"),
    "ledger": ("Is my ledger intact?", "क्या मेरा रिकॉर्ड सुरक्षित है?"),
    "why": ("Why is my risk high?", "मेरा जोखिम ज़्यादा क्यों है?"),
    "fix": ("What should I sell?", "मुझे क्या बेचना चाहिए?"),
    "stress": ("What if the market drops 20%?", "बाज़ार 20% गिरे तो क्या होगा?"),
    "div": ("Am I diversified?", "क्या मेरा पैसा अलग-अलग जगह बँटा है?"),
}


def _chips(*keys: str) -> list[tuple[str, str]]:
    return [_FU[k] for k in keys]


def _pc(x: float) -> str:
    """Round half up, so 88.5% reads 89% here exactly as it does on the overlap ring."""
    return str(int(x + 0.5))


def _mo(n: int, hi: bool) -> str:
    return f"{n} महीने" if hi else f"{n} month{'' if n == 1 else 's'}"


def _fname(fid: str) -> str:
    return practice.FUNDS[fid]["name"].replace("Sample ", "")


# =================================================================== handlers
def h_help(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    a = Answer(headline=t("I help you protect your money, understand it and keep it honest.",
                          "मैं आपके पैसे को सुरक्षित रखने, समझने और ईमानदार रखने में मदद करता हूँ।"),
               bullets=[t("Protect: check a stock tip, a suspicious call or a scam message.",
                          "सुरक्षा: स्टॉक टिप, संदिग्ध कॉल या ठगी के संदेश की जाँच।"),
                        t("Understand: fund overlap, what fees cost, emergency cash, whether a goal is reachable, what a crash would do.",
                          "समझ: फ़ंड ओवरलैप, फ़ीस की असली क़ीमत, इमरजेंसी पैसा, लक्ष्य पूरा होगा या नहीं, गिरावट का असर।"),
                        t("Keep honest: your trade record is tamper-evident, and every figure comes from a calculator, not a guess.",
                          "ईमानदारी: आपका लेन-देन रिकॉर्ड छेड़छाड़ पकड़ने वाला है, और हर आँकड़ा कैलकुलेटर से आता है, अंदाज़े से नहीं।")],
               action=t("Try one of the questions below. I will tell you plainly when I do not know.",
                        "नीचे का कोई सवाल आज़माइए। जो मुझे नहीं पता, वह मैं साफ़ बता दूँगा।"))
    return _done(a, ctx, _chips("overlap", "fee", "emerg", "goal", "panic", "digest"), "help")


def h_chitchat(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    thanks = re.search(r"thank|bye|cool|great|nice|ok", text, re.I)
    a = Answer(headline=t("Happy to help. Ask me anything about your money." if thanks else "Hello! What would you like to know about your money?",
                          "ख़ुशी हुई। अपने पैसे के बारे में कुछ भी पूछिए।" if thanks else "नमस्कार! अपने पैसे के बारे में क्या जानना चाहेंगे?"))
    return _done(a, ctx, _chips("xray", "overlap", "fee", "help"), "chitchat")


def h_out_of_scope(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    a = Answer(headline=t("That is outside what I can help with. I only handle money questions.",
                          "यह मेरे दायरे से बाहर है। मैं सिर्फ़ पैसे से जुड़े सवालों में मदद कर सकता हूँ।"),
               action=t("Ask about your portfolio, funds, fees, emergency cash, goals or scams.",
                        "अपने पोर्टफ़ोलियो, फ़ंड, फ़ीस, इमरजेंसी पैसे, लक्ष्य या ठगी के बारे में पूछिए।"))
    return _done(a, ctx, _chips("xray", "overlap", "scam", "help"), "out_of_scope")


_INTENT_CHIP = {"fund_overlap": "overlap", "fund_list": "list", "fund_info": "list", "fund_vs_direct": "overlap",
                "my_funds_add": "overlap", "my_funds_show": "overlap", "my_funds_remove": "overlap",
                "fee_drag": "fee", "emergency": "emerg", "goal": "goal", "panic": "panic", "digest": "digest",
                "scam_help": "scam", "scam_recovery": "scam", "tip_scan": "scam", "predict": "scam", "ledger": "ledger", "help": "help",
                "xray": "xray", "why": "why", "fix": "fix", "stress": "stress", "diversification": "div",
                "correlation": "xray", "should_buy": "xray", "moneylender": "loan", "policy_check": "loan", "upi_check": "scam", "dbt_trace": "docs", "hold_sell": "income", "saving_goal": "goal", "shg_ledger": "income", "credit_score": "loan", "scheme_check": "schemes", "entitlements": "schemes", "docs_ready": "docs", "income_plan": "income", "define": "help", "simplify": "xray"}


def suggestions(text: str, n: int = 3) -> list[tuple[str, str]]:
    """The nearest few things I know how to do, as tappable questions."""
    chips: list[tuple[str, str]] = []
    try:
        ranked = knn_rank(text)
    except Exception:  # noqa: BLE001
        ranked = []
    for intent, sim in ranked:
        key = _INTENT_CHIP.get(intent)
        if key and sim > 0.15 and _FU[key] not in chips:
            chips.append(_FU[key])
        if len(chips) >= n:
            break
    for key in ("xray", "overlap", "fee"):
        if len(chips) >= n:
            break
        if _FU[key] not in chips:
            chips.append(_FU[key])
    return chips


def h_clarify(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    a = Answer(headline=t("I am not sure what you are asking, and I would rather ask than guess.",
                          "मुझे पक्का समझ नहीं आया कि आप क्या पूछ रहे हैं, और मैं अंदाज़ा लगाने से बेहतर पूछना समझता हूँ।"),
               bullets=[t("Try naming the thing: a fund, a fee, your emergency cash, a goal, a call or message, or a company.",
                          "किसी चीज़ का नाम लीजिए: फ़ंड, फ़ीस, इमरजेंसी पैसा, लक्ष्य, कोई कॉल या संदेश, या कोई कंपनी।")],
               action=t("Or pick one of these.", "या इनमें से कोई चुनिए।"))
    return _done(a, ctx, suggestions(text), "clarify")


def h_predict(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    a = Answer(headline=t("Nobody can reliably predict where a price will go, and I will not pretend to.",
                          "कोई भी भरोसे से नहीं बता सकता कि भाव कहाँ जाएगा, और मैं दिखावा नहीं करूँगा।"),
               bullets=[t("Anyone promising a sure winner or a doubling is selling something. That is the most common scam line.",
                          "जो भी पक्का मुनाफ़ा या पैसा दुगना होने का वादा करे, वह कुछ बेच रहा है। यह सबसे आम ठगी की बात है।"),
                        t("What I can do is show what a fall would do to your own holdings, and check a tip for scam tactics.",
                          "मैं यह दिखा सकता हूँ कि गिरावट आने पर आपके अपने शेयरों का क्या होगा, और किसी टिप में ठगी की चालें जाँच सकता हूँ।")],
               action=t("Ask what a fall would do to you, or paste a tip to check.",
                        "पूछिए कि गिरावट का आप पर क्या असर होगा, या जाँचने के लिए कोई टिप चिपकाइए।"))
    return _done(a, ctx, _chips("panic", "scam", "xray"), "predict")


# ---- funds -----------------------------------------------------------------------------
def _own(ctx: Ctx) -> dict[str, float]:
    return {tk: sh * ctx.prices.get(tk, 0.0) for tk, sh in ctx.portfolio.positions.items()}


def _ranked_pairs(ids: list[str]) -> list[dict[str, Any]]:
    out = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            o = practice.overlap(a, b, universe.name)
            out.append({"a": a, "b": b, "pct": o["overlap_pct"], "n": o["shared_stocks"], "verdict": o["verdict"], "o": o})
    out.sort(key=lambda r: -r["pct"])
    return out


def _verdict_words(v: str, t) -> str:
    return {"red": t("almost the same portfolio", "लगभग एक ही पोर्टफ़ोलियो"),
            "amber": t("a noticeable overlap", "ध्यान देने लायक़ ओवरलैप"),
            "green": t("genuinely different", "सचमुच अलग-अलग")}[v]


_DISC = ("Holdings are illustrative samples typical of each fund type, not live factsheets.",
         "होल्डिंग्स हर तरह के फ़ंड के नमूने हैं, असली फ़ैक्टशीट नहीं।")


def _pair_answer(a_id: str, b_id: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    o = practice.overlap(a_id, b_id, universe.name, _own(ctx))
    A, B = o["a"], o["b"]
    pct, v = o["overlap_pct"], o["verdict"]
    top = o["shared"][:3]
    head = t(f"{_fname(a_id)} and {_fname(b_id)} hold {_pc(pct)}% of the same stocks: {_verdict_words(v, t)}.",
             f"{_fname(a_id)} और {_fname(b_id)} में {_pc(pct)}% शेयर एक जैसे हैं: {_verdict_words(v, t)}।")
    bullets = []
    if top:
        bullets.append(t("Biggest shared holdings: " + ", ".join(f"{r['name']} ({r['wa']}% and {r['wb']}%)" for r in top) + ".",
                         "सबसे बड़े साझा शेयर: " + ", ".join(f"{r['name']} ({r['wa']}% और {r['wb']}%)" for r in top) + "।"))
    else:
        bullets.append(t("They share no stocks, so holding both does spread your money.", "इनमें कोई शेयर साझा नहीं है, इसलिए दोनों रखने से पैसा सचमुच बँटता है।"))
    if v != "green":
        bullets.append(t(f"Fees: {A['er']}% and {B['er']}% a year. On every ₹1 lakh, about {tools.inr(o['wasted_fee_per_lakh'])} a year pays for stocks you already hold through the other fund.",
                         f"फ़ीस: सालाना {A['er']}% और {B['er']}%। हर 1 लाख रुपये पर लगभग {tools.inr_hi(o['wasted_fee_per_lakh'])} साल में उन शेयरों के लिए जाते हैं जो दूसरे फ़ंड से पहले ही आपके पास हैं।"))
    if o.get("own_in_a", 0) or o.get("own_in_b", 0):
        bullets.append(t(f"Of what you hold directly, {o['own_in_a']}% is already inside {_fname(a_id)} and {o['own_in_b']}% inside {_fname(b_id)}.",
                         f"आपके सीधे शेयरों का {o['own_in_a']}% पहले से {_fname(a_id)} में और {o['own_in_b']}% {_fname(b_id)} में है।"))
    action = {"red": t("Ask whether you need both. Keeping one, ideally the cheaper, usually gives the same spread for one fee.",
                       "सोचिए कि क्या दोनों चाहिए। एक रखना, ख़ासकर सस्ता वाला, एक ही फ़ीस में लगभग वही बँटवारा देता है।"),
              "amber": t("Some duplication is normal. Check whether the second fund adds anything the first does not.",
                         "कुछ दोहराव सामान्य है। देखिए कि दूसरा फ़ंड कुछ नया जोड़ता है या नहीं।"),
              "green": t("These two complement each other.", "ये दोनों एक-दूसरे के पूरक हैं।")}[v]
    a = Answer(headline=head, bullets=bullets, action=action,
               detail=t("Overlap = sum over shared stocks of the smaller weight, as a share of the smaller fund. " + _DISC[0],
                        "ओवरलैप = साझा शेयरों के छोटे वज़न का जोड़, छोटे फ़ंड के हिस्से के रूप में। " + _DISC[1]),
               facts=[_fact(t("Overlap", "ओवरलैप"), f"{_pc(pct)}%", {"red": "bad", "amber": "warn", "green": "good"}[v]),
                      _fact(t("Shared stocks", "साझा शेयर"), str(o["shared_stocks"])),
                      _fact(t("Fees", "फ़ीस"), f"{A['er']}% / {B['er']}%")],
               table={"columns": [t("Stock", "शेयर"), _fname(a_id), _fname(b_id)],
                      "rows": [[r["name"], f"{r['wa']}%", f"{r['wb']}%"] for r in o["shared"][:8]]} if o["shared"] else None,
               visual={"page": "practice", "label": t("Open the overlap visual", "ओवरलैप का चित्र खोलें"), "params": {"a": a_id, "b": b_id}})
    a.data = {"overlap_pct": pct, "a": a_id, "b": b_id}
    return _done(a, ctx, _chips("fee", "overlap", "list"), "fund_overlap")


def _ranking_answer(ids: list[str], ctx: Ctx, title_en: str, title_hi: str) -> Answer:
    t = _t(ctx.lang)
    pairs = _ranked_pairs(ids)
    top = pairs[0]
    head = t(f"{title_en}: {_fname(top['a'])} and {_fname(top['b'])} overlap the most, at {_pc(top['pct'])}%.",
             f"{title_hi}: {_fname(top['a'])} और {_fname(top['b'])} सबसे ज़्यादा मिलते हैं, {_pc(top['pct'])}%।")
    a = Answer(headline=head,
               bullets=[t(f"{sum(1 for p in pairs if p['verdict'] == 'red')} pairs are mostly the same portfolio; "
                          f"{sum(1 for p in pairs if p['verdict'] == 'green')} pairs are genuinely different.",
                          f"{sum(1 for p in pairs if p['verdict'] == 'red')} जोड़ियाँ लगभग एक ही पोर्टफ़ोलियो हैं; "
                          f"{sum(1 for p in pairs if p['verdict'] == 'green')} जोड़ियाँ सचमुच अलग हैं।")],
               action=t("Ask about a specific pair for the shared stocks and the fee cost.", "किसी एक जोड़ी के बारे में पूछिए, साझा शेयर और फ़ीस का असर दिखाऊँगा।"),
               detail=_DISC[1] if ctx.lang == "hi" else _DISC[0],
               table={"columns": [t("Pair", "जोड़ी"), t("Overlap", "ओवरलैप"), t("Shared", "साझा")],
                      "rows": [[f"{_fname(p['a'])} + {_fname(p['b'])}", f"{_pc(p['pct'])}%", str(p["n"])] for p in pairs[:6]]},
               facts=[_fact(t("Highest overlap", "सबसे ज़्यादा"), f"{_pc(top['pct'])}%", "bad" if top["verdict"] == "red" else "warn")],
               visual={"page": "practice", "label": t("Open the overlap visual", "ओवरलैप का चित्र खोलें"), "params": {"a": top["a"], "b": top["b"]}})
    return _done(a, ctx, _chips("fee", "list", "overlap"), "fund_overlap")


def h_fund_overlap(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    named = find_funds(text)
    mine = my_funds(ctx.conn)
    wants_mine = bool(re.search(r"\bmy\b|\bi (own|hold|have)\b|\bmine\b|\bi'?m invested\b", text, re.I))
    if len(named) >= 3:
        return _ranking_answer(named, ctx, t("Among the funds you named", "आपके बताए फ़ंडों में"), "आपके बताए फ़ंडों में")
    if len(named) == 2:
        return _pair_answer(named[0], named[1], ctx)
    if wants_mine:
        if len(mine) >= 3:
            return _ranking_answer(mine, ctx, t("Among your funds", "आपके फ़ंडों में"), "आपके फ़ंडों में")
        if len(mine) == 2:
            return _pair_answer(mine[0], mine[1], ctx)
        have = ", ".join(_fname(f) for f in mine) or t("none yet", "अभी कोई नहीं")
        a = Answer(headline=t("I need at least two of your funds to compare. Tell me which ones you own.",
                              "तुलना के लिए मुझे आपके कम से कम दो फ़ंड चाहिए। बताइए कौन से आपके पास हैं।"),
                   bullets=[t(f"Saved so far: {have}.", f"अब तक सहेजे: {have}।"),
                            t("Say, for example: I own Large Cap Fund A and Bluechip Fund B.", "जैसे कहिए: मेरे पास Large Cap Fund A और Bluechip Fund B है।"),
                            t("I can only see the sample funds below, not real fund factsheets.", "मैं सिर्फ़ नीचे के नमूना फ़ंड देख सकता हूँ, असली फ़ैक्टशीट नहीं।")],
                   table={"columns": [t("Fund", "फ़ंड"), t("Type", "प्रकार"), t("Fee", "फ़ीस")],
                          "rows": [[f["name"].replace("Sample ", ""), f["kind"], f"{f['er']}%"] for f in practice.fund_list()]})
        return _done(a, ctx, _chips("list", "overlap"), "fund_overlap")
    if len(named) == 1:
        others = [f for f in practice.FUNDS if f != named[0]]
        pairs = sorted(({"b": b, "o": practice.overlap(named[0], b, universe.name)} for b in others), key=lambda r: -r["o"]["overlap_pct"])
        top = pairs[0]
        a = Answer(headline=t(f"{_fname(named[0])} overlaps most with {_fname(top['b'])}, at {_pc(top['o']['overlap_pct'])}%.",
                              f"{_fname(named[0])} सबसे ज़्यादा {_fname(top['b'])} से मिलता है, {_pc(top['o']['overlap_pct'])}%।"),
                   bullets=[t(f"It overlaps least with {_fname(pairs[-1]['b'])}, at {_pc(pairs[-1]['o']['overlap_pct'])}%.",
                              f"सबसे कम मेल {_fname(pairs[-1]['b'])} से है, {_pc(pairs[-1]['o']['overlap_pct'])}%।")],
                   detail=_DISC[1] if ctx.lang == "hi" else _DISC[0],
                   table={"columns": [t("Other fund", "दूसरा फ़ंड"), t("Overlap", "ओवरलैप")],
                          "rows": [[_fname(p["b"]), f"{_pc(p['o']['overlap_pct'])}%"] for p in pairs[:5]]},
                   facts=[_fact(t("Highest", "सबसे ज़्यादा"), f"{_pc(top['o']['overlap_pct'])}%")],
                   visual={"page": "practice", "label": t("Open the overlap visual", "ओवरलैप का चित्र खोलें"),
                           "params": {"a": named[0], "b": top["b"]}})
        return _done(a, ctx, _chips("fee", "list"), "fund_overlap")
    return _ranking_answer(list(practice.FUNDS), ctx, t("Across all the sample funds", "सभी नमूना फ़ंडों में"), "सभी नमूना फ़ंडों में")


def h_fund_list(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    fl = practice.fund_list()
    a = Answer(headline=t(f"I can compare {len(fl)} sample funds: index, active and sector.", f"मैं {len(fl)} नमूना फ़ंडों की तुलना कर सकता हूँ: इंडेक्स, एक्टिव और सेक्टर।"),
               bullets=[t("These are illustrative samples typical of each fund type. I cannot see real fund factsheets offline.",
                          "ये हर तरह के फ़ंड के नमूने हैं। मैं ऑफ़लाइन असली फ़ंड की फ़ैक्टशीट नहीं देख सकता।")],
               table={"columns": [t("Fund", "फ़ंड"), t("Type", "प्रकार"), t("Fee a year", "सालाना फ़ीस")],
                      "rows": [[f["name"].replace("Sample ", ""), f["kind"], f"{f['er']}%"] for f in fl]},
               visual={"page": "practice", "label": t("Open the overlap visual", "ओवरलैप का चित्र खोलें"), "params": {}})
    return _done(a, ctx, _chips("overlap", "fee"), "fund_list")


def h_fund_info(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    fid = find_funds(text)[0]
    f = practice.FUNDS[fid]
    rows = sorted(f["h"].items(), key=lambda kv: -kv[1])
    a = Answer(headline=t(f"{_fname(fid)} ({f['kind']}, fee {f['er']}% a year) puts most weight on {universe.name(rows[0][0])}, {rows[0][1]}%.",
                          f"{_fname(fid)} ({f['kind']}, सालाना फ़ीस {f['er']}%) सबसे ज़्यादा वज़न {universe.name(rows[0][0])} को देता है, {rows[0][1]}%।"),
               bullets=[t(f"Top three: " + ", ".join(f"{universe.name(k)} {v}%" for k, v in rows[:3]) + ".",
                          "शीर्ष तीन: " + ", ".join(f"{universe.name(k)} {v}%" for k, v in rows[:3]) + "।")],
               detail=_DISC[1] if ctx.lang == "hi" else _DISC[0],
               table={"columns": [t("Stock", "शेयर"), t("Weight", "वज़न")], "rows": [[universe.name(k), f"{v}%"] for k, v in rows[:8]]},
               facts=[_fact(t("Fee a year", "सालाना फ़ीस"), f"{f['er']}%"), _fact(t("Holdings shown", "दिखाए गए शेयर"), str(len(rows)))])
    return _done(a, ctx, _chips("overlap", "fee", "list"), "fund_info")


def h_my_funds_add(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    ids = find_funds(text)
    for fid in ids:
        set_my_fund(ctx.conn, fid, True)
    mine = my_funds(ctx.conn)
    a = Answer(headline=t("Saved: " + ", ".join(_fname(f) for f in ids) + ".", "सहेज लिया: " + ", ".join(_fname(f) for f in ids) + "।"),
               bullets=[t("Your funds: " + ", ".join(_fname(f) for f in mine) + ".", "आपके फ़ंड: " + ", ".join(_fname(f) for f in mine) + "।")],
               action=t("Ask which of your funds overlap." if len(mine) >= 2 else "Add another fund and I can compare them.",
                        "पूछिए कि आपके कौन से फ़ंड मिलते हैं।" if len(mine) >= 2 else "एक और फ़ंड जोड़िए, मैं तुलना कर दूँगा।"))
    return _done(a, ctx, [("Which of my mutual funds overlap?", "मेरे कौन से म्यूचुअल फ़ंड आपस में मिलते हैं?"), _FU["fee"]], "my_funds")


def h_my_funds_remove(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    ids = find_funds(text)
    if not ids or re.search(r"\b(all|everything|clear)\b", text, re.I):
        ids = my_funds(ctx.conn)
    for fid in ids:
        set_my_fund(ctx.conn, fid, False)
    mine = my_funds(ctx.conn)
    a = Answer(headline=t("Removed: " + (", ".join(_fname(f) for f in ids) or "nothing") + ".", "हटा दिया: " + (", ".join(_fname(f) for f in ids) or "कुछ नहीं") + "।"),
               bullets=[t("Your funds now: " + (", ".join(_fname(f) for f in mine) or "none") + ".", "अब आपके फ़ंड: " + (", ".join(_fname(f) for f in mine) or "कोई नहीं") + "।")])
    return _done(a, ctx, _chips("list", "overlap"), "my_funds")


def h_my_funds_show(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    mine = my_funds(ctx.conn)
    if not mine:
        a = Answer(headline=t("You have not told me about any funds yet.", "आपने अभी तक कोई फ़ंड नहीं बताया है।"),
                   action=t("Say, for example: I own Large Cap Fund A and Bluechip Fund B.", "जैसे कहिए: मेरे पास Large Cap Fund A और Bluechip Fund B है।"))
        return _done(a, ctx, _chips("list"), "my_funds")
    a = Answer(headline=t(f"You have told me about {len(mine)} fund{'s' if len(mine) != 1 else ''}.", f"आपने मुझे {len(mine)} फ़ंड बताए हैं।"),
               table={"columns": [t("Fund", "फ़ंड"), t("Fee", "फ़ीस")], "rows": [[_fname(f), f"{practice.FUNDS[f]['er']}%"] for f in mine]})
    return _done(a, ctx, [("Which of my mutual funds overlap?", "मेरे कौन से म्यूचुअल फ़ंड आपस में मिलते हैं?"), _FU["fee"]], "my_funds")


def h_fund_vs_direct(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    ids = find_funds(text) or my_funds(ctx.conn)
    if not ids:
        a = Answer(headline=t("Tell me which fund to check your direct holdings against.", "बताइए किस फ़ंड से आपके सीधे शेयरों की तुलना करूँ।"),
                   action=t("For example: which of my stocks are already inside Nifty 50 Index Fund?", "जैसे: मेरे कौन से शेयर पहले से Nifty 50 Index Fund में हैं?"))
        return _done(a, ctx, _chips("list"), "fund_vs_direct")
    own = _own(ctx)
    tot = sum(own.values()) or 1.0
    rows, best = [], None
    for fid in ids:
        h = practice.FUNDS[fid]["h"]
        inside = [(tk, v) for tk, v in own.items() if tk in h]
        share = sum(v for _, v in inside) / tot * 100
        rows.append([_fname(fid), f"{share:.0f}%", ", ".join(universe.name(tk) for tk, _ in sorted(inside, key=lambda x: -x[1])[:4]) or "-"])
        best = best or (fid, share)
    fid, share = best
    a = Answer(headline=t(f"{share:.0f}% of what you hold directly is already inside {_fname(fid)}.", f"आपके सीधे शेयरों का {share:.0f}% पहले से {_fname(fid)} में है।"),
               bullets=[t("Buying that fund on top means paying its fee to own more of the same companies.", "उस फ़ंड को ऊपर से ख़रीदना यानी उन्हीं कंपनियों को और ख़रीदने के लिए उसकी फ़ीस देना।")],
               detail=_DISC[1] if ctx.lang == "hi" else _DISC[0],
               table={"columns": [t("Fund", "फ़ंड"), t("Your direct stocks inside", "आपके शेयर जो अंदर हैं"), t("Which", "कौन से")], "rows": rows},
               facts=[_fact(t("Already inside", "पहले से अंदर"), f"{share:.0f}%", "warn" if share > 40 else "")])
    return _done(a, ctx, _chips("overlap", "fee"), "fund_vs_direct")


# ---- fees ------------------------------------------------------------------------------
_RET_CUE = re.compile(r"(return|growth|grow|earn|cagr|xirr|gain|appreciat|yield)", re.I)


def _parse_fee(text: str) -> dict[str, Any]:
    q = tools.quantities(text)
    pcts = q["pct"]
    fee_vals, gross = [], None
    for p in pcts:
        around = text[max(0, int(p["at"]) - 30):int(p["at"]) + 40]
        if re.search(r"(fee|expense|ter\b|charge|commission|plan|brokerage|cost)", around, re.I) and not re.search(r"(return|growth|earn)\w*\s*(of|at|is|=)?\s*$", text[max(0, int(p["at"]) - 20):int(p["at"])], re.I):
            fee_vals.append(p["v"])
        elif _RET_CUE.search(text[max(0, int(p["at"]) - 28):int(p["at"])]) or _RET_CUE.search(text[int(p["at"]):int(p["at"]) + 30]):
            gross = p["v"]
        else:
            fee_vals.append(p["v"])
    roles = tools.money_roles(text)
    years = int(q["years"][0]["v"]) if q["years"] else None
    return {"fees": fee_vals, "gross": gross, "lump": roles["lump"], "monthly": roles["monthly"], "years": years}


def h_fee_drag(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    p = _parse_fee(text)
    fees = sorted(p["fees"], reverse=True)
    hi_fee = fees[0] if fees else 2.0
    lo_fee = fees[1] if len(fees) > 1 else (0.2 if hi_fee > 0.2 else 0.0)
    years = p["years"] or 20
    lump, monthly = p["lump"], p["monthly"]
    assumed = []
    if lump is None and monthly is None:
        lump = 100000.0
        assumed.append(t("₹1 lakh invested once", "एक बार 1 लाख रुपये का निवेश"))
    gross = p["gross"] if p["gross"] is not None else 12.0
    if p["gross"] is None:
        assumed.append(t("12% a year before fees", "फ़ीस से पहले सालाना 12%"))
    if not fees:
        assumed.append(t("a 2% fee against a 0.2% index fund", "2% फ़ीस की तुलना 0.2% के इंडेक्स फ़ंड से"))
    elif len(fees) == 1:
        assumed.append(t(f"compared with a {lo_fee:g}% fund", f"{lo_fee:g}% वाले फ़ंड से तुलना"))
    r = tools.fee_drag(lump or 0.0, monthly or 0.0, years, gross, hi_fee, lo_fee)
    money = tools.inr_hi if ctx.lang == "hi" else tools.inr
    inv_txt = ((t(f"{tools.inr(lump)} once", f"{tools.inr_hi(lump)} एक बार") if lump else "") +
               (" + " if lump and monthly else "") +
               (t(f"{tools.inr(monthly)} a month", f"{tools.inr_hi(monthly)} हर महीने") if monthly else ""))
    a = Answer(headline=t(f"A {hi_fee:g}% fee costs you about {money(r['lost_vs_low'])} over {years} years, compared with a {lo_fee:g}% fund.",
                          f"{hi_fee:g}% की फ़ीस {years} साल में {lo_fee:g}% वाले फ़ंड की तुलना में आपको लगभग {money(r['lost_vs_low'])} का नुक़सान कराती है।"),
               bullets=[t(f"Investing {inv_txt}: you would end with {money(r['final_high'])} at the higher fee and {money(r['final_low'])} at the lower one.",
                          f"निवेश {inv_txt}: ज़्यादा फ़ीस पर अंत में {money(r['final_high'])} और कम फ़ीस पर {money(r['final_low'])} मिलेंगे।"),
                        t(f"The fee quietly takes {r['share_of_gain_lost'] * 100:.0f}% of the growth you would otherwise have had.",
                          f"फ़ीस चुपचाप उस बढ़ोतरी का {r['share_of_gain_lost'] * 100:.0f}% खा जाती है जो वरना आपको मिलती।")],
               action=t("Check the yearly fee (expense ratio) of every fund you hold; direct and index funds are usually the cheapest.",
                        "हर फ़ंड की सालाना फ़ीस (एक्सपेंस रेशियो) देखिए; डायरेक्ट और इंडेक्स फ़ंड आम तौर पर सबसे सस्ते होते हैं।"),
               detail=t("Assumes " + "; ".join(assumed) + ". Net return = gross - fee, compounded monthly, contributions at month end. A projection, not a promise.",
                        "मान्यताएँ: " + "; ".join(assumed) + "। शुद्ध रिटर्न = सकल - फ़ीस, मासिक चक्रवृद्धि। यह अनुमान है, वादा नहीं।") if assumed else
                      t("Net return = gross - fee, compounded monthly. A projection, not a promise.", "शुद्ध रिटर्न = सकल - फ़ीस। यह अनुमान है, वादा नहीं।"),
               facts=[_fact(t(f"At {hi_fee:g}%", f"{hi_fee:g}% पर"), tools.inr(r["final_high"]), "bad"),
                      _fact(t(f"At {lo_fee:g}%", f"{lo_fee:g}% पर"), tools.inr(r["final_low"]), "good"),
                      _fact(t("Lost to the fee", "फ़ीस में गया"), tools.inr(r["lost_vs_low"]), "bad")],
               table={"columns": [t("Year", "साल"), t("Invested", "लगाया"), t(f"At {hi_fee:g}%", f"{hi_fee:g}% पर"), t(f"At {lo_fee:g}%", f"{lo_fee:g}% पर"), t("Difference", "अंतर")],
                      "rows": [[str(pt["year"]), tools.inr(r["principal"] + r["monthly"] * pt["year"] * 12), tools.inr(pt["high"]), tools.inr(pt["low"]), tools.inr(pt["low"] - pt["high"])]
                               for pt in r["points"] if pt["year"] in sorted({max(1, years // 4), max(1, years // 2), max(1, 3 * years // 4), years})]},
               visual={"page": "practice", "label": t("Open the fee slider", "फ़ीस स्लाइडर खोलें"),
                       "params": {"fee": hi_fee, "low": lo_fee, "years": years, "lump": lump or 0, "monthly": monthly or 0, "gross": gross}})
    a.data = {"fee_drag": r}
    return _done(a, ctx, _chips("overlap", "emerg", "goal"), "fee_drag")


# ---- emergency -------------------------------------------------------------------------
def h_emergency(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    q = tools.quantities(text)
    money = [m["v"] for m in q["money"]]
    low = text.lower()
    expenses = cash = invest = income = None
    for m in q["money"]:
        ctx_before = m["ctx"]
        after = low[int(m["at"]):int(m["at"]) + 40]
        if re.search(r"(spend|expense|cost|burn|need|bills|rent|living|outgo)", ctx_before) or re.search(r"^[^.,;]{0,18}(a month|per month|monthly|/month|every month)", after):
            if expenses is None:
                expenses = m["v"]
                continue
        if re.search(r"(income|earn|rent from|salary of|partner)", ctx_before) and income is None and expenses is not None:
            income = m["v"]
            continue
        if re.search(r"(invest|stocks?|shares?|mutual|mf|portfolio)", ctx_before) and invest is None:
            invest = m["v"]
            continue
        if cash is None:
            cash = m["v"]
    if expenses is None and len(money) >= 2:
        # "I have 3 lakh and spend 40k": the smaller figure is the monthly one
        expenses = min(money)
        cash = max(money)
    own_cash = float(getattr(ctx.portfolio, "cash", 0.0) or 0.0)
    used_portfolio_cash = False
    if cash is None and own_cash > 0 and expenses:
        cash, used_portfolio_cash = own_cash, True
    if expenses is None or cash is None:
        a = Answer(headline=t("Tell me your monthly spending and the cash you have, and I will show how long it lasts.",
                              "अपना मासिक ख़र्च और नक़द रक़म बताइए, मैं दिखाऊँगा कि वह कितने महीने चलेगी।"),
                   bullets=[t("For example: I have 3 lakh saved and spend 40000 a month.", "जैसे: मेरे पास 3 लाख रुपये हैं और मैं 40000 हर महीने ख़र्च करता हूँ।")],
                   visual={"page": "practice", "label": t("Open the emergency meter", "इमरजेंसी मीटर खोलें"), "params": {}})
        return _done(a, ctx, [_FU["emerg"]], "emergency")
    r = tools.emergency(cash, expenses, invest or 0.0, 20.0, income or 0.0)
    runway = r["runway_cash"]
    band = r["band"]
    money_f = tools.inr_hi if ctx.lang == "hi" else tools.inr
    if runway is None:
        head = t("Your other income covers your spending, so your savings are not being drawn down.", "आपकी दूसरी आमदनी आपके ख़र्च को पूरा करती है, इसलिए बचत घट नहीं रही।")
    else:
        head = t(f"{money_f(cash)} lasts about {runway:.1f} months at {money_f(r['burn'])} a month.",
                 f"{money_f(cash)} लगभग {runway:.1f} महीने चलेंगे, अगर हर महीने {money_f(r['burn'])} ख़र्च हो।")
    bullets = []
    if runway is not None:
        bullets.append({"red": t("That is under three months: a real risk if your income stopped.", "यह तीन महीने से कम है: आमदनी रुकने पर यह सचमुच जोखिम है।"),
                        "amber": t("That is a reasonable start, but the usual target is six months.", "यह एक ठीक शुरुआत है, पर आम लक्ष्य छह महीने का है।"),
                        "green": t("That meets the usual six-month target.", "यह छह महीने के आम लक्ष्य को पूरा करता है।")}[band])
    if r["shortfall"] > 0:
        bullets.append(t(f"Six months of spending is {money_f(r['target_amount'])}. You are {money_f(r['shortfall'])} short; saving {money_f(r['save_per_month_12'])} a month closes that in a year.",
                         f"छह महीने का ख़र्च {money_f(r['target_amount'])} है। आपके पास {money_f(r['shortfall'])} कम है; हर महीने {money_f(r['save_per_month_12'])} बचाने से साल भर में पूरा हो जाएगा।"))
    if invest:
        bullets.append(t(f"Counting {tools.inr(invest)} of investments after a 20% fall, it would stretch to about {r['runway_all']:.1f} months, but selling into a crash locks in losses.",
                         f"{tools.inr_hi(invest)} के निवेश को 20% गिरावट के बाद जोड़ें तो यह लगभग {r['runway_all']:.1f} महीने चलेगा, पर गिरावट में बेचने से नुक़सान पक्का हो जाता है।"))
    if used_portfolio_cash:
        bullets.append(t("I used the cash in your portfolio as your savings.", "मैंने आपके पोर्टफ़ोलियो के नक़द को आपकी बचत माना है।"))
    a = Answer(headline=head, bullets=bullets,
               action=t("Keep this money somewhere easy to reach, not in shares.", "यह पैसा आसानी से मिलने वाली जगह रखिए, शेयरों में नहीं।"),
               detail=t("Months of cover = cash / (monthly spending - other income). Target: 6 months.", "कवर के महीने = नक़द / (मासिक ख़र्च - दूसरी आमदनी)। लक्ष्य: 6 महीने।"),
               facts=[_fact(t("Lasts", "चलेगा"), "∞" if runway is None else t(f"{runway:.1f} months", f"{runway:.1f} महीने"), {"green": "good", "amber": "warn", "red": "bad"}[band]),
                      _fact(t("Target (6 months)", "लक्ष्य (6 महीने)"), tools.inr(r["target_amount"])),
                      _fact(t("Shortfall", "कमी"), tools.inr(r["shortfall"]), "bad" if r["shortfall"] else "good")],
               table={"columns": [t("After", "बाद में"), t("Cash left", "बचा नक़द")],
                      "rows": [[_mo(pt['month'], ctx.lang == "hi"), tools.inr(pt["cash"])] for pt in r["points"] if pt["month"] in (0, 1, 3, 6, 9, 12)][:6]},
               visual={"page": "practice", "label": t("Open the emergency meter", "इमरजेंसी मीटर खोलें"),
                       "params": {"cash": cash, "exp": expenses, "inc": income or 0, "inv": invest or 0}})
    a.data = {"emergency": r}
    return _done(a, ctx, _chips("fee", "goal", "xray"), "emergency")


# ---- goal ------------------------------------------------------------------------------
def h_goal(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    q = tools.quantities(text)
    roles = tools.money_roles(text)
    years = int(q["years"][0]["v"]) if q["years"] else None
    monthly = roles["monthly"]
    target = None
    for m in q["money"]:
        if monthly is not None and abs(m["v"] - monthly) < 1e-6:
            continue
        if re.search(r"(reach|goal|target|want|need|get to|become|build|accumulate|hit|corpus|worth)", m["ctx"]) or target is None:
            target = m["v"]
    if target is None or not ctx.portfolio.positions:
        a = Answer(headline=t("Tell me the goal amount and how many years you have, and I will show the range of outcomes.",
                              "लक्ष्य की रक़म और कितने साल हैं, यह बताइए। मैं नतीजों की रेंज दिखाऊँगा।") if ctx.portfolio.positions else
                   t("Add your holdings first so I can use how your portfolio has actually behaved.", "पहले अपने शेयर जोड़िए, ताकि मैं देख सकूँ कि आपका पोर्टफ़ोलियो असल में कैसे चला है।"),
                   bullets=[t("For example: will 10000 a month reach 50 lakh in 15 years?", "जैसे: क्या 10000 महीने से 15 साल में 50 लाख बनेंगे?")])
        return _done(a, ctx, [_FU["goal"]], "goal")
    years = years or 10
    monthly = monthly if monthly is not None else 10000.0
    res = goal_mod.fan(ctx.pit, ctx.portfolio, ctx.prices, monthly, years, target)
    if not res.get("ok"):
        a = Answer(headline=t("I do not have enough price history for this portfolio to project a range.", "इस पोर्टफ़ोलियो के लिए अनुमान लगाने लायक़ दामों का इतिहास नहीं है।"))
        return _done(a, ctx, _chips("xray"), "goal")
    last = res["points"][-1]
    pt = res["prob_target"] * 100
    band = "good" if pt >= 70 else "warn" if pt >= 40 else "bad"
    mf = tools.inr_hi if ctx.lang == "hi" else tools.inr
    a = Answer(headline=t(f"In {pt:.0f}% of simulated futures you reach {mf(target)} in {years} years.", f"{years} साल में {mf(target)} तक पहुँचने की संभावना {pt:.0f}% सिमुलेशनों में है।"),
               bullets=[t(f"Typical result: {mf(last['p50'])}. Bad case (1 in 10): {mf(last['p10'])}. Good case (1 in 10): {mf(last['p90'])}.",
                          f"सामान्य नतीजा: {mf(last['p50'])}। बुरा हाल (10 में से 1): {mf(last['p10'])}। अच्छा हाल (10 में से 1): {mf(last['p90'])}।"),
                        t(f"You would put in {mf(res['invested'])} in total, including today's portfolio and {mf(monthly)} a month.",
                          f"आप कुल {mf(res['invested'])} लगाएँगे, आज के पोर्टफ़ोलियो और हर महीने {mf(monthly)} सहित।")],
               action=t("Test a worse market with the 'returns are worse by' slider.", "'रिटर्न इतने कम हों तो' स्लाइडर से ख़राब बाज़ार आज़माइए।"),
               detail=t(f"Built from {res['history_years']} years of this portfolio's own history, mostly a rising market, so the future can be worse. A range, not a forecast.",
                        f"इस पोर्टफ़ोलियो के {res['history_years']} साल के इतिहास से बना, जिसमें बाज़ार ज़्यादातर चढ़ा है, इसलिए भविष्य इससे बुरा हो सकता है। रेंज है, पूर्वानुमान नहीं।"),
               facts=[_fact(t("Chance of reaching", "पहुँचने की संभावना"), f"{pt:.0f}%", band), _fact(t("Typical", "सामान्य"), tools.inr(last["p50"])),
                      _fact(t("Bad case", "बुरा हाल"), tools.inr(last["p10"]), "warn")],
               visual={"page": "learn", "label": t("Open the goal chart", "लक्ष्य चार्ट खोलें"), "params": {"monthly": monthly, "years": years, "target": target}})
    a.data = {"goal": {k: v for k, v in res.items() if k != "points"}}
    return _done(a, ctx, _chips("panic", "emerg", "fee"), "goal")


# ---- panic -----------------------------------------------------------------------------
def h_panic(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    low = text.lower()
    key = "adani_2023" if re.search(r"adani|2023", low) else "rate_shock_2022" if re.search(r"2022|rate", low) else "covid"
    if not ctx.portfolio.positions:
        a = Answer(headline=t("Add your holdings first, then I can replay a crash on them.", "पहले अपने शेयर जोड़िए, फिर मैं उन पर गिरावट दोहराकर दिखाऊँगा।"))
        return _done(a, ctx, _chips("help"), "panic")
    r = panic_mod.replay(ctx.pit, ctx.portfolio, ctx.prices, key)
    if not r.get("points"):
        a = Answer(headline=t("I do not have price history for that period.", "उस दौर का दामों का इतिहास मेरे पास नहीं है।"))
        return _done(a, ctx, _chips("help"), "panic")
    mf = tools.inr_hi if ctx.lang == "hi" else tools.inr
    trough, end, start = r["trough_value"], r["end_value"], r["start_value"]
    diff = end - trough
    name = {"covid": t("the Covid crash", "कोविड की गिरावट"), "rate_shock_2022": t("the 2022 rate shock", "2022 की ब्याज दरों की मार"),
            "adani_2023": t("the January 2023 selloff", "जनवरी 2023 की बिकवाली")}[key]
    if diff > 0:
        head = t(f"Selling at the lowest point of {name} would have locked in {mf(trough)}; holding to the end of the window was worth {mf(end)}, {mf(diff)} more.",
                 f"{name} के सबसे निचले बिंदु पर बेचने से {mf(trough)} पक्का हो जाता; खिड़की के अंत तक रुकने पर {mf(end)} होता, यानी {mf(diff)} ज़्यादा।")
    else:
        head = t(f"In {name}, selling at the low would have given {mf(trough)} and holding ended at {mf(end)}: holding did not win this time.",
                 f"{name} में निचले बिंदु पर बेचने पर {mf(trough)} मिलता और रुकने पर {mf(end)}: इस बार रुकना जीता नहीं।")
    a = Answer(headline=head,
               bullets=[t(f"Your portfolio was worth {mf(start)} at the start of that window and {mf(trough)} at its lowest, a fall of {(1 - trough / start) * 100:.0f}%.",
                          f"उस दौर की शुरुआत में पोर्टफ़ोलियो {mf(start)} का था और सबसे निचले बिंदु पर {mf(trough)} का, यानी {(1 - trough / start) * 100:.0f}% की गिरावट।")],
               action=t("History is not a promise: some falls keep going. Decide your limit before a crash, not during it.", "इतिहास वादा नहीं है: कुछ गिरावटें चलती रहती हैं। सीमा गिरावट से पहले तय कीजिए, गिरावट के बीच नहीं।"),
               detail=t("Your current holdings valued at real closing prices through that window.", "आपके मौजूदा शेयर उस दौर के असली बंद भावों पर।"),
               facts=[_fact(t("If sold at the low", "सबसे नीचे बेचा"), tools.inr(trough), "bad"), _fact(t("If held", "रुके रहे"), tools.inr(end), "good" if diff > 0 else "warn")],
               visual={"page": "protect", "label": t("Open the replay", "रीप्ले खोलें"), "params": {"episode": key}})
    return _done(a, ctx, _chips("goal", "emerg", "xray"), "panic")


# ---- digest ----------------------------------------------------------------------------
def h_digest(text: str, ctx: Ctx) -> Answer:
    import datetime
    t = _t(ctx.lang)
    d = digest_mod.build(ctx.pit, ctx.portfolio, ctx.prices, ctx.policy, ctx.lang, datetime.date.today().isocalendar()[1])
    secs = d["sections"]
    a = Answer(headline=d["headline"], bullets=[f"{s['title']}: {s['text']}" for s in secs[1:]],
               speech=d["spoken"],
               action=t("Press play on the Practice page to hear it as a one-minute briefing.", "एक मिनट की ब्रीफ़िंग सुनने के लिए Practice पेज पर प्ले दबाइए।"),
               visual={"page": "practice", "label": t("Open the weekly digest", "साप्ताहिक सार खोलें"), "params": {}})
    return _done(a, ctx, _chips("xray", "overlap", "scam"), "digest")


# ---- scams -----------------------------------------------------------------------------
def _scam_scenario(text: str) -> str:
    """Which rehearsal fits what was described: a police/arrest call, an investment tip, else the bank/KYC call."""
    if re.search(r"arrest|police|cbi|customs|court|parcel|warrant|digital", text, re.I):
        return "police"
    if re.search(r"invest|tip|telegram|whatsapp|group|return|profit|trading|stock", text, re.I):
        return "invest"
    return "kyc"


def h_scam_help(text: str, ctx: Ctx) -> Answer:
    """A scam that has not cost anything yet: the script that matches what was described, what is true instead,
    and the first steps for THAT scam (not one generic reply for every mention of the word)."""
    from backend import scams
    t = _t(ctx.lang)
    lg = "hi" if ctx.lang == "hi" else "en"
    lost = re.search(r"\b(lost|gave|shared|paid|sent|transferred|already|fell for|victim|scammed|cheated|defrauded|duped|debited|deducted|stolen|taken from my account|told (him|her|them)|read out)\b", text, re.I)
    if lost:
        return h_scam_recovery(text, ctx)
    sc = scams.pick(text)
    says, truth = sc["says"][lg], sc["truth"][lg]
    rows = [[a, b] for a, b in zip(says, truth)]
    a = Answer(headline=sc["head"][lg],
               bullets=[f"{i + 1}. {x}" for i, x in enumerate(sc["now"][lg])],
               action=sc["never"][lg],
               table={"columns": [t("What they say", "वे क्या कहते हैं"), t("What is actually true", "असल में क्या सच है")], "rows": rows},
               facts=[_fact(t("Helpline", "हेल्पलाइन"), "1930", "good"), _fact(t("Report online", "ऑनलाइन शिकायत"), "cybercrime.gov.in")],
               detail=t("Matched to the tells in your description. If money or a code has already gone, say so and I will give the first-hour steps instead.",
                        "आपके बताए संकेतों से मिलाया गया। पैसा या कोड जा चुका हो तो बताइए, मैं पहले घंटे के क़दम दूँगा।"),
               visual={"page": "practice", "label": t("Rehearse this call", "इस कॉल का अभ्यास करें"), "params": {"scenario": sc["rehearse"]}})
    a.data = {"scam": sc["id"]}
    return _done(a, ctx, _chips("scam", "digest", "help"), "scam_help")


def h_scam_recovery(text: str, ctx: Ctx) -> Answer:
    """After a scam: the ordered steps for what happened, the helpline and portal, and the coach."""
    from backend import recovery
    t = _t(ctx.lang)
    hi = ctx.lang == "hi"
    kind = recovery.guess_type(text)
    pl = recovery.plan(kind, ctx.lang)
    steps = pl["steps"]
    q = tools.quantities(text)["money"]
    amount = q[0]["v"] if q else None
    bullets = [f"{s['n']}. {s['title']}" for s in steps[:3]]
    bullets.append(t("Helpline 1930 and cybercrime.gov.in: report within the first hour if you can.",
                     "हेल्पलाइन 1930 और cybercrime.gov.in: हो सके तो पहले घंटे में शिकायत कीजिए।"))
    a = Answer(headline=t("Act in this order: the first hour matters most.", "इसी क्रम में कदम उठाइए: पहला घंटा सबसे अहम है।"),
               bullets=bullets,
               action=t("Do not send any more money to 'recover' it: that is a second scam. None of this is your fault.",
                        "पैसा वापस पाने के नाम पर और पैसे मत भेजिए: वह दूसरी ठगी है। इसमें आपकी कोई ग़लती नहीं है।"),
               detail=t("A checklist built from the situation you described. It cannot promise a refund; it puts the steps that improve your chances in the right order.",
                        "आपकी बताई स्थिति से बनी चेकलिस्ट। यह रिफ़ंड का वादा नहीं कर सकती; यह उन क़दमों को सही क्रम में रखती है जिनसे आपकी संभावना बढ़ती है।"),
               facts=[_fact(t("Helpline", "हेल्पलाइन"), "1930", "good"), _fact(t("Portal", "पोर्टल"), "cybercrime.gov.in"),
                      _fact(t("Steps", "क़दम"), str(len(steps)))],
               table={"columns": [t("Step", "क़दम"), t("Do it", "कब तक")],
                      "rows": [[s["title"], recovery.when_label(s["mins"], hi)] for s in steps]},
               visual={"page": "protect", "label": t("Open the recovery coach", "रिकवरी कोच खोलें"),
                       "params": {"type": kind, **({"amount": amount} if amount else {})}})
    a.data = {"recovery_type": kind}
    return _done(a, ctx, _chips("scam", "help"), "scam_recovery")


def _tip_body(text: str) -> str:
    m = re.search(r"[:\n]\s*(.{25,})$", text, re.S)
    q = re.search(r"[\"“'](.{25,})[\"”']", text, re.S)
    return (q.group(1) if q else m.group(1) if m else text).strip()


def h_tip_scan(text: str, ctx: Ctx) -> Answer:
    from analysis import scanner
    t = _t(ctx.lang)
    body = _tip_body(text)
    if len(body.split()) < 6:
        a = Answer(headline=t("Paste the tip itself and I will check it for scam tactics and for claims I can test.", "टिप ख़ुद चिपकाइए, मैं ठगी की चालें और जाँच लायक़ दावे परखूँगा।"),
                   bullets=[t("For example: Is this tip legit: SURE SHOT! TCS profit up 300%, target 9000, join my VIP group.", "जैसे: क्या यह टिप सही है: पक्की टिप! TCS का मुनाफ़ा 300% बढ़ा, टारगेट 9000, मेरे VIP ग्रुप में जुड़ें।")])
        return _done(a, ctx, _chips("scam"), "tip_scan")
    from analysis import tipminds
    r = tipminds.analyse_sync(ctx.pit, body)
    return tip_answer(r, ctx)


_ACTION_TITLE = {"IGNORE_REPORT": ("Do not act. Report it.", "कार्रवाई मत कीजिए। शिकायत कीजिए।"), "IGNORE": ("Do not act on it.", "इस पर कार्रवाई मत कीजिए।"),
                 "VERIFY": ("Verify before anything else.", "पहले जाँच कीजिए।"), "RESEARCH": ("Research it yourself first.", "पहले ख़ुद शोध कीजिए।"), "INFO": ("Nothing to act on.", "कार्रवाई की कोई बात नहीं।")}
_STANCE = {"against": ("Against", "ख़िलाफ़"), "for": ("For", "पक्ष में"), "neutral": ("Neutral", "तटस्थ"), "unknown": ("Cannot tell", "पता नहीं")}


def tip_answer(r: dict, ctx: Ctx) -> Answer:
    """The tip verdict, in the order a person needs it: what the tip is suggesting, what the analysts found, what to do."""
    t = _t(ctx.lang)
    if r.get("empty"):
        a = Answer(headline=t("I could not find anything to check in that.", "उसमें जाँचने लायक़ कुछ नहीं मिला।"))
        return _done(a, ctx, _chips("scam"), "tip_scan")
    band = r.get("tone", "")
    rd = r.get("reading", {})
    agents = r["agents"]
    verdict_en = r["verdict"].capitalize() if False else r["verdict"]
    reading = rd.get("summary") or t("It suggests a " + (rd.get("action") or "action") + " on " + (", ".join(rd.get("instruments", [])) or "a stock that was not identified") + ".",
                                    "यह " + (", ".join(rd.get("instruments", [])) or "किसी अनजान शेयर") + " पर कदम उठाने को कहता है।")
    a = Answer(headline=t(f"{verdict_en}. Scam likelihood {r['scam_likelihood']}%, backing for the idea {r['reliability']}/100.",
                          f"{ {'red': 'ठगी या अविश्वसनीय', 'amber': 'अप्रमाणित', 'green': 'कुछ आधार दिखा'}.get(band, '') }: ठगी की संभावना {r['scam_likelihood']}%, विचार का आधार {r['reliability']}/100।"),
               bullets=[t(f"What it suggests: {reading}", f"यह क्या सुझाता है: {reading}")] +
                       [f"{f['agent']} ({_STANCE[f['stance']][1 if ctx.lang == 'hi' else 0]}): {f['headline']}" for f in agents if f["stance"] != "unknown" or f["agent"] in ("Feasibility", "Fundamentals")][:7] +
                       [t("Wording found: " + f["label"].lower() + f" ({f['quote']})", "भाषा में मिला: " + FLAG_LABEL_HI.get(f["code"], f["label"]) + f" ({f['quote']})") for f in r["flags"][:3] if f["code"] in FLAG_LABEL_HI and f["code"] not in ("NO_BASIS", "CHASING", "FAST_TARGET")] +
                       [f"{i + 1}. {x}" for i, x in enumerate(r["steps"][:4])],
               action=t(_ACTION_TITLE[r["action"]][0] + " " + (r["steps"][0] if r["steps"] else ""), _ACTION_TITLE[r["action"]][1]),
               table={"columns": [t("Specialist", "विशेषज्ञ"), t("Finding", "निष्कर्ष"), t("Why", "क्यों")],
                      "rows": [[f"{f['agent']}: {_STANCE[f['stance']][0]}", f["headline"], f["reasoning"]] for f in agents]},
               facts=[_fact(t("Scam likelihood", "ठगी की संभावना"), f"{r['scam_likelihood']}%", {"red": "bad", "amber": "warn", "green": "good"}.get(band, "")),
                      _fact(t("Backing for the idea", "विचार का आधार"), f"{r['reliability']}/100", "good" if r["reliability"] >= 60 else "warn" if r["reliability"] >= 30 else "bad"),
                      _fact(t("Read by", "समझा गया"), t("local model + rules", "लोकल मॉडल + नियम") if rd.get("how") == "model+rules" else t("rules", "नियम"))],
               detail=r.get("summary"),
               visual={"page": "protect", "label": t("Open the tip checker", "टिप जाँचक खोलें"), "params": {"tool": "tip"}})
    a.data = {"tip": {"action": r["action"], "scam": r["scam_likelihood"], "reliability": r["reliability"]}}
    return _done(a, ctx, _chips("scam", "panic", "help"), "tip_scan")


def h_ledger(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    v = ledger_mod.verify(ctx.conn)
    if v["ok"]:
        head = t(f"Your record is intact: {v['entries']} entr{'y' if v['entries'] == 1 else 'ies'}, none altered.", f"आपका रिकॉर्ड सुरक्षित है: {v['entries']} प्रविष्टियाँ, किसी में बदलाव नहीं।")
    else:
        head = t(f"Tampering detected at entry {v['broken_at']}. Everything after it cannot be trusted.", f"प्रविष्टि {v['broken_at']} पर छेड़छाड़ मिली। उसके बाद का कुछ भरोसेमंद नहीं।")
    a = Answer(headline=head,
               bullets=[t("Each trade stores a fingerprint of the one before it, so editing any past entry breaks every later one. The database also refuses edits and deletes.",
                          "हर लेन-देन पिछले का फ़िंगरप्रिंट रखता है, इसलिए कोई पुरानी प्रविष्टि बदलने से बाद की सारी टूट जाती हैं। डेटाबेस बदलाव और मिटाना भी नहीं मानता।")],
               facts=[_fact(t("Entries", "प्रविष्टियाँ"), str(v["entries"])), _fact(t("Status", "स्थिति"), t("Intact", "सुरक्षित") if v["ok"] else t("Broken", "टूटा"), "good" if v["ok"] else "bad")],
               visual={"page": "govern", "label": t("Try to tamper with it", "छेड़छाड़ करके देखें"), "params": {}})
    return _done(a, ctx, _chips("xray", "help"), "ledger")



# ---- "can I buy X?" --------------------------------------------------------------------
def _median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def h_buy_advice(text: str, ctx: Ctx) -> Answer:
    """Can I buy X? Two separate, plainly-labelled facts: is the business sound (its numbers against
    sector peers) and does it fit THIS portfolio (room under the person's own limits). Never a price
    prediction, never an order; the decision is left to the person."""
    from backend.intents import resolve_ticker
    t = _t(ctx.lang)
    tk = resolve_ticker(text)
    if not tk:
        a = Answer(headline=t("Which company do you mean?", "आप किस कंपनी की बात कर रहे हैं?"),
                   bullets=[t("Name it, for example: can I buy HDFC Bank?", "नाम बताइए, जैसे: क्या मैं HDFC बैंक ले सकता हूँ?")])
        return _done(a, ctx, _chips("xray"), "should_buy")
    name, sector = universe.name(tk), universe.sector(tk)
    sec_label = universe.sector_label(sector)
    prices, pf, lim = ctx.prices, ctx.portfolio, ctx.policy.limits
    nav = pf.nav(prices) or 0.0
    price = prices.get(tk) or ctx.pit.last_close(tk) or 0.0

    # --- 1. the business, against its sector peers -------------------------------------
    def val(x: str, kind: str) -> float | None:
        s_ = ctx.pit.latest(x, kind)
        return s_.value_num if s_ and s_.value_num is not None else None
    peers = [x for x in universe.tickers() if x != tk and universe.sector(x) == sector]
    def vs_peers(kind: str) -> tuple[float | None, float | None]:
        return val(tk, kind), _median([v for v in (val(p, kind) for p in peers) if v is not None])
    pe, pe_m = vs_peers("pe_ratio")
    roe, roe_m = vs_peers("roe")
    mar, mar_m = vs_peers("profit_margin")
    good, weak, rows = [], [], []
    if pe:
        cheap = pe_m and pe < pe_m * 0.9
        dear = pe_m and pe > pe_m * 1.15
        rows.append([t("Price vs earnings (P/E)", "भाव बनाम कमाई (P/E)"), f"{pe:.1f}", f"{pe_m:.1f}" if pe_m else "–",
                     t("cheaper than peers", "साथियों से सस्ता") if cheap else t("dearer than peers", "साथियों से महँगा") if dear else t("in line", "बराबर")])
        (good if cheap else weak if dear else []).append(t(f"valued at {pe:.0f}x earnings vs about {pe_m:.0f}x for its sector peers",
                                                           f"भाव कमाई का {pe:.0f} गुना, जबकि सेक्टर के साथियों का लगभग {pe_m:.0f} गुना"))
    if roe is not None:
        strong, low = roe >= 0.15 or (roe_m and roe > roe_m * 1.1), roe < 0.10
        rows.append([t("Return on equity (ROE)", "इक्विटी पर रिटर्न (ROE)"), f"{roe*100:.1f}%", f"{roe_m*100:.1f}%" if roe_m else "–",
                     t("strong", "मज़बूत") if strong else t("weak", "कमज़ोर") if low else t("average", "औसत")])
        (good if strong else weak if low else []).append(t(f"earns {roe*100:.0f}% a year on shareholders' money", f"शेयरधारकों के पैसे पर साल में {roe*100:.0f}% कमाती है"))
    if mar is not None:
        strong, low = bool(mar_m and mar > mar_m * 1.1), bool(mar_m and mar < mar_m * 0.8)
        rows.append([t("Profit margin", "मुनाफ़ा मार्जिन"), f"{mar*100:.1f}%", f"{mar_m*100:.1f}%" if mar_m else "–",
                     t("strong", "मज़बूत") if strong else t("weak", "कमज़ोर") if low else t("average", "औसत")])
        (good if strong else weak if low else []).append(t(f"keeps {mar*100:.0f} paise of profit from every rupee of sales", f"हर रुपये की बिक्री में से {mar*100:.0f} पैसे मुनाफ़ा बचता है"))
    if not rows:
        biz = t("I have no fundamentals saved for this company yet (run 'analyse' on it first).", "इस कंपनी के आँकड़े अभी सहेजे नहीं हैं (पहले इसका 'विश्लेषण' चलाइए)।")
        biz_tone = ""
    elif len(good) > len(weak):
        biz, biz_tone = t("Its numbers look healthy for its sector.", "सेक्टर के हिसाब से इसके आँकड़े अच्छे दिखते हैं।"), "good"
    elif len(weak) > len(good):
        biz, biz_tone = t("Its numbers look weaker than its sector.", "सेक्टर के हिसाब से इसके आँकड़े कमज़ोर दिखते हैं।"), "bad"
    else:
        biz, biz_tone = t("Its numbers look average for its sector.", "सेक्टर के हिसाब से इसके आँकड़े औसत हैं।"), "warn"

    # --- 2. the fit with YOUR portfolio ------------------------------------------------
    held_val = pf.positions.get(tk, 0) * price
    cur_w = held_val / nav if nav else 0.0
    sec_val = pf.sector_value(sector, prices, universe.sectors())
    sec_w = sec_val / nav if nav else 0.0
    room_pos = max(0.0, lim.max_position_pct * nav - held_val)
    room_sec = max(0.0, lim.max_sector_pct * nav - sec_val)
    room_cash = max(0.0, pf.cash - getattr(lim, "min_cash_pct", 0.0) * nav)
    room = min(room_pos, room_sec, room_cash)
    binding = ("position" if room == room_pos else "sector" if room == room_sec else "cash")
    inr_ = tools.inr_hi if ctx.lang == "hi" else tools.inr
    pct = lambda x: f"{x*100:.1f}%"
    corr = None
    others = [x for x in pf.positions if x != tk]
    if others:
        try:
            from analysis import factors
            m = factors.correlation_matrix(ctx.pit, others + [tk])
            pairs = [(x, m.get(tk, {}).get(x)) for x in others]
            pairs = [(x, r) for x, r in pairs if r is not None]
            corr = max(pairs, key=lambda kv: kv[1]) if pairs else None
        except Exception:  # noqa: BLE001
            corr = None

    small = nav * 0.02
    if room < small:
        fit_tone = "bad"
        why = {"position": t(f"you already hold {pct(cur_w)} in {name}, which is at or near your {pct(lim.max_position_pct)} single-stock limit",
                             f"आपके पास {name} में पहले से {pct(cur_w)} है, जो आपकी {pct(lim.max_position_pct)} की सीमा के पास या ऊपर है"),
               "sector": t(f"{sec_label} is already {pct(sec_w)} of your money, against your {pct(lim.max_sector_pct)} sector limit",
                           f"{sec_label} पहले से आपके पैसे का {pct(sec_w)} है, जबकि आपकी सेक्टर सीमा {pct(lim.max_sector_pct)} है"),
               "cash": t("you do not have enough free cash", "आपके पास पर्याप्त खाली नक़द नहीं है")}[binding]
        fit = t(f"It does not fit your portfolio right now: {why}.", f"अभी यह आपके पोर्टफ़ोलियो में फ़िट नहीं बैठता: {why}।")
    else:
        fit_tone = "good" if room >= nav * 0.05 else "warn"
        fit = t(f"It fits your portfolio. You could add up to about {inr_(room)} before hitting one of your own limits.",
                f"यह आपके पोर्टफ़ोलियो में फ़िट बैठता है। अपनी सीमा तक पहुँचने से पहले आप लगभग {inr_(room)} तक जोड़ सकते हैं।")

    bullets = [t(f"The company: {biz}", f"कंपनी: {biz}")]
    if good or weak:
        bullets.append(t("Why: " + "; ".join((good + weak)[:3]) + ".", "क्यों: " + "; ".join((good + weak)[:3]) + "।"))
    bullets.append(t(f"Your portfolio: {fit}", f"आपका पोर्टफ़ोलियो: {fit}"))
    if held_val:
        bullets.append(t(f"You already hold {name} at {pct(cur_w)} of your money; {sec_label} is {pct(sec_w)} in total.",
                         f"आपके पास {name} पहले से {pct(cur_w)} है; {sec_label} कुल {pct(sec_w)} है।"))
    else:
        bullets.append(t(f"You do not hold {name} today; {sec_label} is already {pct(sec_w)} of your money.",
                         f"आपके पास अभी {name} नहीं है; {sec_label} पहले से आपके पैसे का {pct(sec_w)} है।"))
    if corr and corr[1] >= 0.7:
        bullets.append(t(f"It tends to move with {universe.name(corr[0])} (correlation {corr[1]:.2f}), so it adds less variety than it looks.",
                         f"यह {universe.name(corr[0])} के साथ चलता है (सहसंबंध {corr[1]:.2f}), इसलिए उतनी विविधता नहीं जुड़ती जितनी लगती है।"))

    if fit_tone == "bad":
        head = t(f"Not now: {name} would over-concentrate your portfolio.", f"अभी नहीं: {name} से आपका पोर्टफ़ोलियो एक जगह ज़्यादा केंद्रित हो जाएगा।")
        action = t("If you still want it, first reduce a heavy holding in the same sector, or add other sectors. The choice is yours.",
                   "फिर भी लेना चाहें तो पहले इसी सेक्टर का कोई भारी हिस्सा घटाइए, या दूसरे सेक्टर जोड़िए। फ़ैसला आपका है।")
    elif biz_tone == "bad":
        head = t(f"It fits your portfolio, but {name}'s numbers are weaker than its sector.", f"{name} आपके पोर्टफ़ोलियो में फ़िट है, पर इसके आँकड़े सेक्टर से कमज़ोर हैं।")
        action = t("If you buy, keep it small and compare it with stronger peers first. The choice is yours.", "ख़रीदें तो छोटी मात्रा रखिए और पहले मज़बूत साथियों से तुलना कीजिए। फ़ैसला आपका है।")
    else:
        if not biz_tone:
            head = t(f"{name} fits your portfolio, but I have no company numbers for it yet.", f"{name} आपके पोर्टफ़ोलियो में फ़िट है, पर अभी इसके कंपनी-आँकड़े मेरे पास नहीं हैं।")
        else:
            head = t(f"Yes, {name} can fit: {('numbers look healthy' if biz_tone == 'good' else 'numbers look average')} and you have room.",
                     f"हाँ, {name} फ़िट हो सकता है: {('आँकड़े अच्छे हैं' if biz_tone == 'good' else 'आँकड़े औसत हैं')} और आपके पास जगह है।")
        action = t(f"A sensible size is up to about {inr_(min(room, nav * 0.05))}. I cannot say whether the price will rise; nobody can. The choice is yours.",
                   f"समझदारी की मात्रा लगभग {inr_(min(room, nav * 0.05))} तक है। भाव बढ़ेगा या नहीं, यह मैं नहीं बता सकता; कोई नहीं बता सकता। फ़ैसला आपका है।")

    a = Answer(headline=head, bullets=bullets, action=action, subject=tk,
               detail=t("Peers = other companies in the same sector in this app's list. Room = the smallest gap to your position, sector and cash limits.",
                        "साथी = इस ऐप की सूची में उसी सेक्टर की दूसरी कंपनियाँ। जगह = आपकी पोज़िशन, सेक्टर और नक़द सीमाओं में सबसे छोटा अंतर।"),
               facts=[_fact(t("Company numbers", "कंपनी के आँकड़े"), {"good": t("Healthy", "अच्छे"), "warn": t("Average", "औसत"), "bad": t("Weaker", "कमज़ोर"), "": "–"}[biz_tone], biz_tone),
                      _fact(t("Fit with you", "आपके साथ फ़िट"), t("Fits", "फ़िट") if fit_tone != "bad" else t("Does not fit", "फ़िट नहीं"), fit_tone),
                      _fact(t("Room to add", "जोड़ने की जगह"), inr_(room) if room >= small else "₹0")],
               table={"columns": [t("Measure", "पैमाना"), name, t("Sector peers", "सेक्टर के साथी"), t("Reading", "मतलब")], "rows": rows} if rows else None)
    a.data = {"ticker": tk, "room": room, "binding": binding, "fit": fit_tone, "company": biz_tone}
    return _done(a, ctx, [(f"Analyse {name}", f"{name} का विश्लेषण"), _FU["xray"] if "xray" in _FU else _FU["digest"]], "should_buy")




# ---- insurance policy check (backend/rural.py: policy_check) ---------------------------------
def _policy_args(text: str) -> dict:
    """premium, pay_years, term_years, maturity, cover from a sentence in English (digits) -- Hindi arrives already
    rewritten into an English sentence by hindi_input."""
    q = tools.quantities(text)
    out: dict = {}
    for m in q["money"]:
        c = m["ctx"]
        if re.search(r"(cover|assured|death benefit)", c[-14:]) and "cover" not in out:
            out["cover"] = m["v"]
        elif re.search(r"(maturity|get back|receive|payout|returns?|bonus)", c) and "maturity" not in out:
            out["maturity"] = m["v"]
        elif re.search(r"(premium|pay|paying|instal)", c) and "premium" not in out:
            out["premium"] = m["v"]
    yrs = q["years"]
    for y in yrs:
        c = y["ctx"]
        if re.search(r"(after|term|policy of|for the next|matur)", c) and "term_years" not in out:
            out["term_years"] = int(y["v"])
        elif "pay_years" not in out:
            out["pay_years"] = int(y["v"])
    if "term_years" in out and "pay_years" not in out and len(yrs) == 1:
        out["pay_years"] = out["term_years"]
    if "pay_years" in out and "term_years" not in out and len(yrs) == 1:
        out["term_years"] = out["pay_years"]
    return out


def h_policy_check(text: str, ctx: Ctx) -> Answer:
    from backend import rural
    t = _t(ctx.lang)
    p = _policy_args(text)
    need = [k for k in ("premium", "pay_years", "term_years", "maturity") if k not in p]
    flags = rural.policy_check(1, 1, 1, 1, None, None, text, ctx.lang)
    if need and not flags["scam"]:
        a = Answer(headline=t("Tell me the premium, how many years you pay, the policy term and the maturity amount, and I will work out what it really returns.",
                              "प्रीमियम, कितने साल भरते हैं, पॉलिसी कितने साल की है और मैच्योरिटी रक़म बताइए, मैं निकालूँगा कि असल में कितना रिटर्न है।"),
                   bullets=[t("For example: premium 50000 a year for 10 years, 10 lakh after 20 years, cover 5 lakh.", "जैसे: साल का प्रीमियम 50000, 10 साल तक, 20 साल बाद 10 लाख, कवर 5 लाख।")],
                   visual={"page": "rural", "label": t("Open the policy checker", "पॉलिसी जाँचक खोलें"), "params": {"tool": "policy"}})
        return _done(a, ctx, [_RURAL_FU["policy"], _RURAL_FU["loan"]], "policy_check")
    r = rural.policy_check(p.get("premium", 1), p.get("pay_years", 1), p.get("term_years", 1), p.get("maturity", 1), p.get("cover"), None, text, ctx.lang)
    money = tools.inr_hi if ctx.lang == "hi" else tools.inr
    bullets = [*r["bullets"][:3], *[f["text"] for f in r["flags"][:2]]]
    a = Answer(headline=r["headline"], bullets=bullets, action=r["verdict"] if not r["scam"] else r["flags"][0]["text"],
               detail=r["note"],
               facts=[_fact(t("Yearly return", "सालाना रिटर्न"), "–" if r["irr_pct"] is None else f"{r['irr_pct']:.1f}%", {"red": "bad", "amber": "warn", "green": "good"}[r["band"]]),
                      _fact(t("You pay", "आप भरते हैं"), money(r["total_paid"])), _fact(t("You get", "आपको मिलेगा"), money(r["maturity"]))],
               visual={"page": "rural", "label": t("Open the policy checker", "पॉलिसी जाँचक खोलें"),
                       "params": {"tool": "policy", "premium": p.get("premium", 0), "pay_years": p.get("pay_years", 0), "term_years": p.get("term_years", 0),
                                  "maturity": p.get("maturity", 0), **({"cover": p["cover"]} if p.get("cover") else {}), "run": 1}})
    a.data = {"policy": r}
    return _done(a, ctx, [_RURAL_FU["loan"], _RURAL_FU["schemes"]], "policy_check")


# ---- UPI safety (backend/rural.py: upi_check) -------------------------------------------------
def h_upi_check(text: str, ctx: Ctx) -> Answer:
    from backend import rural
    t = _t(ctx.lang)
    r = rural.upi_check(text, ctx.lang)
    bullets = [r["why"], *r["do"], r["rules"][0] if not r["matched"] else r["recover"]]
    a = Answer(headline=r["headline"], bullets=bullets[:4], action=r["verdict"],
               detail=t("Matched against the common UPI tricks. If no trick matches, the six rules below still catch almost every fraud.", "आम UPI चालों से मिलाया गया। कोई चाल न मिले तो भी नीचे के छह नियम लगभग हर ठगी पकड़ लेते हैं।"),
               facts=[_fact(t("Verdict", "नतीजा"), {"red": t("Scam", "ठगी"), "amber": t("Careful", "सावधान"), "green": t("OK", "ठीक")}[r["level"]], {"red": "bad", "amber": "warn", "green": "good"}[r["level"]])],
               visual={"page": "rural", "label": t("Open UPI safety", "UPI सुरक्षा खोलें"), "params": {"tool": "upi", "text": text[:300], "run": 1}})
    a.data = {"upi": r}
    return _done(a, ctx, [_RURAL_FU["upi_recover"], _RURAL_FU["policy"]], "upi_check")


# ---- DBT / subsidy tracer (backend/rural.py: dbt_trace) ---------------------------------------
def _dbt_args(text: str) -> dict:
    """Whatever the sentence already says about the scheme and the break; the rest is asked."""
    low = text.lower()
    out: dict = {}
    for sid, rx in (("pm_kisan", r"kisan|किसान"), ("pension", r"pension|पेंशन"), ("scholarship", r"scholar|छात्रवृत्ति"), ("lpg", r"\blpg\b|gas|गैस"),
                    ("mgnrega", r"nrega|मनरेगा|wages"), ("ration", r"ration|राशन")):
        if re.search(rx, low):
            out["scheme"] = sid
            break
    for st, rx in (("rejected", r"reject|failed|रिजेक्ट"), ("other_account", r"another account|other account|different account|दूसरे खाते|अनजान खाते"),
                   ("pending", r"pending|under process|पेंडिंग"), ("not_applied", r"never applied|not registered|आवेदन नहीं")):
        if re.search(rx, low):
            out["status"] = st
            break
    if re.search(r"aadhaa?r.{0,25}(not|isn.t|is not).{0,12}(linked|seeded)|आधार.{0,20}(लिंक|जुड़ा) नहीं", low):
        out["linked"] = "no"
    if re.search(r"name.{0,25}(different|mismatch|not (the )?same|spell)|नाम.{0,20}(अलग|गलत)", low):
        out["name_same"] = "no"
    if re.search(r"(bank|branch).{0,25}(merged|merger|changed)|बैंक.{0,15}(विलय|मर्ज)", low):
        out["merged"] = "yes"
    if re.search(r"(not used|unused|dormant|inactive).{0,25}(year|years|long)|साल.{0,20}से.{0,10}नहीं", low):
        out["last_used"] = "old"
    return out


def h_dbt_trace(text: str, ctx: Ctx) -> Answer:
    from backend import rural
    t = _t(ctx.lang)
    p = _dbt_args(text)
    r = rural.dbt_trace(p.get("scheme", "other"), p.get("status", "no_status"), p.get("linked", "unsure"), p.get("name_same", "unsure"),
                        p.get("merged", "unsure"), p.get("last_used", "recent"), "unsure", text, ctx.lang)
    top = r["causes"][:3]
    bullets = [f"{i + 1}. {c['title']}: {c['steps'][0]}" for i, c in enumerate(top)]
    if r["scam"]:
        bullets.insert(0, r["scam_note"])
    a = Answer(headline=r["headline"], bullets=bullets, action=r["complain"][0], detail=r["note"],
               facts=[_fact(t("Check status at", "स्टेटस कहाँ देखें"), r["check_at"][:40])],
               visual={"page": "rural", "label": t("Trace my payment", "मेरा भुगतान खोजें"), "params": {"tool": "dbt", **p}})
    a.data = {"dbt": r}
    return _done(a, ctx, [_RURAL_FU["docs"], _RURAL_FU["schemes"]], "dbt_trace")


# ---- sell now or hold the crop (backend/rural.py: hold_or_sell) ---------------------------------
def _hold_args(text: str) -> dict:
    """quantity (quintals), price now, months to wait, expected later price from one sentence."""
    low = text.lower()
    out: dict = {}
    taken: list[tuple[int, int]] = []
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(quintals?|qtl|q|क्विंटल)", low)
    if m:
        out["qty"] = float(m.group(1).replace(",", ""))
        taken.append(m.span(1))
    mm = re.search(r"(\d+)\s*(months?|महीने|माह)", low)
    if mm:
        out["months"] = int(mm.group(1))
        taken.append(mm.span(1))
    for x in re.finditer(r"\d[\d,]*(?:\.\d+)?", low):
        if any(a_ <= x.start() < b_ for a_, b_ in taken):
            continue
        v = float(x.group(0).replace(",", ""))
        if v < 50:
            continue
        before = low[max(0, x.start() - 30):x.start()]
        if re.search(r"(later|after|then|expect|hope|reach|will be|may be|next|worth)", before) and "price_later" not in out and "price_now" in out:
            out["price_later"] = v
        elif "price_now" not in out:
            out["price_now"] = v
        elif "price_later" not in out:
            out["price_later"] = v
    return out


def h_hold_sell(text: str, ctx: Ctx) -> Answer:
    from backend import rural
    t = _t(ctx.lang)
    p = _hold_args(text)
    if not all(k in p for k in ("qty", "price_now", "months")):
        a = Answer(headline=t("Tell me how much you have, today's price and how long you could wait, and I will work out what the price must reach to make waiting worth it.",
                              "बताइए कितनी फ़सल है, आज का भाव क्या है और कितना रुक सकते हैं; मैं निकालूँगा कि रुकना फ़ायदे का होने के लिए भाव कितना चाहिए।"),
                   bullets=[t("For example: 50 quintals of wheat at 2100 a quintal, wait 4 months for 2400.", "जैसे: 50 क्विंटल गेहूँ 2100 के भाव, 4 महीने रुकूँ तो 2400 की उम्मीद।")],
                   visual={"page": "rural", "label": t("Open the sell-or-hold tool", "बेचें या रोकें औज़ार खोलें"), "params": {"tool": "hold"}})
        return _done(a, ctx, [_RURAL_FU["hold"], _RURAL_FU["loan"]], "hold_sell")
    r = rural.hold_or_sell(p["qty"], p["price_now"], p["months"], p.get("price_later"), 0, 0, 0, 7.0, "", None, ctx.lang)
    a = Answer(headline=r["headline"], bullets=[r["cash_note"], r["tip"]], action=r["verdict"], detail=r["warn"],
               facts=[_fact(t("Break-even price", "बराबरी का भाव"), f"₹{r['breakeven_price']:,.0f}" if r["breakeven_price"] else "–"),
                      _fact(t("Rise needed", "ज़रूरी बढ़त"), f"{r['rise_needed_pct']:.1f}%" if r["rise_needed_pct"] is not None else "–", {"red": "bad", "amber": "warn", "green": "good"}[r["band"]])],
               visual={"page": "rural", "label": t("Open the sell-or-hold tool", "बेचें या रोकें औज़ार खोलें"),
                       "params": {"tool": "hold", "qty": p["qty"], "price_now": p["price_now"], "months": p["months"], **({"price_later": p["price_later"]} if p.get("price_later") else {}), "run": 1}})
    a.data = {"hold": r}
    return _done(a, ctx, [_RURAL_FU["loan"], _RURAL_FU["income"]], "hold_sell")


# ---- daily-wage saving (backend/rural.py: daily_saving) ---------------------------------------
def _saving_args(text: str) -> dict:
    low = text.lower()
    out: dict = {}
    for gid, rx in (("daughter", r"daughter|beti|बेटी"), ("son", r"\bson\b|beta|बेटे"), ("house", r"house|roof|repair|मकान|छत"), ("medical", r"medical|hospital|illness|इलाज"),
                    ("animal", r"cow|buffalo|goat|गाय|भैंस|बकरी"), ("tools", r"cart|tools?|shop|ठेला|दुकान"), ("festival", r"festival|wedding|त्योहार|शादी"), ("oldage", r"old age|retire|बुढ़ापा")):
        if re.search(rx, low):
            out["goal"] = gid
            break
    m = re.search(r"(\d[\d,]*)\s*(?:rs|rupees?|₹)?\s*(?:a|per|each|every)\s*(?:working )?day|(?:rs|rupees?|₹)\s*(\d[\d,]*)\s*(?:a|per|each|every)\s*(?:working )?day", low)
    if m:
        val = float((m.group(1) or m.group(2)).replace(",", ""))
        if re.search(r"(earn|wage|paid|get|income|make)\D{0,10}$", low[max(0, m.start() - 14):m.start()]):
            out["daily_wage"] = val                         # "I earn 400 a day" is a wage, not a saving
        else:
            out["daily"] = val
    q = tools.quantities(text)
    if q["years"]:
        out["months"] = int(q["years"][0]["v"] * 12)
    elif q["months"]:
        out["months"] = int(q["months"][0]["v"])
    for x in q["money"]:
        if x["v"] >= 1000 and (out.get("daily") is None or abs(x["v"] - out["daily"]) > 1e-9):
            out["target"] = x["v"]
            break
    w = re.search(r"(?:earn|wage|wages|paid|mazdoori)\D{0,15}(\d[\d,]*)", low)
    if w:
        out["daily_wage"] = float(w.group(1).replace(",", ""))
    return out


def h_saving_goal(text: str, ctx: Ctx) -> Answer:
    from backend import rural
    t = _t(ctx.lang)
    p = _saving_args(text)
    if not (p.get("daily") or (p.get("target") and p.get("months"))):
        a = Answer(headline=t("Tell me what you want to save for and when, or how much you can put aside each day, and I will work out the rest.",
                              "बताइए किसके लिए और कब तक बचाना है, या रोज़ कितना अलग रख सकते हैं; बाक़ी मैं निकालूँगा।"),
                   bullets=[t("For example: I want 1 lakh for my daughter's marriage in 5 years. Or: I can save 10 rupees a day.", "जैसे: बेटी की शादी के लिए 5 साल में 1 लाख चाहिए। या: मैं रोज़ 10 रुपये बचा सकता हूँ।")],
                   visual={"page": "rural", "label": t("Open the saving planner", "बचत योजना खोलें"), "params": {"tool": "saving"}})
        return _done(a, ctx, [_RURAL_FU["saving"], _RURAL_FU["loan"]], "saving_goal")
    r = rural.daily_saving(p.get("goal", "other"), p.get("target"), p.get("months"), p.get("daily"), p.get("daily_wage"), 26, 6.7, 6.0, ctx.lang)
    a = Answer(headline=r["headline"], bullets=r["bullets"][:3], action=r["rules"][0], detail=r["note"],
               facts=[_fact(t("Per working day", "प्रति काम के दिन"), f"₹{r.get('per_day_rd', r.get('daily', 0)):,.0f}")],
               visual={"page": "rural", "label": t("Open the saving planner", "बचत योजना खोलें"),
                       "params": {"tool": "saving", **{k: v for k, v in p.items()}, "run": 1}})
    a.data = {"saving": r}
    return _done(a, ctx, [_RURAL_FU["loan"], _RURAL_FU["income"]], "saving_goal")


# ---- self-help group ledger (backend/rural.py: shg_summary) ------------------------------------
def h_shg_ledger(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    a = Answer(headline=t("I can keep your self-help group's accounts: savings, loans, repayments and fines, and work out the interest and the year-end share for you.",
                          "मैं आपके स्वयं सहायता समूह का हिसाब रख सकता हूँ: बचत, ऋण, किस्तें और जुर्माना, और ब्याज तथा वर्ष के अंत का हिस्सा निकाल सकता हूँ।"),
               bullets=[t("Add the members, then enter each meeting's savings and loans. It shows who owes what, who has missed saving, whether the group has lent more than it has, and writes the meeting report and each member's statement to share.",
                          "सदस्य जोड़िए, फिर हर बैठक की बचत और ऋण भरिए। यह दिखाता है किस पर कितना बाक़ी है, किसने बचत छोड़ी, समूह ने अपने पास से ज़्यादा उधार तो नहीं दिया, और बैठक की रिपोर्ट और हर सदस्य का विवरण साझा करने को लिख देता है।"),
                        t("The ledger stays on this device; export a backup file regularly.", "बही इसी डिवाइस पर रहती है; नियमित रूप से बैकअप फ़ाइल निकालिए।")],
               visual={"page": "rural", "label": t("Open the group ledger", "समूह की बही खोलें"), "params": {"tool": "shg"}})
    return _done(a, ctx, [_RURAL_FU["saving"], _RURAL_FU["loan"]], "shg_ledger")


# ---- credit score explainer (backend/rural.py: credit_check) ------------------------------------
def h_credit_score(text: str, ctx: Ctx) -> Answer:
    from backend import rural
    t = _t(ctx.lang)
    r = rural.credit_check(lang=ctx.lang)
    a = Answer(headline=t("A credit score is a number the credit bureaus work out from how you repay loans. Having none is not bad, and you can build one.", "क्रेडिट स्कोर वह संख्या है जो क्रेडिट ब्यूरो आपके ऋण चुकाने के तरीक़े से निकालते हैं। स्कोर न होना बुरा नहीं, और आप इसे बना सकते हैं।"),
               bullets=[r["facts"][1], r["facts"][3], r["scams"][0]], action=r["report_note"],
               detail=r["note"], facts=[_fact(t("Good score", "अच्छा स्कोर"), "750+", "good"), _fact(t("Range", "सीमा"), "300–900")],
               visual={"page": "rural", "label": t("Check my situation", "मेरी स्थिति जाँचें"), "params": {"tool": "credit"}})
    a.data = {"credit": r}
    return _done(a, ctx, [_RURAL_FU["loan"], _RURAL_FU["saving"]], "credit_score")

# ---- rural / low-income tools (backend/rural.py) ---------------------------------------
def _open_rural(tool: str, label_en: str, label_hi: str, ctx: Ctx, params: dict | None = None) -> dict:
    return {"page": "rural", "label": _t(ctx.lang)(label_en, label_hi), "params": {"tool": tool, **(params or {})}}


def _loan_args(text: str) -> tuple[float | None, str, float | None, int | None]:
    """(rate, unit, principal, months) from a sentence like '5 rupees per hundred a month on 50000 for 10 months'."""
    low = text.lower()
    q = tools.quantities(text)
    rate, unit = None, "per100_month"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:rupees?|rs\.?|₹)?\s*(?:per|a|for every|on every|in every)\s*(?:hundred|100)\b", low) or \
        re.search(r"(\d+(?:\.\d+)?)\s*(?:rupees?|rs\.?|₹)?\s*(?:sainkda|saikda|sekda)\b", low)
    if m:
        rate = float(m.group(1))
    elif q["pct"]:
        rate = q["pct"][0]["v"]
        yearly = re.search(r"(per|a|every|each)\s*(year|annum)|yearly|annual|p\.?a\b", low)
        unit = "pct_year" if yearly else "pct_month"
    money = [x["v"] for x in q["money"] if x["v"] >= 500]
    principal = max(money) if money else None
    months = None
    if q["months"]:
        months = int(q["months"][0]["v"])
    elif q["years"]:
        months = int(q["years"][0]["v"] * 12)
    return rate, unit, principal, months


def h_moneylender(text: str, ctx: Ctx) -> Answer:
    from backend import rural
    t = _t(ctx.lang)
    rate, unit, principal, months = _loan_args(text)
    if rate is None:
        a = Answer(headline=t("Tell me the interest the lender charges and I will show what it really costs.",
                              "साहूकार कितना ब्याज लेता है यह बताइए, मैं दिखाऊँगा कि असल में कितना पड़ता है।"),
                   bullets=[t("For example: 5 rupees per hundred a month on 50000 for 10 months.", "जैसे: 50000 रुपये पर 5 रुपये सैकड़ा महीना, 10 महीने के लिए।")],
                   visual=_open_rural("loan", "Open the loan checker", "ऋण जाँचक खोलें", ctx))
        return _done(a, ctx, [_RURAL_FU["loan"], _RURAL_FU["schemes"]], "moneylender")
    principal = principal or 10000.0
    months = months or 12
    compound = bool(re.search(r"compound|byaj pe byaj|interest on interest|ब्याज पर ब्याज", text.lower()))
    r = rural.loan_cost(principal, rate, unit, months, "bullet_compound" if compound else "interest_only", ctx.lang)
    inr_ = tools.inr_hi if ctx.lang == "hi" else tools.inr
    best = next(x for x in r["alternatives"] if x["id"] == "shg")
    bullets = [t(f"On {inr_(principal)} for {months} months you pay {inr_(r['interest'])} interest, {inr_(r['total'])} in all.",
                 f"{inr_(principal)} पर {months} महीने में {inr_(r['interest'])} ब्याज लगता है, कुल {inr_(r['total'])}।"),
               r["verdict"],
               t(f"At a normal rate the same loan would cost about {inr_(best['interest'])}: you would save about {inr_(best['saves'])}. Ask about {best['name']}.",
                 f"सामान्य दर पर यही ऋण लगभग {inr_(best['interest'])} का पड़ता: आप लगभग {inr_(best['saves'])} बचाते। {best['name']} के बारे में पूछिए।")]
    if r["double_months"]:
        bullets.append(t(f"At this rate a debt can double in about {r['double_months']:.0f} months if nothing is repaid.", f"इस दर पर कुछ न चुकाने पर क़र्ज़ लगभग {r['double_months']:.0f} महीने में दोगुना हो सकता है।"))
    a = Answer(headline=r["headline"], bullets=bullets, action=r["legal"],
               detail=t("Yearly rate = monthly rate x 12 (or compounded if interest is added to the loan). Interest = amount x monthly rate x months.",
                        "सालाना दर = मासिक दर x 12 (या ब्याज मूल में जुड़ता हो तो चक्रवृद्धि)। ब्याज = रक़म x मासिक दर x महीने।"),
               facts=[_fact(t("Per year", "साल में"), f"{r['yearly_pct']:.0f}%", {"red": "bad", "amber": "warn", "green": "good"}[r["band"]]),
                      _fact(t("Interest", "ब्याज"), inr_(r["interest"]), "bad" if r["band"] == "red" else ""),
                      _fact(t("You repay", "आप चुकाते हैं"), inr_(r["total"]))],
               table={"columns": [t("Where from", "कहाँ से"), t("Rate a year", "साल की दर"), t("Interest", "ब्याज")],
                      "rows": [[t("This lender", "यह साहूकार"), f"{r['yearly_pct']:.0f}%", inr_(r["interest"])]] +
                              [[x["name"], f"~{x['yearly_pct']:.0f}%", inr_(x["interest"])] for x in r["alternatives"]]},
               visual=_open_rural("loan", "Open the loan checker", "ऋण जाँचक खोलें", ctx,
                                  {"principal": principal, "rate": rate, "unit": unit, "months": months}))
    a.data = {"loan": r}
    return _done(a, ctx, [_RURAL_FU["schemes"], _RURAL_FU["income"]], "moneylender")


def h_scheme_check(text: str, ctx: Ctx) -> Answer:
    from backend import rural
    t = _t(ctx.lang)
    q = tools.quantities(text)
    money = [x["v"] for x in q["money"] if x["v"] >= 100]
    months = (q["months"][0]["v"] if q["months"] else q["years"][0]["v"] * 12 if q["years"] else None)
    put, get = (money[0], money[1]) if len(money) >= 2 else (None, None)
    r = rural.scheme_check(text, put, get, months, ctx.lang)
    bullets = [f["text"] for f in r["flags"][:4]] or [r["rule"]]
    if r["implied_yearly_pct"] is not None:
        bullets.append(t(f"For comparison a bank fixed deposit pays about 7% a year.", "तुलना के लिए बैंक एफ़डी लगभग 7% साल देती है।"))
    bullets.append(t("Check it here: ", "यहाँ जाँचिए: ") + r["verify"][0])
    a = Answer(headline=r["verdict"], bullets=bullets, action=r["rule"],
               detail=t("Each warning sign you describe adds to a score; none of this proves a scheme is honest or a fraud, it shows how many known fraud patterns it matches.",
                        "आपके बताए हर चेतावनी-संकेत से स्कोर बढ़ता है; इससे यह साबित नहीं होता कि योजना सही है या ठगी, बस यह दिखता है कि वह कितने जाने-पहचाने ठगी के पैटर्न से मिलती है।"),
               facts=[_fact(t("Risk score", "जोखिम स्कोर"), f"{r['score']}/100", {"red": "bad", "amber": "warn", "green": "good"}[r["level"]]),
                      _fact(t("Warning signs", "चेतावनी-संकेत"), str(len([f for f in r["flags"]])))],
               visual=_open_rural("scheme", "Open the offer checker", "ऑफ़र जाँचक खोलें", ctx, {"text": text[:600]}))
    a.data = {"scheme": r}
    return _done(a, ctx, [_RURAL_FU["loan"], _RURAL_FU["schemes"]], "scheme_check")


def h_entitlements(text: str, ctx: Ctx) -> Answer:
    from backend import rural
    t = _t(ctx.lang)
    a = Answer(headline=t("Answer a few questions and I will list the government schemes you may be missing.",
                          "कुछ सवालों के जवाब दीजिए, मैं सरकारी योजनाओं की सूची दूँगा जो शायद आपसे छूट रही हैं।"),
               bullets=[t("Examples: PM-KISAN (₹6,000 a year), crop and life insurance for ₹20-₹436 a year, free health cover up to ₹5 lakh, pensions, 100 days of work.",
                          "जैसे: पीएम-किसान (साल में ₹6,000), ₹20-₹436 सालाना में फ़सल और जीवन बीमा, ₹5 लाख तक मुफ़्त इलाज, पेंशन, 100 दिन का काम।"),
                        t("Nobody may charge you a fee to apply.", "आवेदन के लिए किसी को पैसे देने की ज़रूरत नहीं।")],
               action=t(f"Rules change: confirm at the place shown. List last reviewed {rural.CHECKED}.", f"नियम बदलते रहते हैं: दिखाई जगह पर पक्का कीजिए। सूची की आख़िरी समीक्षा {rural.CHECKED}।"),
               visual=_open_rural("schemes", "Find my schemes", "मेरी योजनाएँ खोजें", ctx))
    return _done(a, ctx, [_RURAL_FU["docs"], _RURAL_FU["loan"]], "entitlements")


def h_docs_ready(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    a = Answer(headline=t("Most payments stop for three simple reasons. Check these first.", "ज़्यादातर भुगतान तीन सीधी वजहों से रुकते हैं। पहले इन्हें जाँचिए।"),
               bullets=[t("1. Aadhaar is not linked (seeded) to your bank account for benefit transfers.", "1. आपका आधार बैंक खाते से (लाभ हस्तांतरण के लिए) जुड़ा नहीं है।"),
                        t("2. Your name is spelled differently on Aadhaar and in the bank.", "2. आधार और बैंक में आपके नाम की वर्तनी अलग है।"),
                        t("3. The account is dormant or closed.", "3. खाता निष्क्रिय या बंद है।")],
               action=t("Check the link on myaadhaar.uidai.gov.in → Bank Seeding Status, or ask the bank for a written acknowledgement.",
                        "myaadhaar.uidai.gov.in → Bank Seeding Status पर जाँचिए, या बैंक से लिखित पावती माँगिए।"),
               visual=_open_rural("docs", "Check my documents", "मेरे काग़ज़ जाँचें", ctx))
    return _done(a, ctx, [_RURAL_FU["schemes"], _RURAL_FU["loan"]], "docs_ready")


def h_income_plan(text: str, ctx: Ctx) -> Answer:
    t = _t(ctx.lang)
    a = Answer(headline=t("Tell me when your money comes in and what you spend, and I will plan the lean months.", "बताइए पैसा कब आता है और ख़र्च कितना है, मैं कमज़ोर महीनों की योजना बनाऊँगा।"),
               bullets=[t("It shows which months you run short, how much to keep aside from each harvest or lump, and what a loan would cost if you do not.",
                          "यह दिखाता है कि किन महीनों में कमी पड़ती है, हर फ़सल या एकमुश्त आमदनी में से कितना अलग रखना है, और न रखने पर क़र्ज़ कितना महँगा पड़ेगा।")],
               visual=_open_rural("income", "Plan my year", "मेरा साल बनाएँ", ctx))
    return _done(a, ctx, [_RURAL_FU["loan"], _RURAL_FU["schemes"]], "income_plan")


_RURAL_FU = {
    "saving": ("How much should I save a day for my daughter's marriage?", "बेटी की शादी के लिए रोज़ कितना बचाऊँ?"),
    "hold": ("Should I sell my wheat now or wait for a better price?", "गेहूँ अभी बेचूँ या बेहतर भाव के लिए रुकूँ?"),
    "upi_recover": ("I already paid a scammer on UPI, what do I do?", "मैंने UPI से ठग को पैसे भेज दिए, अब क्या करूँ?"),
    "policy": ("Is my insurance policy a good deal?", "क्या मेरी बीमा पॉलिसी अच्छा सौदा है?"),
    "loan": ("What does 5 rupees per hundred a month really cost?", "5 रुपये सैकड़ा महीने का असल में कितना पड़ता है?"),
    "schemes": ("Which government schemes can I get?", "मुझे कौन सी सरकारी योजनाएँ मिल सकती हैं?"),
    "docs": ("Why has my payment not come?", "मेरा पैसा क्यों नहीं आया?"),
    "income": ("Plan my money around the harvest", "फ़सल के हिसाब से मेरे पैसे की योजना बनाइए"),
}


HANDLERS: dict[str, Callable[[str, Ctx], Answer]] = {
    "help": h_help, "chitchat": h_chitchat, "clarify": h_clarify, "out_of_scope": h_out_of_scope, "predict": h_predict,
    "fund_overlap": h_fund_overlap, "fund_list": h_fund_list, "fund_info": h_fund_info, "fund_vs_direct": h_fund_vs_direct,
    "my_funds_add": h_my_funds_add, "my_funds_remove": h_my_funds_remove, "my_funds_show": h_my_funds_show,
    "fee_drag": h_fee_drag, "emergency": h_emergency, "goal": h_goal, "panic": h_panic, "digest": h_digest,
    "should_buy": h_buy_advice, "credit_score": h_credit_score, "shg_ledger": h_shg_ledger, "saving_goal": h_saving_goal, "hold_sell": h_hold_sell, "dbt_trace": h_dbt_trace, "upi_check": h_upi_check, "policy_check": h_policy_check, "moneylender": h_moneylender, "scheme_check": h_scheme_check, "entitlements": h_entitlements, "docs_ready": h_docs_ready, "income_plan": h_income_plan, "scam_help": h_scam_help, "scam_recovery": h_scam_recovery, "tip_scan": h_tip_scan, "ledger": h_ledger,
}


def _drop_echo(a: Answer, asked: str) -> None:
    """Never offer, as a next step, the very question that was just asked."""
    def norm(x: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", x.lower()).strip()
    keep = [i for i, q in enumerate(a.follow_ups) if norm(q) != norm(asked)]
    a.follow_ups = [a.follow_ups[i] for i in keep]
    if a.follow_ups_hi:
        a.follow_ups_hi = [a.follow_ups_hi[i] for i in keep if i < len(a.follow_ups_hi)]


# =================================================================== LLM intent fallback
INTENT_DOC = {
    "fund_overlap": "do mutual funds hold the same stocks / are funds duplicates / compare two funds",
    "fund_list": "which sample mutual funds are available",
    "fund_info": "what stocks a particular fund holds",
    "fund_vs_direct": "how much of the user's own direct stocks is already inside a fund",
    "my_funds_add": "the user states which funds they own",
    "my_funds_show": "show the funds the user saved",
    "fee_drag": "what a fund fee / expense ratio / plan type costs over time",
    "emergency": "how long cash savings last without income / emergency fund",
    "goal": "will a SIP or portfolio reach a target amount in some years / retirement planning",
    "panic": "what would have happened if the user sold during a crash",
    "digest": "weekly summary of the user's portfolio",
    "scam_help": "a suspicious call/message/link or OTP request (not yet scammed)",
    "scam_recovery": "the user already lost money or shared a code / was scammed and wants to know what to do",
    "tip_scan": "check if a pasted stock tip / group promise is legitimate",
    "predict": "asks for price predictions or hot stock picks",
    "ledger": "is the trade record tamper-proof",
    "help": "what can the assistant do",
    "xray": "overall health summary of the user's portfolio",
    "why": "why is the portfolio risk high / main risks",
    "fix": "what to sell or reduce to lower risk",
    "stress": "what happens in a market fall or crash scenario",
    "diversification": "is the portfolio spread out enough",
    "correlation": "which holdings move together",
    "should_buy": "should the user buy / add a specific stock",
    "credit_score": "what a credit score / CIBIL is, why a loan was rejected, how a first-time borrower builds credit",
    "shg_ledger": "keep the accounts of a self-help group: savings, internal loans, repayments, interest, member statements",
    "saving_goal": "how much to save per day / week for a goal like a daughter's marriage, or what saving a small amount a day becomes",
    "hold_sell": "should a farmer sell the crop now or hold it for a better mandi price; what price makes waiting worth it",
    "dbt_trace": "a government payment / subsidy / pension / instalment has not arrived, why, and how to fix it",
    "upi_check": "is a UPI request / QR / link / call asking for PIN or payment safe, or a UPI scam trick",
    "policy_check": "is an insurance policy / endowment / agent-sold plan a good deal, or a bonus-refund scam",
    "moneylender": "what a moneylender / private loan interest really costs per year",
    "scheme_check": "is a double-your-money / chit fund / pay-and-earn offer a scam",
    "entitlements": "which government schemes / benefits the user may qualify for",
    "docs_ready": "documents needed / why a government payment has not arrived",
    "income_plan": "plan money around harvest / seasonal / irregular income",
    "define": "definition of a finance term",
    "out_of_scope": "unrelated to money or investing",
    "none": "none of these / too unclear",
}


async def llm_intent(text: str) -> tuple[str | None, float]:
    """Ask the local model to PICK one intent from the closed list above. It never writes the
    answer: whatever it picks is run by the same calculators, and a handler that lacks the
    figures it needs still asks for them."""
    from agents.llm import ANALYST_MODEL, chat_json
    schema = {"type": "object", "properties": {"intent": {"type": "string", "enum": list(INTENT_DOC)},
                                                 "confidence": {"type": "number"}}, "required": ["intent", "confidence"]}
    menu = "\n".join(f"- {k}: {v}" for k, v in INTENT_DOC.items())
    prompt = (f"Classify the user's message about money into exactly one intent.\nIntents:\n{menu}\n\n"
              f"Message: {text}\nReturn the best intent and your confidence from 0 to 1. "
              f"If it is unclear, choose none.")
    try:
        out = await chat_json(prompt, schema, model=ANALYST_MODEL, temperature=0.0, max_tokens=40,
                              attempts=1, timeout=8.0)
    except Exception:  # noqa: BLE001
        return None, 0.0
    intent = out.get("intent")
    return (intent if intent in INTENT_DOC else None), float(out.get("confidence") or 0.0)


LLM_MIN = 0.75


def _glossary_term(text: str) -> str | None:
    low = text.lower()
    for term in sorted(explain.GLOSSARY, key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", low):
            return term
    return None


# =================================================================== entry point
def _run(intent: str, how: str, resolved: str, ctx: Ctx) -> Answer:
    convo = ctx.convo
    if intent in HANDLERS:
        out = HANDLERS[intent](resolved, ctx)
        if convo is not None and intent not in ("clarify", "chitchat", "out_of_scope"):
            convo.last_question = resolved
        out.data["intent"] = intent
        out.data["routed_by"] = how
        _drop_echo(out, resolved)
        return out
    if intent == "define" and ctx.lang == "hi":
        term = _glossary_term(resolved)
        if term and term in GLOSSARY_HI:
            a = Answer(headline=GLOSSARY_HI[term], kind="define", detail=None)
            a.data = {"term": term, "intent": "define", "routed_by": how}
            return _done(a, ctx, _chips("xray", "overlap", "fee"), "define")
    out = explain.answer(resolved, ctx.pit, ctx.portfolio, ctx.prices, ctx.policy, convo=convo,
                         level=ctx.level, kind=intent if intent in LEGACY else None)
    out.data["intent"] = intent
    out.data["routed_by"] = how
    if convo is not None:
        convo.last_question = None
    return out


def answer(text: str, ctx: Ctx) -> Answer:
    """One question in, one structured answer out."""
    convo = ctx.convo
    resolved = convo.resolve(text) if convo and hasattr(convo, "resolve") else text
    intent, how = detect(resolved)
    better = understand.override(resolved, intent, how, ctx)
    if better is not None:
        return better

    # "explain that simpler" after a tool answer: replay that question at the simpler level.
    if intent == "simplify" and convo is not None and getattr(convo, "last_kind", None) in NEW_KINDS and getattr(convo, "last_question", None):
        ctx.level = "simple"
        resolved = convo.last_question
        intent, how = detect(resolved)
    return _run(intent, how, resolved, ctx)


def will_need_model(text: str, ctx: Ctx) -> bool:
    """True if aanswer() is about to call the local model for this text -- cheap and synchronous
    (the same detect()/override() calls aanswer() makes first, before anything that touches
    Ollama). Lets a slow transport (WhatsApp/SMS) send an immediate acknowledgement instead of
    risking a timeout, without duplicating aanswer()'s own routing logic."""
    resolved = ctx.convo.resolve(text) if ctx.convo is not None and hasattr(ctx.convo, "resolve") else text
    intent, how = detect(resolved)
    if understand.override(resolved, intent, how, ctx) is not None:
        return False                               # understand.py already has a fast, local answer
    if intent == "tip_scan" and len(_tip_body(resolved).split()) >= 6:
        return True                                # tipminds.analyse also reads the wording via the model
    fast = intent != "clarify" and not (how == "legacy" and len(resolved.split()) >= 8)
    return not fast


async def aanswer(text: str, ctx: Ctx) -> Answer:
    """Like answer(), but when the rules do not recognise the question a local model may pick
    the intent. Anything it is not confident about is asked about, never guessed."""
    resolved = ctx.convo.resolve(text) if ctx.convo is not None and hasattr(ctx.convo, "resolve") else text
    intent, how = detect(resolved)
    better = understand.override(resolved, intent, how, ctx)
    if better is not None:
        return better
    if intent == "tip_scan":                      # the full panel, with the local model reading the wording
        from analysis import tipminds
        body = _tip_body(resolved)
        if len(body.split()) >= 6:
            out = tip_answer(await tipminds.analyse(ctx.pit, body), ctx)
            out.data["intent"] = "tip_scan"
            return out
    # Specific rules are trusted. The model is consulted when nothing matched, and also when
    # only one of the older loose keyword rules matched on a long sentence (a stray word like
    # "fall" or "invest" can mislead those); it must be confident to overrule them.
    if intent != "clarify" and not (how == "legacy" and len(resolved.split()) >= 8):
        return answer(text, ctx)
    picked, conf = await llm_intent(resolved)
    if picked and picked != "none" and conf >= LLM_MIN and picked != intent:
        out = _run(picked, "llm", resolved, ctx)
        out.data["llm_confidence"] = round(conf, 2)
        return out
    if intent == "clarify":
        near = understand.similar_concept(resolved)
        if near:
            return understand.h_concept(near, ctx)
        cid, cconf = await understand.llm_concept(resolved)
        if cid and cconf >= LLM_MIN:
            out = understand.h_concept(cid, ctx)
            out.data["llm_confidence"] = round(cconf, 2)
            return out
    return answer(text, ctx)
