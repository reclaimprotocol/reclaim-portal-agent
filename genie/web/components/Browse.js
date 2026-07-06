"use client";
import { useEffect, useState } from "react";
import { browsePortals, getCategories, getMetricsBatch } from "../app/lib/api";
import MetricsBadge from "./MetricsBadge";
import VerifiedTick from "./VerifiedTick";
import Pager from "./Pager";
import { useProfile } from "./ProfileContext";
import UniLogo from "./UniLogo";

const PAGE = 50;
const hostOf = (w) => (w || "").replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0];

export default function Browse({ country = "", state = "" }) {
  const [cats, setCats] = useState([]);
  const [cat, setCat] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [metrics, setMetrics] = useState({}); // url -> metric ({} = loading map)
  const [view, setView] = useState("list"); // "list" (table) | "cards"
  const openProfile = useProfile();

  useEffect(() => { getCategories().then((d) => setCats(d.categories || [])); }, []);

  async function load(off = 0) {
    setLoading(true);
    setMetrics({});
    try {
      const d = await browsePortals({ offset: off, limit: PAGE, category: cat, q, country, state });
      setData(d);
      setOffset(off);
      // fetch traffic metrics for this page (cached server-side after first time)
      const urls = (d.portals || []).map((p) => p.portal_url);
      getMetricsBatch(urls).then((mr) => {
        const map = {};
        for (const m of mr.metrics || []) map[m.url] = m;
        setMetrics(map);
      });
    } finally { setLoading(false); }
  }
  // load on mount and whenever the category, country, or state changes
  useEffect(() => { load(0); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [cat, country, state]);

  const total = data?.total ?? 0;
  const page = Math.floor(offset / PAGE) + 1;
  const pages = Math.max(1, Math.ceil(total / PAGE));

  return (
    <div>
      <div className="row" style={{ alignItems: "center" }}>
        <select value={cat} onChange={(e) => setCat(e.target.value)}
          style={{ padding: "13px 14px", borderRadius: 12, border: "1px solid var(--rc-line)", fontSize: 15 }}>
          <option value="">All categories</option>
          {cats.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <input type="text" placeholder="Filter by name / domain / URL…" value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && load(0)} />
        <button className="btn" onClick={() => load(0)} disabled={loading}>
          {loading ? "Loading…" : "Filter"}
        </button>
      </div>

      <div className="ulist-bar">
        <p className="muted" style={{ margin: 0 }}>
          <span className="count">{total.toLocaleString()}</span> portals{cat ? ` in “${cat}”` : ""} · page {page} of {pages}
        </p>
        <div className="seg small">
          <button className={view === "cards" ? "active" : ""} onClick={() => setView("cards")}>Cards</button>
          <button className={view === "list" ? "active" : ""} onClick={() => setView("list")}>List</button>
        </div>
      </div>

      {view === "list" ? (
        <div style={{ overflowX: "auto", marginTop: 6 }}>
          <table className="tbl">
            <thead>
              <tr><th>University</th><th>Category</th><th>Portal URL</th><th>Traffic</th></tr>
            </thead>
            <tbody>
              {(data?.portals || []).map((p) => (
                <tr key={p.id}>
                  <td>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                      <button className="uni-link" onClick={() => openProfile(p.orgid)}
                        disabled={!p.orgid} title="Open university profile">
                        {p.university || "—"}
                      </button>
                      {p.verified && <VerifiedTick size={15} />}
                    </span>
                    <div className="mono" style={{ color: "var(--rc-muted)", fontSize: 11 }}>{p.domain}</div>
                  </td>
                  <td><span className="pill">{p.category || "Portal"}</span></td>
                  <td>
                    <a href={p.portal_url} target="_blank" rel="noreferrer" className="mono lnk">{p.portal_url}</a>
                    {p.portal_verified && <span className="livechip" title="Live in production">● live</span>}
                  </td>
                  <td><MetricsBadge m={Object.keys(metrics).length ? metrics[p.portal_url] || null : undefined} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="cardgrid">
          {(data?.portals || []).map((p) => (
            <div className={"pcard" + (p.verified ? " is-verified" : "")} key={p.id}>
              <div className="pcard-head">
                <UniLogo domain={hostOf(p.domain)} name={p.university} size={34} />
                <div style={{ minWidth: 0 }}>
                  <div className="pcard-uni">
                    <button className="uni-link" onClick={() => openProfile(p.orgid)} disabled={!p.orgid} title="Open university profile">
                      {p.university || "—"}
                    </button>
                    {p.verified && <VerifiedTick size={14} />}
                  </div>
                  <div className="mono pcard-dom">{p.domain}</div>
                </div>
              </div>
              <div className="pcard-body">
                <span className="pill">{p.category || "Portal"}</span>
                {p.portal_verified && <span className="livechip" title="Live in production">● live</span>}
                <a href={p.portal_url} target="_blank" rel="noreferrer" className="mono lnk pcard-url">{p.portal_url}</a>
              </div>
              <div className="pcard-foot">
                <MetricsBadge m={Object.keys(metrics).length ? metrics[p.portal_url] || null : undefined} />
              </div>
            </div>
          ))}
        </div>
      )}

      <Pager page={page} pages={pages} loading={loading} onGo={(n) => load((n - 1) * PAGE)} />
    </div>
  );
}
