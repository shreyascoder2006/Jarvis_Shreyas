/** All-pairs fund overlap, assembled in the browser.
 *
 * The backend answers one pair at a time (`GET /funds/overlap?a=&b=`). The matrix
 * view needs every pair, so we fan out here: 8 funds is 28 requests. Overlap is
 * symmetric -- practice.py scores it as the sum of min(wa, wb) -- so a sorted key
 * means we never ask for both (a,b) and (b,a).
 *
 * Three things matter more than speed:
 *   - the dev backend is one local process, so requests are capped at 4 in flight;
 *   - a pair that fails resolves to {ok:false} and never rejects, because a matrix
 *     with two holes in it is still worth reading;
 *   - results are published as each pair settles, so the view fills in rather than
 *     blocking on the slowest request.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export type Verdict = "red" | "amber" | "green";

export interface FundMeta { id: string; name: string; kind: string; er: number }
export interface Holding { ticker: string; name: string; w: number }
export interface SharedHolding { ticker: string; name: string; wa: number; wb: number; common: number }

export interface OverlapPair {
  a: { id: string; name: string; er: number; kind: string; holdings: Holding[] };
  b: { id: string; name: string; er: number; kind: string; holdings: Holding[] };
  shared: SharedHolding[];
  overlap_pct: number;
  shared_stocks: number;
  wasted_fee_per_lakh: number;
  verdict: Verdict;
}

export type PairKey = string;
export type PairResult = { ok: true; data: OverlapPair } | { ok: false; error: string };

/** Order-independent key: (a,b) and (b,a) are the same measurement. */
export function pairKey(a: string, b: string): PairKey {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

export function pairIds(key: PairKey): [string, string] {
  const [a, b] = key.split("|");
  return [a, b];
}

/** Holdings are static sample data, so a settled pair is good for the session. */
const cache = new Map<PairKey, PairResult>();
const inflight = new Map<PairKey, Promise<PairResult>>();

/** Above this, N(N-1)/2 stops being a reasonable thing to do from a browser. */
export const MAX_FUNDS = 12;
const CONCURRENCY = 4;

async function requestPair(a: string, b: string, signal: AbortSignal): Promise<PairResult> {
  try {
    const r = await fetch(`/funds/overlap?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`, { signal });
    if (!r.ok) return { ok: false, error: `HTTP ${r.status}` };
    const data = (await r.json()) as OverlapPair;
    if (typeof data?.overlap_pct !== "number") return { ok: false, error: "malformed response" };
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "request failed" };
  }
}

/** Dedupes against both the settled cache and anything already in the air. */
function loadPair(key: PairKey, signal: AbortSignal): Promise<PairResult> {
  const settled = cache.get(key);
  if (settled) return Promise.resolve(settled);

  const running = inflight.get(key);
  if (running) return running;

  const [a, b] = pairIds(key);
  const p = requestPair(a, b, signal).then((res) => {
    inflight.delete(key);
    // An abort is this component unmounting, not a fact about the pair -- leaving it
    // in the cache would poison the next mount with a failure that never happened.
    if (!signal.aborted) cache.set(key, res);
    return res;
  });
  inflight.set(key, p);
  return p;
}

/** Bounded worker pool. Publishes each result as it lands. */
async function runPool(
  keys: PairKey[],
  signal: AbortSignal,
  onSettle: (key: PairKey, res: PairResult) => void,
): Promise<void> {
  let next = 0;
  const worker = async () => {
    while (next < keys.length) {
      const key = keys[next++];
      const res = await loadPair(key, signal);
      if (signal.aborted) return;
      onSettle(key, res);
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, keys.length) }, worker));
}

export interface MatrixState {
  pairs: Map<PairKey, PairResult>;
  done: number;
  total: number;
  failed: number;
  loading: boolean;
}

export interface MatrixApi extends MatrixState {
  get(a: string, b: string): PairResult | undefined;
  retryFailed(): void;
}

export function useOverlapMatrix(fundIds: string[]): MatrixApi {
  // Sorted join: a stable identity for "this set of funds", so re-renders that pass
  // a fresh array with the same contents don't restart the fan-out.
  const idKey = useMemo(() => [...fundIds].sort().join(","), [fundIds]);

  const keys = useMemo(() => {
    const ids = [...fundIds].sort().slice(0, MAX_FUNDS);
    const out: PairKey[] = [];
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) out.push(pairKey(ids[i], ids[j]));
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idKey]);

  const [pairs, setPairs] = useState<Map<PairKey, PairResult>>(new Map());
  const [loading, setLoading] = useState(false);
  const [nonce, setNonce] = useState(0);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (keys.length === 0) { setPairs(new Map()); setLoading(false); return; }

    const ctl = new AbortController();
    abortRef.current = ctl;
    setLoading(true);
    // Seed from cache so a remount paints instantly instead of flashing empty.
    setPairs(new Map(keys.flatMap((k) => { const c = cache.get(k); return c ? [[k, c] as const] : []; })));

    runPool(keys, ctl.signal, (key, res) => {
      setPairs((prev) => { const nextMap = new Map(prev); nextMap.set(key, res); return nextMap; });
    }).finally(() => { if (!ctl.signal.aborted) setLoading(false); });

    return () => ctl.abort();
  }, [keys, nonce]);

  const retryFailed = useCallback(() => {
    let evicted = 0;
    for (const k of keys) {
      const c = cache.get(k);
      if (c && !c.ok) { cache.delete(k); evicted++; }
    }
    if (evicted) setNonce((n) => n + 1);
  }, [keys]);

  const get = useCallback((a: string, b: string) => pairs.get(pairKey(a, b)), [pairs]);

  let failed = 0;
  for (const r of pairs.values()) if (!r.ok) failed++;

  return { pairs, done: pairs.size, total: keys.length, failed, loading, get, retryFailed };
}

/** Exported for tests: the module cache outlives components by design. */
export function __clearOverlapCache(): void {
  cache.clear();
  inflight.clear();
}
