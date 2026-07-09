"use client";
import { useRef, useState } from "react";
import {
  streamUrl, startDiscover, getMetrics, getMetricsBatch,
  confirmPortal, disputePortal, lookupDb,
} from "../app/lib/api";
import MetricsBadge from "./MetricsBadge";
import VerifiedTick from "./VerifiedTick";
import { useProfile } from "./ProfileContext";

// Map raw agent log lines -> a friendly stage + a forward-only progress %.
const PHASES = [
  { re: /gemini|web search|searching|search:/i, label: "searching the web for its portals", pct: 20 },
  { re: /path|subdomain|probe|owned domain|same-host/i, label: "probing common portal addresses", pct: 40 },
  { re: /sibling|homepage|crawl|extract|link/i, label: "scanning the homepage for portal links", pct: 55 },
  { re: /affiliat|parent/i, label: "checking the affiliating university", pct: 70, aff: true },
  { re: /validat|reject|keep|render|dns|http|form|membership/i, label: "validating the portals it found", pct: 88 },
];

export default function Discover() {
  const openProfile = useProfile();
  const [url, setUrl] = useState("");
  const [affiliated, setAffiliated] = useState(true);
  const [running, setRunning] = useState(false);
  const [portals, setPortals] = useState([]); // [{url, category, _db}]
  const [metrics, setMetrics] = useState({});
  const [done, setDone] = useState(false);
  const [source, setSource] = useState(null); // "db" | "live" | null
  const [ranLive, setRanLive] = useState(false);
  const [pct, setPct] = useState(0);
  const [status, setStatus] = useState("getting started");
  const [checked, setChecked] = useState(0);
  const [logLines, setLogLines] = useState([]);
  const [meta, setMeta] = useState(null); // { orgid, university, domain }
  const [busy, setBusy] = useState({});    // url -> "confirming"|"confirmed"|"disputing"|"trained"
  const esRef = useRef(null);
  const logRef = useRef(null);
  const pctRef = useRef(0);
  const affiliatedRef = useRef(true); // mirrors `affiliated` for the log handler

  function onLog(msg) {
    setLogLines((L) => [...L, msg]);
    requestAnimationFrame(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; });
    for (const ph of PHASES) {
      // Skip the affiliating-university stage entirely when the checkbox is off.
      if (ph.aff && !affiliatedRef.current) continue;
      if (ph.re.test(msg) && ph.pct > pctRef.current) {
        pctRef.current = ph.pct; setPct(ph.pct); setStatus(ph.label);
      }
    }
    if (/reject|keep|validat/i.test(msg)) setChecked((n) => n + 1);
  }

  // DB-first: check what we already have before spending a live agent run.
  async function run(e) {
    e?.preventDefault();
    if (!url.trim() || running) return;
    setRunning(false); setDone(false); setPortals([]); setMetrics({});
    setLogLines([]); setChecked(0); setStatus("getting started");
    setMeta(null); setBusy({}); setSource(null); setRanLive(false);
    setPct(0); pctRef.current = 0;

    let hit = { found: false, portals: [] };
    try { hit = await lookupDb(url.trim()); } catch { /* fall through to live */ }

    if (hit.found && hit.portals.length > 0) {
      const ps = hit.portals.map((p) => ({ ...p, _db: true }));
      setPortals(ps);
      setMeta({ orgid: hit.orgid || "", university: hit.university || "", domain: hit.domain || "", verified: !!hit.verified });
      setSource("db"); setDone(true);
      const urls = ps.map((p) => p.url);
      getMetricsBatch(urls).then((mr) => {
        const m = {}; for (const x of mr.metrics || []) m[x.url] = x; setMetrics(m);
      });
    } else {
      // nothing on record — go straight to a live agent run
      runLive({ orgid: "", university: "" });
    }
  }

  // Live agent discovery. Appends to whatever is already shown (dedup by url).
  function runLive(seed = {}) {
    if (running) return;
    setRunning(true); setDone(false); setSource("live"); setRanLive(true);
    setLogLines([]); setChecked(0); setStatus("getting started");
    setPct(6); pctRef.current = 6;
    affiliatedRef.current = affiliated;
    const orgid = seed.orgid ?? meta?.orgid ?? "";
    const name = seed.university ?? meta?.university ?? "";
    startDiscover(url.trim(), affiliated, { name, orgid })
      .then(({ job_id }) => {
        const es = new EventSource(streamUrl(job_id));
        esRef.current = es;
        es.addEventListener("log", (ev) => onLog(JSON.parse(ev.data).message || ""));
        es.addEventListener("portal", (ev) => {
          const p = JSON.parse(ev.data).data;
          setPortals((P) => (P.some((x) => x.url === p.url) ? P : [...P, { ...p, _db: false }]));
          getMetrics(p.url).then((m) => { if (m) setMetrics((M) => ({ ...M, [p.url]: m })); });
        });
        es.addEventListener("result", (ev) => {
          pctRef.current = 100; setPct(100); setStatus("done");
          try {
            const d = JSON.parse(ev.data).data || {};
            setMeta((prev) => ({
              orgid: d.orgid || prev?.orgid || "",
              university: d.university || prev?.university || "",
              domain: d.domain || prev?.domain || "",
              verified: d.verified || prev?.verified || false,
            }));
          } catch { /* */ }
        });
        es.addEventListener("error", (ev) => {
          try { onLog("ERROR: " + (JSON.parse(ev.data).message || "stream error")); } catch { /* */ }
        });
        const finish = () => { es.close(); setRunning(false); setDone(true); pctRef.current = 100; setPct(100); };
        es.addEventListener("close", finish);
        es.onerror = finish;
      })
      .catch((e) => { onLog("ERROR: " + String(e.message || e)); setRunning(false); setDone(true); });
  }

  async function confirm(p) {
    if (!meta) return;
    setBusy((b) => ({ ...b, [p.url]: "confirming" }));
    try {
      await confirmPortal({ orgid: meta.orgid, url: p.url, category: p.category || "",
        university: meta.university, domain: meta.domain });
      setBusy((b) => ({ ...b, [p.url]: "confirmed" }));
    } catch { setBusy((b) => ({ ...b, [p.url]: undefined })); }
  }

  async function dispute(p) {
    if (!meta) return;
    setBusy((b) => ({ ...b, [p.url]: "disputing" }));
    try {
      await disputePortal({ orgid: meta.orgid, url: p.url, category: p.category || "",
        source: p.source || "", reasoning: p.reasoning || "" });
      setBusy((b) => ({ ...b, [p.url]: "trained" }));
      setTimeout(() => setPortals((P) => P.filter((x) => x.url !== p.url)), 1200);
    } catch { setBusy((b) => ({ ...b, [p.url]: undefined })); }
  }

  const liveCount = portals.filter((p) => !p._db).length;

  return (
    <div>
      <form className="row" onSubmit={run}>
        <input type="text" placeholder="Paste a college website… (e.g. https://www.gtu.ac.in)"
               value={url} onChange={(e) => setUrl(e.target.value)} />
        <button className="btn" disabled={running}>
          {running ? "Discovering…" : "Find portals"}
        </button>
      </form>
      <label className="checkline">
        <input type="checkbox" checked={affiliated} onChange={(e) => setAffiliated(e.target.checked)} />
        Also find the affiliating university&apos;s portals
      </label>

      {source === "db" && (
        <div className="db-note">
          ⚡ Found <b>{portals.filter((p) => p._db).length}</b> portal{portals.filter((p) => p._db).length === 1 ? "" : "s"} in our database
          {meta?.university ? <> for <b>{meta.university}</b></> : null} — shown instantly, no live search needed.
        </div>
      )}

      {(running || (done && ranLive)) && (
        <div className="genie-progress">
          <div className="genie-status">
            <img src="/genie.png" alt="Genie" className={"genie-icon" + (running ? " bob" : "")} />
            <span className="genie-line">
              {running
                ? <>Genie is <b>{status}</b><span className="dots"><span>.</span><span>.</span><span>.</span></span></>
                : <>Genie finished — found <b>{liveCount}</b> live portal{liveCount === 1 ? "" : "s"}</>}
            </span>
            <span className="genie-pct">{pct}%</span>
          </div>
          <div className="pbar">
            <div className={"pbar-fill" + (running ? " live" : "")} style={{ width: `${pct}%` }} />
          </div>
          <div className="pmeta">
            {checked} candidate{checked === 1 ? "" : "s"} checked · <b>{liveCount}</b> kept
          </div>
        </div>
      )}

      {portals.length > 0 && (
        <div className={"uni" + (meta?.verified ? " is-verified" : "")} style={{ marginTop: 18 }}>
          <h3>
            {meta?.orgid
              ? <button className="uni-link" onClick={() => openProfile(meta.orgid)} title="Open university profile">{meta?.university || "Portals"}</button>
              : (meta?.university || "Portals")}
            {" "}<span className="count">({portals.length})</span>
            {meta?.verified && <VerifiedTick label={`${meta.university} — verified, portals live in production`} />}
            {meta?.verified && <span className="ulive" title="Live in production">● live</span>}
          </h3>
          {meta?.domain && <div className="meta">{meta.domain}</div>}
          {done && meta && <div className="cand-hd">Review — tick ✓ if correct, ✗ to dispute &amp; train Genie</div>}
          {portals.map((p, j) => {
            const st = busy[p.url];
            return (
              <div className="portal" key={j}>
                <span className="pill">{p.category || "Portal"}{p.affiliated_from ? " · affiliated" : ""}</span>
                <a href={p.url} target="_blank" rel="noreferrer">{p.url}</a>
                <span className={"origin " + (p._db ? "saved" : "new")}
                      title={p._db ? "Saved in our database" : "Newly discovered by live search — not verified"}>{p._db ? "saved" : "new"}</span>
                {p.verified && <span className="livechip" title="This portal is live in production">● live</span>}
                {p.flag && <span className="flagchip" title={`Learned rule: "${p.flag}" — likely not a student portal`}>⚠ likely wrong</span>}
                <span style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 8 }}>
                  <MetricsBadge m={metrics[p.url]} />
                  {done && meta && (
                    st === "trained" ? (
                      <span className="train-note">🧞 Genie learned — won&apos;t suggest this again</span>
                    ) : st === "disputing" ? (
                      <span className="train-note"><span className="spinner" style={{ borderTopColor: "var(--rc-blue)", borderColor: "#0000ee44" }} />Genie is getting trained…</span>
                    ) : st === "confirmed" ? (
                      <span className="ok-note">✓ confirmed</span>
                    ) : (
                      <>
                        <button className="act ok" title="Correct portal — confirm it"
                          disabled={st === "confirming"} onClick={() => confirm(p)}>✓</button>
                        <button className="act no" title="Wrong portal — dispute & train Genie"
                          onClick={() => dispute(p)}>✗</button>
                      </>
                    )
                  )}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {/* Offer a live search when results came from the DB (or to look for more). */}
      {done && !running && (source === "db" || (source === "live" && portals.length > 0)) && (
        <div style={{ marginTop: 14 }}>
          <button className="btn ghost" onClick={() => runLive()}>
            🔎 {source === "db" ? "Discover more portals (live search)" : "Search live again for more"}
          </button>
          <span className="muted" style={{ marginLeft: 12, fontSize: 13 }}>
            Runs the full agent — may surface portals not yet in our database.
          </span>
        </div>
      )}

      {done && portals.length === 0 && (
        <p className="muted" style={{ marginTop: 14 }}>No student portal found for this site.</p>
      )}

      {logLines.length > 0 && (
        <details className="logdetails">
          <summary>Show technical log ({logLines.length} lines)</summary>
          <div className="log" ref={logRef}>{logLines.map((l, i) => <div key={i}>{l}</div>)}</div>
        </details>
      )}
    </div>
  );
}
