import { useEffect, useMemo, useRef, useState } from "react";

import "../styles-practice.css";
import { Page } from "./Page";
import { allowSpeech, onVoice, speak, speechGeneration, stop as stopSpeech, voiceReplies } from "../lib/speak";
import * as socket from "../lib/socket";
import { MicIcon } from "../components/VoiceInput";
import { useLang } from "../lib/lang";
import { hashParams } from "../lib/router";
import { EmergencyMeter, FeeDrag, WeeklyDigest } from "./PracticeMore";
import { OverlapMatrixPanel } from "../components/OverlapMatrixPanel";

const post = (url: string, body: unknown) =>
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then((r) => r.json());
const inr = (v: number) => `₹${Math.round(v).toLocaleString("en-IN")}`;
const short = (n: string) => n.replace(" Mahindra Bank", "").replace(" Industries", "").replace(" Technologies", "");

/** Number that counts up to its target, so a result lands instead of just appearing. */
function CountUp({ to, suffix = "", decimals = 0 }: { to: number; suffix?: string; decimals?: number }) {
  const [v, setV] = useState(0);
  useEffect(() => {
    let raf = 0; const t0 = performance.now();
    const tick = (t: number) => {
      const k = Math.min(1, (t - t0) / 900);
      setV(to * (1 - Math.pow(1 - k, 3)));
      if (k < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    const safety = setTimeout(() => setV(to), 1200);           // rAF is throttled in background tabs
    return () => { cancelAnimationFrame(raf); clearTimeout(safety); };
  }, [to]);
  return <>{v.toFixed(decimals)}{suffix}</>;
}

/* ------------------------------------------------------------ fund overlap */
const ROW = 30, SHOW = 12;

function Ring({ pct, tone }: { pct: number; tone: string }) {
  const hi = useLang((s) => s.lang) === "hi";
  const R = 52, C = 2 * Math.PI * R;
  const [p, setP] = useState(0);
  useEffect(() => { const t = setTimeout(() => setP(pct), 60); return () => clearTimeout(t); }, [pct]);
  return (
    <svg viewBox="0 0 130 130" className="ring">
      <circle cx="65" cy="65" r={R} className="ring-bg" />
      <circle cx="65" cy="65" r={R} className={`ring-fg ${tone}`} strokeDasharray={C}
              strokeDashoffset={C * (1 - Math.min(100, p) / 100)} transform="rotate(-90 65 65)" />
      <text x="65" y="64" textAnchor="middle" className="ring-num"><CountUp to={pct} decimals={0} suffix="%" /></text>
      <text x="65" y="84" textAnchor="middle" className="ring-sub">{hi ? "एक जैसे शेयर" : "same stocks"}</text>
    </svg>
  );
}

function OverlapChecker({ pick, setPick, onFunds }: {
  pick: string[];
  setPick: React.Dispatch<React.SetStateAction<string[]>>;
  onFunds?: (f: any[]) => void;
}) {
  const hi = useLang((s) => s.lang) === "hi";
  const [funds, setFunds] = useState<any[]>([]);
  const [mine, setMine] = useState<string[]>([]);
  const [d, setD] = useState<any>(null);
  const [hot, setHot] = useState<string | null>(null);

  useEffect(() => {
    fetch("/funds").then((r) => r.json()).then((x) => { setFunds(x.funds); onFunds?.(x.funds); }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { fetch("/myfunds").then((r) => r.json()).then((x) => setMine(x.funds)).catch(() => {}); }, []);
  const own = async (id: string) => {
    const on = !mine.includes(id);
    setMine((m) => (on ? [...m, id] : m.filter((x) => x !== id)));
    const r = await post("/myfunds", { fund_id: id, on });
    if (r?.funds) setMine(r.funds);
  };
  useEffect(() => {
    if (pick.length === 2) fetch(`/funds/overlap?a=${pick[0]}&b=${pick[1]}`).then((r) => r.json()).then(setD).catch(() => {});
  }, [pick]);

  const toggle = (id: string) =>
    setPick((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length === 2 ? [p[1], id] : [...p, id]));

  const rows = useMemo(() => {
    if (!d) return null;
    const A = d.a.holdings.slice(0, SHOW), B = d.b.holdings.slice(0, SHOW);
    const yb = new Map<string, number>(B.map((h: any, i: number) => [h.ticker, i]));
    const links = A.map((h: any, i: number) => ({ h, i, j: yb.get(h.ticker) }))
      .filter((l: any) => l.j !== undefined);
    return { A, B, links };
  }, [d]);

  return (
    <section className="card wide">
      <h2>{hi ? "फ़ंड ओवरलैप जाँच" : "Fund overlap checker"}</h2>
      <p className="muted">{hi ? "अलग नाम वाले दो फ़ंड चुपचाप एक ही शेयर रख सकते हैं, और आप एक ही पोर्टफ़ोलियो के लिए दो मैनेजरों को फ़ीस देते हैं। कोई भी दो फ़ंड चुनिए और साझा शेयर चमकते देखिए।"
        : "Two \"different\" funds can quietly hold the same stocks, so you pay two managers for one portfolio. Pick any two funds and watch the shared holdings light up."}</p>
      <div className="fundpick">
        {funds.map((f) => {
          const n = pick.indexOf(f.id);
          return (
            <button key={f.id} className={`fundcard ${n >= 0 ? "sel" : ""}`} onClick={() => toggle(f.id)}>
              {n >= 0 && <span className="slot">{n === 0 ? "A" : "B"}</span>}
              <span role="button" tabIndex={0} className={`ownstar ${mine.includes(f.id) ? "on" : ""}`}
                    title={hi ? "यह फ़ंड मेरे पास है (सहायक याद रखेगा)" : "I own this fund (the assistant remembers it)"}
                    onClick={(e) => { e.stopPropagation(); void own(f.id); }}
                    onKeyDown={(e) => { if (e.key === "Enter") { e.stopPropagation(); void own(f.id); } }}>{mine.includes(f.id) ? (hi ? "★ मेरे पास है" : "★ I own this") : (hi ? "☆ मेरे पास है" : "☆ I own this")}</span>
              <b>{f.name.replace("Sample ", "")}</b>
              <span className="tiny muted">{f.kind} · fee {f.er}%</span>
            </button>);
        })}
      </div>

      {d && rows && (
        <div className="ovl">
          <div className="ovl-grid">
            <div className="ovl-col">
              <div className="ovl-title">{d.a.name.replace("Sample ", "")} <span className="tiny muted">{hi ? "फ़ीस" : "fee"} {d.a.er}%</span></div>
              {rows.A.map((h: any) => {
                const shared = d.shared.some((s: any) => s.ticker === h.ticker);
                return <div key={h.ticker} style={{ height: ROW }} onMouseEnter={() => setHot(h.ticker)} onMouseLeave={() => setHot(null)}
                            className={`ovl-row ${shared ? "shared" : ""} ${hot === h.ticker ? "hot" : ""}`}>
                  <span>{short(h.name)}</span><b>{h.w}%</b></div>;
              })}
            </div>
            <svg className="ovl-links" viewBox={`0 0 160 ${ROW * SHOW}`} preserveAspectRatio="none">
              {rows.links.map((l: any, k: number) => {
                const y1 = l.i * ROW + ROW / 2, y2 = l.j * ROW + ROW / 2;
                const w = Math.max(2, l.h.w * 1.1);
                return <path key={l.h.ticker} d={`M0 ${y1} C80 ${y1}, 80 ${y2}, 160 ${y2}`} fill="none"
                             className={`ribbon ${d.verdict} ${hot === l.h.ticker ? "hot" : ""}`}
                             strokeWidth={w} style={{ animationDelay: `${k * 70}ms` }} />;
              })}
            </svg>
            <div className="ovl-col">
              <div className="ovl-title">{d.b.name.replace("Sample ", "")} <span className="tiny muted">{hi ? "फ़ीस" : "fee"} {d.b.er}%</span></div>
              {rows.B.map((h: any) => {
                const shared = d.shared.some((s: any) => s.ticker === h.ticker);
                return <div key={h.ticker} style={{ height: ROW }} onMouseEnter={() => setHot(h.ticker)} onMouseLeave={() => setHot(null)}
                            className={`ovl-row ${shared ? "shared" : ""} ${hot === h.ticker ? "hot" : ""}`}>
                  <b>{h.w}%</b><span>{short(h.name)}</span></div>;
              })}
            </div>
          </div>

          <div className="ovl-side">
            <Ring pct={d.overlap_pct} tone={d.verdict} />
            <div className={`vbanner ${d.verdict}`}>
              {d.verdict === "red" ? (hi ? "लगभग एक ही पोर्टफ़ोलियो" : "Mostly the same portfolio") : d.verdict === "amber" ? (hi ? "ध्यान देने लायक़ ओवरलैप" : "Noticeable overlap") : (hi ? "सचमुच अलग-अलग" : "Genuinely different")}
              <div className="small">{hi ? `${d.shared_stocks} शेयर साझा` : `${d.shared_stocks} stocks in common`}</div>
            </div>
            {d.verdict !== "green" && (
              <div className="statbox"><b>{inr(d.wasted_fee_per_lakh)}</b><span>{hi ? "हर 1 लाख रुपये पर साल में उन्हीं शेयरों के लिए दो बार फ़ीस जाती है" : "a year per ₹1 lakh goes to paying for the same stocks twice"}</span></div>)}
            {d.own_in_a > 0 && (
              <div className="statbox"><b>{d.own_in_a}% / {d.own_in_b}%</b>
                <span>{hi ? `आपके सीधे शेयरों (${d.own_names.join(", ")}) का इतना हिस्सा इन फ़ंडों में पहले से है` : `of what you hold directly (${d.own_names.join(", ")}) is already inside these funds`}</span></div>)}
          </div>
        </div>
      )}
      <p className="tiny muted">{hi ? "दिखाई गई होल्डिंग्स हर तरह के फ़ंड के नमूने हैं, असली फ़ैक्टशीट नहीं। ओवरलैप = दोनों फ़ंडों के साझा हर शेयर का छोटा वज़न।" : "Holdings shown are illustrative samples typical of each fund type, not live factsheets. Overlap = the smaller weight of every stock both funds hold."}</p>
    </section>
  );
}

/* ------------------------------------------------------------ scam call rehearsal */
type Step = { status: string; node?: number; line?: string; flags?: any[]; hints?: string[];
              pressure: number; feedback?: string; loss?: number; kind?: string };

function Wave({ on }: { on: boolean }) {
  return <div className={`wave ${on ? "on" : ""}`}>{Array.from({ length: 22 }, (_, i) => <i key={i} style={{ animationDelay: `${i * 55}ms` }} />)}</div>;
}

export function ScamCall({ init, compact = false }: { init?: Record<string, string | number>; compact?: boolean }) {
  const lang = useLang((s) => s.lang);
  const hi = lang === "hi";
  const [list, setList] = useState<any[]>([]);
  const [sc, setSc] = useState<any>(null);
  const [phase, setPhase] = useState<"pick" | "ring" | "call" | "end">("pick");
  const [step, setStep] = useState<Step | null>(null);
  const [shown, setShown] = useState("");
  const [seen, setSeen] = useState<any[]>([]);
  const [log, setLog] = useState<{ who: string; text: string }[]>([]);
  const [text, setText] = useState("");
  const [secs, setSecs] = useState(0);
  const [voice, setVoice] = useState(true);
  const [busy, setBusy] = useState(false);
  const [talking, setTalking] = useState(false);
  const typer = useRef<number | undefined>(undefined);

  useEffect(() => {
    // a language switch mid-call restarts it: the script is in one language only
    stopSpeech(); setPhase("pick");
    fetch(`/scamcall/scenarios?lang=${lang}`).then((r) => r.json()).then((x) => setList(x.scenarios)).catch(() => {});
  }, [lang]);
  useEffect(() => {
    if (phase !== "call") return;
    const t = setInterval(() => setSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [phase]);
  useEffect(() => () => { stopSpeech(); window.clearInterval(typer.current); }, []);

  // ---- talking back: the reply is spoken, transcribed locally, and scored like a typed one
  const [listening, setListening] = useState(false);
  const [handsfree, setHandsfree] = useState(false);
  const [callerDone, setCallerDone] = useState(false);
  const [heard, setHeard] = useState("");
  const [micMsg, setMicMsg] = useState("");
  const replyRef = useRef<(t: string) => Promise<void>>(async () => {});
  const offVoice = useRef<() => void>(() => {});

  const startListen = async () => {
    if (listening || busy) return;
    stopSpeech(); setMicMsg(""); setHeard("");
    const ok = await socket.startMic((why) => {
      if (why === "nospeech") {
        socket.cancelMic(); setListening(false);
        setMicMsg(hi ? "कुछ सुनाई नहीं दिया। माइक दबाकर बोलिए।" : "I did not hear anything. Tap the mic and speak.");
      } else void finishListen();
    });
    if (!ok) { setMicMsg(socket.micError ?? (hi ? "माइक शुरू नहीं हो सका।" : "The microphone could not be started.")); return; }
    setListening(true);
  };
  const finishListen = async () => {
    setListening(false);
    const r = await socket.stopMic(true);                     // transcript only: nothing is dispatched
    if (r?.ok && r.transcript) { setHeard(r.transcript); await replyRef.current(r.transcript); }
    else setMicMsg(r?.transcript ? (hi ? `मैंने सुना "${r.transcript}", पर साफ़ नहीं। फिर कोशिश कीजिए।` : `I heard "${r.transcript}" but not clearly. Try again.`)
                                 : (socket.micError ?? (hi ? "समझ नहीं आया। फिर बोलिए।" : "I could not make that out. Try again.")));
  };
  const toggleMic = () => (listening ? void finishListen() : void startListen());

  // Hands-free: once the caller has finished speaking, open the microphone by itself.
  useEffect(() => {
    if (phase === "call" && handsfree && callerDone && !listening && !busy && step?.status === "continue") void startListen();
  }, [phase, handsfree, callerDone, listening, busy, step]); // eslint-disable-line
  useEffect(() => { if (phase !== "call" && listening) { socket.cancelMic(); setListening(false); } }, [phase, listening]);
  useEffect(() => () => { socket.cancelMic(); offVoice.current(); }, []);
  // A Stop anywhere also ends hands-free listening: it must not reopen the microphone by itself.
  useEffect(() => onVoice((st) => { if (st === "stopped") { socket.cancelMic(); setListening(false); setHandsfree(false); } }), []);

  const say = (line: string) => {
    window.clearInterval(typer.current);
    offVoice.current(); setCallerDone(false);
    setShown(""); setTalking(true);
    let i = 0;
    typer.current = window.setInterval(() => {
      i += 2; setShown(line.slice(0, i));
      if (i >= line.length) { window.clearInterval(typer.current); setTimeout(() => setTalking(false), 400); }
    }, 28);
    if (voice && voiceReplies()) {
      allowSpeech(); speak(line, lang);
      // the caller is "done" when the audio has played to the end (or after a safety timeout)
      let started = false;
      const gen = speechGeneration();              // taken after speak(): any later bump is a Stop
      const done = () => { off(); window.clearTimeout(fallback); if (speechGeneration() === gen) setCallerDone(true); };
      const off = onVoice((st) => {
        if (st === "speaking") started = true;
        if (started && st !== "speaking") done();
      });
      const fallback = window.setTimeout(done, Math.max(8000, line.split(" ").length * 600));
      offVoice.current = () => { off(); window.clearTimeout(fallback); };
    } else {
      window.setTimeout(() => setCallerDone(true), line.length * 28 + 600);
    }
  };

  const auto = useRef(false);
  useEffect(() => {
    if (!init?.scenario || auto.current || !list.length) return;
    const s = list.find((x) => x.id === init.scenario);
    if (s) { auto.current = true; begin(s); }
  }, [list]); // eslint-disable-line
  const begin = (s: any) => { setSc(s); setPhase("ring"); setSeen([]); setLog([]); setSecs(0); setShown(""); setStep(null); };
  const accept = async () => {
    setPhase("call");
    const r: Step = await post("/scamcall/step", { scenario: sc.id, node: 0, reply: null, lang });
    setStep(r); setSeen(r.flags ?? []); setLog([{ who: "caller", text: r.line! }]); say(r.line!);
  };
  const reply = async (t: string) => {
    if (!step || busy || !t.trim()) return;
    setBusy(true); setText(""); stopSpeech();
    setLog((l) => [...l, { who: "you", text: t }]);
    const r: Step = await post("/scamcall/step", { scenario: sc.id, node: step.node ?? 0, reply: t, pressure: step.pressure, lang });
    setBusy(false);
    if (r.status === "continue") {
      setStep(r); setSeen((s) => [...s, ...(r.flags ?? [])]); setLog((l) => [...l, { who: "caller", text: r.line! }]); say(r.line!);
    } else { setStep(r); setPhase("end"); window.clearInterval(typer.current); }
  };
  replyRef.current = reply;
  const hang = () => reply(hi ? "यह ठगी है। मैं फ़ोन काट रहा हूँ।" : "I am hanging up. This is a scam.");

  // Different order every call so the safe answer is not always in the same place.
  const options = useMemo(() => {
    const h = step?.hints ?? [];
    return h.map((x, i) => ({ x, k: (i * 7 + (step?.node ?? 0) * 3) % 5 })).sort((a, b) => a.k - b.k).map((o) => o.x);
  }, [step]);

  if (phase === "pick") return (
    <section className={compact ? "fd-inline" : "card wide"}>
      {!compact && <h2>{hi ? "ठगी वाली कॉल का अभ्यास" : "Scam call rehearsal"}</h2>}
      <p className="muted">{hi ? "असली कॉल आने से पहले मना करना सीखिए। कॉल करने वाला वही चालें चलेगा जो असली ठग चलते हैं। यहाँ कुछ भी असली नहीं है और कोई डेटा आपकी मशीन से बाहर नहीं जाता।"
        : "Practise saying no before it is real. A caller will pressure you using the exact tactics scammers use. Nothing here is real and no data leaves your machine."}</p>
      <div className="scgrid">
        {list.map((s) => (
          <button key={s.id} className="sccard" onClick={() => begin(s)}>
            <span className="scicon">☎</span><b>{s.title}</b>
            <span className="small muted">{hi ? `${inr(s.loss)} तक दाँव पर · ${s.steps} चरण` : `Up to ${inr(s.loss)} at stake · ${s.steps} turns`}</span>
            <span className="scgo">{hi ? "कॉल उठाइए →" : "Receive the call →"}</span>
          </button>))}
      </div>
    </section>);

  return (
    <section className={compact ? "fd-inline" : "card wide"}>
      <div className="phone">
        <div className="phone-top">
          <div className={`avatar ${phase === "ring" ? "ringing" : ""}`}>☎</div>
          <div>
            <div className="callname">{sc.caller}</div>
            <div className="small muted">{phase === "ring" ? sc.intro : sc.number}</div>
            {phase !== "ring" && <div className="small calltime">{phase === "end" ? (hi ? "कॉल ख़त्म" : "Call ended") : `${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`}</div>}
          </div>
          <label className="small voicetog"><input type="checkbox" checked={voice} onChange={(e) => { setVoice(e.target.checked); if (!e.target.checked) stopSpeech(); }} /> {hi ? "कॉलर की आवाज़" : "caller voice"}</label>
        </div>

        {phase === "ring" && (
          <div className="ringactions">
            <button className="callbtn red" onClick={() => setPhase("pick")} aria-label="Decline">✕</button>
            <button className="callbtn green" onClick={accept} aria-label="Answer">✆</button>
          </div>)}

        {phase === "call" && step && (
          <>
            <div className="pressure"><span>{hi ? "दबाव" : "Pressure"}</span>
              <div className="pbar"><div style={{ width: `${step.pressure}%` }} className={step.pressure > 60 ? "hi" : ""} /></div></div>
            <Wave on={talking} />
            <div className="bubble caller">{shown}</div>
            <div className="flagpops">
              {(step.flags ?? []).map((f) => <span key={f.code} className="flagpop" title={f.why}>⚑ {f.label}</span>)}
            </div>
            <div className="replies">
              {options.map((o) => <button key={o} className="reply" disabled={busy} onClick={() => reply(o)}>{o}</button>)}
            </div>
            <label className="small voicetog hf"><input type="checkbox" checked={handsfree} onChange={(e) => setHandsfree(e.target.checked)} />
              {hi ? " हैंड्स-फ़्री: कॉलर के बाद माइक अपने आप खुले" : " Hands-free: the mic opens after the caller finishes"}</label>
            {(heard || micMsg) && <div className={`micmsg ${micMsg ? "warn" : ""}`}>{micMsg || `${hi ? "आपने कहा" : "You said"}: “${heard}”`}</div>}
            <div className="cmdrow">
              <input className="field" placeholder={hi ? "…या अपना जवाब लिखिए" : "…or type your own reply"} value={text} onChange={(e) => setText(e.target.value)}
                     onKeyDown={(e) => { if (e.key === "Enter") void reply(text); }} />
              <button className={`btn micbtn ${listening ? "listening" : ""}`} disabled={busy} onClick={toggleMic}
                      aria-label={listening ? (hi ? "सुनना बंद करें" : "Stop listening") : (hi ? "बोलकर जवाब दें" : "Answer by voice")}>
                <MicIcon size={16} /> {listening ? (hi ? "सुन रहा हूँ…" : "Listening…") : (hi ? "बोलिए" : "Speak")}</button>
              <button className="btn go" disabled={busy} onClick={() => reply(text)}>{hi ? "भेजें" : "Send"}</button>
              <button className="btn ghost danger" onClick={hang}>{hi ? "फ़ोन काटें" : "Hang up"}</button>
            </div>
          </>)}

        {phase === "end" && step && (
          <div className={`endcard ${step.status}`}>
            <div className="endicon">{step.status === "won" ? "🛡" : "⚠"}</div>
            <div className="endtitle">{step.status === "won" ? (hi ? "आप सुरक्षित रहे" : "You stayed safe") : (hi ? `आपके ${inr(step.loss ?? 0)} जा सकते थे` : `You would have lost ${inr(step.loss ?? 0)}`)}</div>
            <p>{step.feedback}</p>
            <h3>{hi ? "आप पर आज़माई गई चालें" : "Tactics used on you"}</h3>
            <div className="tactics">
              {[...new Map(seen.map((f) => [f.code, f])).values()].map((f: any) => (
                <div key={f.code} className="tactic"><b>⚑ {f.label}</b><span className="small muted">{f.why}</span></div>))}
            </div>
            <div className="rule">{hi ? <>तीन का नियम: <b>रुकिए</b> (कॉल पर कभी कार्रवाई मत कीजिए), <b>फ़ोन काटिए</b>, फिर <b>आधिकारिक नंबर</b> या 1930 पर फ़ोन कीजिए।</>
              : <>Rule of three: <b>Stop</b> (never act on a call), <b>Hang up</b>, then <b>call the official number</b> or 1930.</>}</div>
            <div className="cmdrow">
              <button className="btn go" onClick={() => begin(sc)}>{hi ? "फिर से" : "Try again"}</button>
              <button className="btn ghost" onClick={() => { setPhase("pick"); stopSpeech(); }}>{hi ? "दूसरा परिदृश्य" : "Another scenario"}</button>
            </div>
          </div>)}

        {phase !== "ring" && (
          <div className="calllog" aria-label="Conversation so far">
            {log.slice(0, -1).map((m, i) => <div key={i} className={`logline ${m.who}`}>{m.who === "you" ? "You: " : "Caller: "}{m.text}</div>)}
          </div>)}
      </div>
    </section>);
}

export default function Practice() {
  const hi = useLang((s) => s.lang) === "hi";
  // Lifted so the all-pairs matrix can drive the pair-by-pair checker below it.
  const [pick, setPick] = useState<string[]>(() => {
    const q = hashParams(); const a = q.get("a"), b = q.get("b");
    return a && b ? [a, b] : ["largecap_a", "bluechip_b"];
  });
  const [funds, setFunds] = useState<any[]>([]);
  const checkerRef = useRef<HTMLDivElement>(null);

  const pickPair = (a: string, b: string) => {
    setPick([a, b]);
    checkerRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <Page title="Practice" lead={hi ? "करके सीखिए: एक जैसे दो फ़ंड ख़रीदने से पहले तुलना कीजिए, और असली ठग कॉल आने से पहले अभ्यास कीजिए।" : "Learn by doing: compare funds before you buy two of the same, and rehearse a scam call before a real one arrives."}>
      <div className="grid">
        <OverlapMatrixPanel
          funds={funds}
          onPick={pickPair}
          selected={pick.length === 2 ? [pick[0], pick[1]] : null}
        />
      </div>
      <div className="grid" ref={checkerRef}>
        <OverlapChecker pick={pick} setPick={setPick} onFunds={setFunds} />
      </div>
      <div className="grid"><FeeDrag /></div>
      <div className="grid"><EmergencyMeter /></div>
      <div className="grid"><WeeklyDigest /></div>
      <div className="grid"><ScamCall /></div>
    </Page>
  );
}
