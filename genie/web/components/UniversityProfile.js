"use client";
import { useEffect, useState } from "react";
import { getUniversity, getMetricsBatch, updateUniversityWebsite } from "../app/lib/api";
import MetricsBadge from "./MetricsBadge";
import VerifiedTick from "./VerifiedTick";
import UniLogo from "./UniLogo";

const hostOf = (w) => (w || "").replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0];

export default function UniversityProfile({ orgid, onClose }) {
  const [u, setU] = useState(null);
  const [metrics, setMetrics] = useState({});
  const [err, setErr] = useState("");
  const [wsite, setWsite] = useState("");
  const [savingW, setSavingW] = useState(false);

  async function saveWebsite() {
    if (!wsite.trim()) return;
    setSavingW(true);
    try {
      const res = await updateUniversityWebsite(u.orgid, wsite.trim());
      setU((cur) => ({ ...cur, website: res.website, domain: hostOf(res.website) }));
    } finally { setSavingW(false); }
  }

  useEffect(() => {
    let alive = true;
    getUniversity(orgid).then((d) => {
      if (!alive) return;
      setU(d);
      const urls = (d.portals || []).map((p) => p.url);
      getMetricsBatch(urls).then((mr) => {
        if (!alive) return;
        const m = {}; for (const x of mr.metrics || []) m[x.url] = x; setMetrics(m);
      });
    }).catch((e) => {
      const msg = String(e.message || e);
      setErr(msg.includes("404") ? "This university isn’t saved in the database yet — confirm a portal for it first." : msg);
    });
    return () => { alive = false; };
  }, [orgid]);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <button className="modal-x" onClick={onClose} aria-label="Close">✕</button>
        {err && <p style={{ color: "var(--rc-red)" }}>{err}</p>}
        {!u && !err && <p className="muted">Loading…</p>}
        {u && (
          <>
            <div className="uphead">
              <UniLogo domain={u.domain} name={u.name} size={68} />
              <div className="upinfo">
                <h2>
                  {u.name}
                  {u.verified && <VerifiedTick size={20} label={`${u.name} — verified, portals live in production`} />}
                  {u.verified && <span className="ulive" title="Live in production">● live</span>}
                </h2>
                <div className="upmeta">
                  {[u.country, u.state, u.city, u.org_type].filter(Boolean).join(" · ")}
                </div>
                {u.website ? (
                  <a className="upweb" href={u.website} target="_blank" rel="noreferrer">
                    {u.website.replace(/^https?:\/\//, "")}
                  </a>
                ) : (
                  <div className="addweb">
                    <span className="muted" style={{ fontSize: 13 }}>No website on record.</span>
                    <input type="text" placeholder="add website… e.g. du.ac.bd" value={wsite}
                      onChange={(e) => setWsite(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && saveWebsite()} />
                    <button className="btn small" onClick={saveWebsite} disabled={savingW || !wsite.trim()}>
                      {savingW ? "Saving…" : "Save website"}
                    </button>
                  </div>
                )}
              </div>
            </div>

            <h3 className="upsec">Portals <span className="count">({u.portals.length})</span></h3>
            {u.portals.length === 0 && <p className="muted">No portals on record yet.</p>}
            {u.portals.map((p, i) => (
              <div className="portal" key={i}>
                <span className="pill">{p.category || "Portal"}</span>
                <a href={p.url} target="_blank" rel="noreferrer" className="mono lnk">{p.url}</a>
                {p.verified && <span className="livechip" title="Live in production">● live</span>}
                <span style={{ marginLeft: "auto" }}>
                  <MetricsBadge m={Object.keys(metrics).length ? metrics[p.url] || null : undefined} />
                </span>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
