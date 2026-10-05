/** "Which of my funds are really the same fund?" -- every pair at once.
 *
 * The pair-by-pair checker below this panel answers "how alike are these two".
 * This one answers the question you cannot ask it: which funds across the whole
 * list are quietly duplicating each other. Clicking anything here drives that
 * checker, so this is an index into the numbers, never a replacement for them.
 */
import { useEffect, useState } from "react";

import "../styles-overlap.css";
import { useLang } from "../lib/lang";
import { MAX_FUNDS, useOverlapMatrix, type FundMeta } from "../lib/overlapMatrix";
import { OverlapMatrixTable } from "./OverlapMatrixTable";

export function OverlapMatrixPanel({
  funds, onPick, selected,
}: {
  funds: FundMeta[];
  onPick?: (a: string, b: string) => void;
  selected?: [string, string] | null;
}) {
  const hi = useLang((s) => s.lang) === "hi";
  const shown = funds.slice(0, MAX_FUNDS);
  const matrix = useOverlapMatrix(shown.map((f) => f.id));

  // Keep the fan-out off the critical path of first paint: the pair checker above
  // is what people came for, and 28 requests should not compete with it.
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setArmed(true), 400);
    return () => clearTimeout(t);
  }, []);

  if (!armed || shown.length < 2) return null;

  const pct = matrix.total ? Math.round((matrix.done / matrix.total) * 100) : 0;

  return (
    <section className="card wide omx-panel">
      <h2>{hi ? "सारे फ़ंडों का नक़्शा" : "Every fund against every other"}</h2>
      <p className="muted">
        {hi
          ? "एक-एक जोड़ी देखने के बजाय पूरा नक़्शा देखिए: कौन से फ़ंड असल में एक ही पोर्टफ़ोलियो हैं, यह एक नज़र में दिखता है।"
          : "Instead of checking one pair at a time, see the whole picture: which funds are effectively the same portfolio wearing different names."}
      </p>

      {matrix.loading && (
        <div className="omx-progress" role="status" aria-live="polite">
          <div className="omx-bar"><i style={{ width: `${pct}%` }} /></div>
          <span className="tiny muted">
            {hi ? `${matrix.done} / ${matrix.total} जोड़ियाँ` : `${matrix.done} of ${matrix.total} pairs`}
          </span>
        </div>
      )}

      {matrix.failed > 0 && !matrix.loading && (
        <p className="tiny omx-warn">
          {hi ? `${matrix.failed} जोड़ियाँ नहीं मिलीं।` : `${matrix.failed} pair${matrix.failed > 1 ? "s" : ""} could not be loaded.`}{" "}
          <button type="button" className="omx-retry" onClick={matrix.retryFailed}>
            {hi ? "फिर कोशिश करें" : "Retry"}
          </button>
        </p>
      )}

      <OverlapMatrixTable funds={shown} matrix={matrix} onPick={onPick} selected={selected} />

      <p className="tiny muted">
        {hi
          ? "दिखाई गई होल्डिंग्स हर तरह के फ़ंड के नमूने हैं, असली फ़ैक्टशीट नहीं। ओवरलैप = दोनों फ़ंडों के साझा हर शेयर का छोटा वज़न।"
          : "Holdings shown are illustrative samples typical of each fund type, not live factsheets. Overlap = the smaller weight of every stock both funds hold."}
      </p>
    </section>
  );
}
