"use client";
import { useEffect, useState } from "react";

// Prev / Next + "Page [n] of N  Go" jump-to-page control. `page` is 1-based.
export default function Pager({ page, pages, loading, onGo }) {
  const [val, setVal] = useState(String(page));
  useEffect(() => { setVal(String(page)); }, [page]);

  function go() {
    let n = parseInt(val, 10);
    if (isNaN(n)) { setVal(String(page)); return; }
    n = Math.max(1, Math.min(pages, n));
    if (n !== page) onGo(n);
    setVal(String(n));
  }

  return (
    <div className="pager">
      <button className="btn ghost" disabled={page <= 1 || loading} onClick={() => onGo(page - 1)}>← Prev</button>
      <div className="pager-jump">
        <span>Page</span>
        <input type="text" inputMode="numeric" value={val}
          onChange={(e) => setVal(e.target.value.replace(/[^0-9]/g, ""))}
          onKeyDown={(e) => e.key === "Enter" && go()} onBlur={go} />
        <span>of {pages.toLocaleString()}</span>
        <button className="btn ghost small" disabled={loading} onClick={go}>Go</button>
      </div>
      <button className="btn ghost" disabled={page >= pages || loading} onClick={() => onGo(page + 1)}>Next →</button>
    </div>
  );
}
