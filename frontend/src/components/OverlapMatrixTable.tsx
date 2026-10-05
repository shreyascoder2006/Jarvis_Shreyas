/** The authoritative view of all-pairs fund overlap.
 *
 * Presentational only: it is handed the matrix and renders it. Anything a future
 * 3D view shows has to appear here too -- this is the surface you can read the
 * numbers off, and the one that still works with no WebGL.
 */
import { useLang } from "../lib/lang";
import type { FundMeta, MatrixApi, Verdict } from "../lib/overlapMatrix";

const inr = (v: number) => `₹${Math.round(v).toLocaleString("en-IN")}`;
const shortFund = (n: string) => n.replace("Sample ", "");

export interface RankedPair {
  a: FundMeta; b: FundMeta; pct: number; shared: number; wasted: number; verdict: Verdict;
}

/** Every successfully loaded pair, worst overlap first. */
export function rankPairs(funds: FundMeta[], m: MatrixApi): RankedPair[] {
  const byId = new Map(funds.map((f) => [f.id, f]));
  const out: RankedPair[] = [];
  for (let i = 0; i < funds.length; i++) {
    for (let j = i + 1; j < funds.length; j++) {
      const res = m.get(funds[i].id, funds[j].id);
      if (!res?.ok) continue;
      const a = byId.get(funds[i].id), b = byId.get(funds[j].id);
      if (!a || !b) continue;
      out.push({
        a, b,
        pct: res.data.overlap_pct,
        shared: res.data.shared_stocks,
        wasted: res.data.wasted_fee_per_lakh,
        verdict: res.data.verdict,
      });
    }
  }
  return out.sort((x, y) => y.pct - x.pct);
}

export function OverlapMatrixTable({
  funds, matrix, onPick, selected,
}: {
  funds: FundMeta[];
  matrix: MatrixApi;
  onPick?: (a: string, b: string) => void;
  selected?: [string, string] | null;
}) {
  const hi = useLang((s) => s.lang) === "hi";
  const ranked = rankPairs(funds, matrix);
  const isSel = (a: string, b: string) =>
    !!selected && ((selected[0] === a && selected[1] === b) || (selected[0] === b && selected[1] === a));

  return (
    <div className="omx">
      <div className="omx-scroll">
        <table className="omx-table">
          <caption className="tiny muted">
            {hi ? "हर जोड़ी का ओवरलैप प्रतिशत। किसी भी ख़ाने पर क्लिक कर के नीचे पूरी तुलना देखिए।"
                : "Overlap % for every pair. Click any cell to open the full comparison below."}
          </caption>
          <thead>
            <tr>
              <th scope="col" className="omx-corner">
                <span className="tiny muted">{hi ? "फ़ंड" : "Fund"}</span>
              </th>
              {funds.map((f) => (
                <th key={f.id} scope="col" className="omx-head" title={shortFund(f.name)}>
                  <span>{shortFund(f.name)}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {funds.map((row) => (
              <tr key={row.id}>
                <th scope="row" className="omx-rowhead">
                  {shortFund(row.name)}
                  <span className="tiny muted"> {hi ? "फ़ीस" : "fee"} {row.er}%</span>
                </th>
                {funds.map((col) => {
                  if (row.id === col.id) return <td key={col.id} className="omx-cell self" aria-label="—">—</td>;
                  const res = matrix.get(row.id, col.id);
                  if (!res) return <td key={col.id} className="omx-cell pending" aria-busy="true" />;
                  if (!res.ok) {
                    return (
                      <td key={col.id} className="omx-cell failed" title={res.error}>
                        <span aria-label={hi ? "नहीं मिला" : "unavailable"}>—</span>
                      </td>
                    );
                  }
                  const d = res.data;
                  return (
                    <td
                      key={col.id}
                      className={`omx-cell ${d.verdict} ${isSel(row.id, col.id) ? "sel" : ""}`}
                      style={{ "--fill": `${Math.min(100, d.overlap_pct)}%` } as React.CSSProperties}
                    >
                      <button
                        type="button"
                        onClick={() => onPick?.(row.id, col.id)}
                        title={`${shortFund(row.name)} / ${shortFund(col.name)} — ${d.shared_stocks} ${hi ? "साझा शेयर" : "shared stocks"}`}
                      >
                        {Math.round(d.overlap_pct)}%
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {ranked.length > 0 && (
        <div className="omx-rank">
          <h3 className="tiny muted">
            {hi ? "सबसे ज़्यादा एक जैसे जोड़े" : "Most duplicated pairs"}
          </h3>
          <ol>
            {ranked.slice(0, 5).map((p) => (
              <li key={`${p.a.id}|${p.b.id}`}>
                <button
                  type="button"
                  className={`omx-rankrow ${p.verdict} ${isSel(p.a.id, p.b.id) ? "sel" : ""}`}
                  onClick={() => onPick?.(p.a.id, p.b.id)}
                >
                  <span className="omx-rankpct">{Math.round(p.pct)}%</span>
                  <span className="omx-rankname">
                    {shortFund(p.a.name)} <i>/</i> {shortFund(p.b.name)}
                  </span>
                  <span className="tiny muted">
                    {p.shared} {hi ? "साझा" : "shared"}
                    {p.verdict !== "green" && <> · {inr(p.wasted)} {hi ? "प्रति लाख/साल" : "per lakh/yr"}</>}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
