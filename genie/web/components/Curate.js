"use client";
import { useEffect, useRef, useState } from "react";
import {
  browseUniversities, confirmPortal, disputePortal,
  startDiscover, streamUrl, getMetrics,
} from "../app/lib/api";
import MetricsBadge from "./MetricsBadge";
import VerifiedTick from "./VerifiedTick";
import Pager from "./Pager";
import { useProfile } from "./ProfileContext";
import UniLogo from "./UniLogo";

const hostOf = (w) => (w || "").replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0];

// Compact list row (with logo) — the "List" view of the Universities tab.
function UniListRow({ uni }) {
  const openProfile = useProfile();
  return (
    <div className="ulist-row">
      <UniLogo domain={hostOf(uni.website)} name={uni.name} size={38} />
      <div className="ulist-main">
        <div className="ulist-name">
          <button className="uni-link" onClick={() => openProfile(uni.orgid)} title="Open university profile">{uni.name}</button>
          {uni.verified && <VerifiedTick size={15} />}
          {uni.verified && <span className="ulive" title="Live in production">● live</span>}
        </div>
        <div className="ulist-meta">{[uni.state, uni.city, uni.org_type].filter(Boolean).join(" · ") || "—"}</div>
        <div className="ulist-portals">
          {uni.portals.length === 0
            ? <span className="muted" style={{ fontSize: 13 }}>no portal yet</span>
            : uni.portals.map((p, i) => (
                <a key={i} className="plink" href={p.url} target="_blank" rel="noreferrer" title={p.url}>
                  {p.category || "Portal"}{p.verified && <span className="livedot" title="Live in production"> ●</span>}
                </a>
              ))}
        </div>
      </div>
    </div>
  );
}

const PAGE = 25;

// Same friendly-phase mapping used by the Discover tab.
const PHASES = [
  { re: /gemini|web search|searching|search:/i, label: "searching the web", pct: 20 },
  { re: /path|subdomain|probe|owned domain|same-host/i, label: "probing portal addresses", pct: 40 },
  { re: /sibling|homepage|crawl|extract|link/i, label: "scanning the homepage", pct: 55 },
  { re: /affiliat|parent/i, label: "checking the affiliating university", pct: 70 },
  { re: /validat|reject|keep|render|dns|http|form|membership/i, label: "validating what it found", pct: 88 },
];

function UniversityCard({ uni }) {
  const openProfile = useProfile();
  // live-editable copy of the confirmed portals for this university
  const [portals, setPortals] = useState(uni.portals || []);
  const [busy, setBusy] = useState({}); // url -> "confirming" | "disputing" | "trained" | "confirmed"
  // discovery state
  const [running, setRunning] = useState(false);
  const [done, setDone] = useState(false);
  const [pct, setPct] = useState(0);
  const [status, setStatus] = useState("getting started");
  const [candidates, setCandidates] = useState([]); // discovered, not yet confirmed
  const [metrics, setMetrics] = useState({});
  const [logLines, setLogLines] = useState([]);
  const pctRef = useRef(0);
  const esRef = useRef(null);

  useEffect(() => () => { if (esRef.current) esRef.current.close(); }, []);

  function onLog(msg) {
    setLogLines((L) => [...L, msg]);
    for (const ph of PHASES) {
      if (ph.re.test(msg) && ph.pct > pctRef.current) {
        pctRef.current = ph.pct; setPct(ph.pct); setStatus(ph.label);
      }
    }
  }

  async function discover() {
    if (running) return;
    setRunning(true); setDone(false); setCandidates([]); setMetrics({});
    setLogLines([]); setStatus("getting started"); setPct(6); pctRef.current = 6;
    try {
      const { job_id } = await startDiscover(uni.website || uni.name, true, {
        name: uni.name, orgid: uni.orgid,
      });
      const es = new EventSource(streamUrl(job_id));
      esRef.current = es;
      es.addEventListener("log", (ev) => onLog(JSON.parse(ev.data).message || ""));
      es.addEventListener("portal", (ev) => {
        const p = JSON.parse(ev.data).data;
        setCandidates((C) => (C.some((x) => x.url === p.url) ? C : [...C, p]));
        getMetrics(p.url).then((m) => { if (m) setMetrics((M) => ({ ...M, [p.url]: m })); });
      });
      es.addEventListener("result", () => { pctRef.current = 100; setPct(100); setStatus("done"); });
      const finish = () => { es.close(); setRunning(false); setDone(true); pctRef.current = 100; setPct(100); };
      es.addEventListener("close", finish);
      es.onerror = finish;
    } catch (e) {
      onLog("ERROR: " + String(e.message || e));
      setRunning(false); setDone(true);
    }
  }

  async function confirm(p, fromCandidate) {
    setBusy((b) => ({ ...b, [p.url]: "confirming" }));
    try {
      await confirmPortal({ orgid: uni.orgid, url: p.url, category: p.category || "", university: uni.name });
      setBusy((b) => ({ ...b, [p.url]: "confirmed" }));
      if (fromCandidate) {
        setCandidates((C) => C.filter((x) => x.url !== p.url));
        setPortals((P) => (P.some((x) => x.url === p.url) ? P : [...P, { ...p, status: "confirmed" }]));
      } else {
        setPortals((P) => P.map((x) => (x.url === p.url ? { ...x, status: "confirmed" } : x)));
      }
    } catch {
      setBusy((b) => ({ ...b, [p.url]: undefined }));
    }
  }

  async function dispute(p, fromCandidate) {
    // "Genie is getting trained" moment
    setBusy((b) => ({ ...b, [p.url]: "disputing" }));
    try {
      await disputePortal({ orgid: uni.orgid, url: p.url, category: p.category || "",
        source: p.source || "", reasoning: p.reasoning || "" });
      setBusy((b) => ({ ...b, [p.url]: "trained" }));
      setTimeout(() => {
        if (fromCandidate) setCandidates((C) => C.filter((x) => x.url !== p.url));
        else setPortals((P) => P.filter((x) => x.url !== p.url));
      }, 1200);
    } catch {
      setBusy((b) => ({ ...b, [p.url]: undefined }));
    }
  }

  function PortalRow({ p, fromCandidate }) {
    const st = busy[p.url];
    return (
      <div className="portal">
        <span className="pill">
          {p.category || "Portal"}{p.affiliated_from ? " · affiliated" : ""}
        </span>
        <a href={p.url} target="_blank" rel="noreferrer">{p.url}</a>
        {p.verified && <span className="livechip" title="This portal is live in production">● live</span>}
        {p.flag && <span className="flagchip" title={`Learned rule: "${p.flag}" — likely not a student portal`}>⚠ likely wrong</span>}
        <span style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 8 }}>
          <MetricsBadge m={fromCandidate ? metrics[p.url] : undefined} />
          {st === "trained" ? (
            <span className="train-note">🧞 Genie learned — won&apos;t suggest this again</span>
          ) : st === "disputing" ? (
            <span className="train-note"><span className="spinner" style={{ borderTopColor: "var(--rc-blue)", borderColor: "#0000ee44" }} />Genie is getting trained…</span>
          ) : st === "confirmed" ? (
            <span className="ok-note">✓ confirmed</span>
          ) : (
            <>
              <button className="act ok" title="Correct portal — confirm it"
                disabled={st === "confirming"} onClick={() => confirm(p, fromCandidate)}>✓</button>
              <button className="act no" title="Wrong portal — dispute & train Genie"
                onClick={() => dispute(p, fromCandidate)}>✗</button>
            </>
          )}
        </span>
      </div>
    );
  }

  const hasPortals = portals.length > 0;

  return (
    <div className={"uni" + (uni.verified ? " is-verified" : "")}>
      <h3>
        <button className="uni-link" onClick={() => openProfile(uni.orgid)} disabled={!uni.orgid}
          title="Open university profile">
          {uni.name}
        </button>
        {uni.verified && <VerifiedTick label={`${uni.name} — verified, portals live in production`} />}
        {uni.verified && <span className="ulive" title="Live in production">● live</span>}
      </h3>
      <div className="meta">
        {[uni.state, uni.city, uni.org_type].filter(Boolean).join(" · ")}
        {uni.website ? <> · <a href={uni.website} target="_blank" rel="noreferrer" className="lnk">{uni.website.replace(/^https?:\/\//, "")}</a></> : null}
      </div>

      {portals.map((p, i) => <PortalRow key={"p" + i} p={p} fromCandidate={false} />)}

      {!hasPortals && !running && !done && (
        <div className="nolabel">
          <span className="muted">No login portal on record.</span>
          <button className="btn ghost small" onClick={discover}>🔍 Discover portals</button>
        </div>
      )}

      {(running || done) && (
        <div className="genie-progress compact">
          <div className="genie-status">
            <img src="/genie.png" alt="Genie" className={"genie-icon" + (running ? " bob" : "")} />
            <span className="genie-line">
              {running
                ? <>Genie is <b>{status}</b><span className="dots"><span>.</span><span>.</span><span>.</span></span></>
                : <>Genie finished — <b>{candidates.length}</b> candidate{candidates.length === 1 ? "" : "s"} to review</>}
            </span>
            <span className="genie-pct">{pct}%</span>
          </div>
          <div className="pbar"><div className={"pbar-fill" + (running ? " live" : "")} style={{ width: `${pct}%` }} /></div>
        </div>
      )}

      {candidates.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div className="cand-hd">Discovered — tick ✓ if correct, ✗ to dispute &amp; train Genie</div>
          {candidates.map((p, i) => <PortalRow key={"c" + i} p={p} fromCandidate />)}
        </div>
      )}

      {done && candidates.length === 0 && !hasPortals && (
        <p className="muted" style={{ marginTop: 8 }}>Genie found no portal for this one.</p>
      )}

      {logLines.length > 0 && (running || done) && (
        <details className="logdetails">
          <summary>Show technical log ({logLines.length} lines)</summary>
          <div className="log">{logLines.map((l, i) => <div key={i}>{l}</div>)}</div>
        </details>
      )}
    </div>
  );
}

export default function Curate({ country = "", state = "" }) {
  const [q, setQ] = useState("");
  const [onlyMissing, setOnlyMissing] = useState(false);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [view, setView] = useState("cards"); // "cards" | "list"

  async function load(off = 0) {
    setLoading(true);
    try {
      const d = await browseUniversities({ offset: off, limit: PAGE, country, state, q, onlyMissing });
      setData(d); setOffset(off);
    } finally { setLoading(false); }
  }
  useEffect(() => { load(0); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [country, state, onlyMissing]);

  const total = data?.total ?? 0;
  const page = Math.floor(offset / PAGE) + 1;
  const pages = Math.max(1, Math.ceil(total / PAGE));

  return (
    <div>
      <div className="row" style={{ alignItems: "center" }}>
        <input type="text" placeholder="Filter universities by name…" value={q}
          onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load(0)} />
        <button className="btn" onClick={() => load(0)} disabled={loading}>{loading ? "Loading…" : "Filter"}</button>
        <label className="checkline" style={{ marginTop: 0 }}>
          <input type="checkbox" checked={onlyMissing} onChange={(e) => setOnlyMissing(e.target.checked)} />
          Only ones missing a portal
        </label>
      </div>

      <div className="ulist-bar">
        <p className="muted" style={{ margin: 0 }}>
          <span className="count">{total.toLocaleString()}</span> universit{total === 1 ? "y" : "ies"}
          {state ? ` in ${state}` : ""}{onlyMissing ? " with no portal yet" : ""} · page {page} of {pages}
        </p>
        <div className="seg small">
          <button className={view === "cards" ? "active" : ""} onClick={() => setView("cards")}>Cards</button>
          <button className={view === "list" ? "active" : ""} onClick={() => setView("list")}>List</button>
        </div>
      </div>

      {view === "cards"
        ? (data?.universities || []).map((u) => <UniversityCard key={u.orgid} uni={u} />)
        : <div className="ulist">{(data?.universities || []).map((u) => <UniListRow key={u.orgid} uni={u} />)}</div>}

      <Pager page={page} pages={pages} loading={loading} onGo={(n) => load((n - 1) * PAGE)} />
    </div>
  );
}
