"use client";
import { useState } from "react";
import { searchPortals, getMetricsBatch } from "../app/lib/api";
import MetricsBadge from "./MetricsBadge";
import VerifiedTick from "./VerifiedTick";
import { useProfile } from "./ProfileContext";
import UniLogo from "./UniLogo";

export default function Search({ country = "", state = "" }) {
  const openProfile = useProfile();
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState("");
  const [metrics, setMetrics] = useState({});
  const [view, setView] = useState("cards"); // "cards" | "list"

  async function run(e) {
    e?.preventDefault();
    if (!q.trim()) return;
    setLoading(true); setErr(""); setRes(null); setMetrics({});
    try {
      const r = await searchPortals(q.trim(), 20, country, state);
      setRes(r);
      const urls = r.results.flatMap((u) => u.portals.map((p) => p.url));
      getMetricsBatch(urls).then((mr) => {
        const map = {};
        for (const m of mr.metrics || []) map[m.url] = m;
        setMetrics(map);
      });
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <form className="row" onSubmit={run}>
        <input type="text" placeholder="Search a university… (e.g. Delhi University, aktu.ac.in)"
               value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn" disabled={loading}>{loading ? "Searching…" : "Search"}</button>
      </form>

      {err && <p style={{ color: "var(--rc-red)" }}>{err}</p>}

      {res && (
        <div style={{ marginTop: 8 }}>
          <div className="ulist-bar">
            <p className="muted" style={{ margin: 0 }}>
              <span className="count">{res.count}</span> match{res.count === 1 ? "" : "es"} for “{res.query}”
            </p>
            {res.count > 0 && (
              <div className="seg small">
                <button className={view === "cards" ? "active" : ""} onClick={() => setView("cards")}>Cards</button>
                <button className={view === "list" ? "active" : ""} onClick={() => setView("list")}>List</button>
              </div>
            )}
          </div>

          {view === "cards" && res.results.map((u, i) => (
            <div className={"uni" + (u.verified ? " is-verified" : "")} key={i}>
              <h3>
                <button className="uni-link" onClick={() => openProfile(u.orgid)} disabled={!u.orgid}
                  title="Open university profile">
                  {u.university || u.domain || u.orgid}
                </button>
                {u.verified && <VerifiedTick label={`${u.university} — verified, portals live in production`} />}
                {u.verified && <span className="ulive" title="Live in production">● live</span>}
              </h3>
              <div className="meta">
                {u.domain} · org {u.orgid} · {u.portals.length} portal{u.portals.length === 1 ? "" : "s"}
              </div>
              {u.portals.map((p, j) => (
                <div className="portal" key={j}>
                  <span className="pill">{p.category || "Portal"}</span>
                  <a href={p.url} target="_blank" rel="noreferrer">{p.url}</a>
                  {p.verified && <span className="livechip" title="This portal is live in production">● live</span>}
                  <span style={{ marginLeft: "auto" }}>
                    <MetricsBadge m={Object.keys(metrics).length ? metrics[p.url] || null : undefined} />
                  </span>
                </div>
              ))}
            </div>
          ))}

          {view === "list" && (
            <div className="ulist">
              {res.results.map((u, i) => (
                <div className="ulist-row" key={i}>
                  <UniLogo domain={u.domain} name={u.university} size={38} />
                  <div className="ulist-main">
                    <div className="ulist-name">
                      <button className="uni-link" onClick={() => openProfile(u.orgid)} disabled={!u.orgid} title="Open university profile">
                        {u.university || u.domain || u.orgid}
                      </button>
                      {u.verified && <VerifiedTick size={15} />}
                      {u.verified && <span className="ulive" title="Live in production">● live</span>}
                    </div>
                    <div className="ulist-meta">{u.domain} · {u.portals.length} portal{u.portals.length === 1 ? "" : "s"}</div>
                    <div className="ulist-portals">
                      {u.portals.map((p, j) => (
                        <a key={j} className="plink" href={p.url} target="_blank" rel="noreferrer" title={p.url}>
                          {p.category || "Portal"}{p.verified && <span className="livedot" title="Live in production"> ●</span>}
                        </a>
                      ))}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {res.count === 0 && <p className="muted">No portals in our DB yet — try the Discover tab to find them live.</p>}
        </div>
      )}
    </div>
  );
}
