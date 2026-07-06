"use client";
// Presentational metric chip. `m` is a metrics object (or undefined while loading).
export default function MetricsBadge({ m }) {
  if (m === undefined) return <span className="mbadge load">…</span>;
  if (!m) return null;
  const pr = m.opr_authority;
  const title = `OpenPageRank authority: ${pr ?? "n/a"}/10  (rank ${m.opr_rank ?? "n/a"})`;
  // color the PR chip by authority tier
  const tier = pr == null ? "" : pr >= 6 ? "hi" : pr >= 4 ? "mid" : "lo";
  return (
    <span className="mwrap" title={title}>
      <span className={`mbadge pr ${tier}`}>PR {pr == null ? "–" : pr}</span>
    </span>
  );
}
