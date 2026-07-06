"use client";
import { useEffect, useRef, useState } from "react";
import {
  getTrainingStats, mineRules, getRules, updateRule,
  getDisputes, updateDisputeComment, deleteDispute,
  startDiscover, streamUrl, createRule, getRuleTokens,
} from "../app/lib/api";

const norm = (u) => (u || "").trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/\/$/, "");

function RuleCard({ rule, onChange }) {
  const [busy, setBusy] = useState(false);
  async function act(patch) {
    setBusy(true);
    try { await updateRule(rule.id, patch); await onChange(); }
    finally { setBusy(false); }
  }
  const isProposed = rule.status === "proposed";
  const isActive = rule.status === "active";
  return (
    <div className={"rule " + rule.status}>
      <div className="rule-main">
        <span className={"rtype " + rule.rule_type}>{rule.rule_type === "host" ? "HOST" : "PATTERN"}</span>
        <code className="rpat">{rule.pattern}</code>
        <span className="rstat">
          disputed <b>{rule.support}×</b> across <b>{rule.orgs}</b> universit{rule.orgs === 1 ? "y" : "ies"}
          {rule.confirms ? <> · confirmed {rule.confirms}×</> : <> · never confirmed</>}
        </span>
      </div>
      {rule.examples?.length > 0 && (
        <div className="rex">e.g. {rule.examples.slice(0, 2).map((e, i) => <code key={i}>{e}</code>)}</div>
      )}
      <div className="ractions">
        {isProposed && (
          <>
            <button className="btn small" disabled={busy}
              onClick={() => act({ status: "active", action: "deny" })}>✓ Approve — Deny</button>
            <button className="btn ghost small" disabled={busy}
              onClick={() => act({ status: "active", action: "flag" })}>Approve — Flag only</button>
            <button className="btn ghost small danger" disabled={busy}
              onClick={() => act({ status: "rejected" })}>✗ Reject</button>
          </>
        )}
        {isActive && (
          <>
            <span className={"abadge " + rule.action}>{rule.action === "deny" ? "🚫 Deny" : "⚠ Flag"}</span>
            <button className="btn ghost small" disabled={busy}
              onClick={() => act({ action: rule.action === "deny" ? "flag" : "deny" })}>
              Switch to {rule.action === "deny" ? "Flag" : "Deny"}
            </button>
            <button className="btn ghost small" disabled={busy}
              onClick={() => act({ status: "proposed" })}>Disable</button>
          </>
        )}
        {rule.status === "rejected" && (
          <>
            <span className="abadge rejected">rejected</span>
            <button className="btn ghost small" disabled={busy}
              onClick={() => act({ status: "proposed" })}>Restore</button>
          </>
        )}
      </div>
    </div>
  );
}

function DisputeRow({ d, onChanged }) {
  const [comment, setComment] = useState(d.reason || "");
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  // investigate (re-run the agent in raw mode to see WHY this URL surfaces)
  const [inv, setInv] = useState(null); // {running, log[], portals[], done}
  const esRef = useRef(null);
  // make-a-rule
  const [mk, setMk] = useState(null); // { host, tokens[] } once loaded
  const [mkMsg, setMkMsg] = useState("");
  useEffect(() => () => { if (esRef.current) esRef.current.close(); }, []);

  async function toggleMakeRule() {
    if (mk) { setMk(null); return; }
    const t = await getRuleTokens(d.url);
    setMk({ host: t.host || d.host || "", tokens: t.tokens || [] });
  }
  async function makeRule(rule_type, pattern, action) {
    setMkMsg("saving");
    try {
      const { rule } = await createRule({ rule_type, pattern, action });
      const warn = rule.confirms > 0 ? ` ⚠ also confirmed ${rule.confirms}× elsewhere` : "";
      setMkMsg(`✓ ${action === "deny" ? "Deny" : "Flag"} rule live: ${rule_type} “${pattern}”${warn}`);
      await onChanged();
    } catch (e) { setMkMsg("error: " + e.message); }
  }

  async function save() {
    setBusy(true);
    try { await updateDisputeComment(d.id, comment); setSaved(true); setTimeout(() => setSaved(false), 1500); }
    finally { setBusy(false); }
  }
  async function remove() {
    setBusy(true);
    try { await deleteDispute(d.id); await onChanged(); }
    finally { setBusy(false); }
  }

  function investigate() {
    if (inv?.running) return;
    const runUrl = d.website || (d.host ? `https://${d.host}` : d.url);
    setInv({ running: true, log: [], portals: [], done: false });
    startDiscover(runUrl, true, { name: d.university || "", orgid: d.orgid || "", suppress: false })
      .then(({ job_id }) => {
        const es = new EventSource(streamUrl(job_id));
        esRef.current = es;
        es.addEventListener("log", (ev) => {
          const m = JSON.parse(ev.data).message || "";
          setInv((s) => ({ ...s, log: [...s.log, m] }));
        });
        es.addEventListener("portal", (ev) => {
          const p = JSON.parse(ev.data).data;
          setInv((s) => ({ ...s, portals: [...s.portals, p] }));
        });
        const finish = () => { es.close(); setInv((s) => ({ ...(s || {}), running: false, done: true })); };
        es.addEventListener("close", finish);
        es.onerror = finish;
      })
      .catch((e) => setInv((s) => ({ ...(s || { log: [], portals: [] }), running: false, done: true, log: [...(s?.log || []), "ERROR: " + e.message] })));
  }

  const hit = inv?.portals?.find((p) => norm(p.url) === norm(d.url));
  return (
    <div className="dispute">
      <div className="drow1">
        <a href={d.url} target="_blank" rel="noreferrer" className="mono lnk">{d.url}</a>
        {d.category && <span className="pill">{d.category}</span>}
        {d.source && <span className="dsrc">via {d.source}</span>}
        {d.covered === "deny" && <span className="abadge deny">🚫 covered by rule “{d.covered_by}”</span>}
        {d.covered === "flag" && <span className="abadge flag">⚠ flagged by rule “{d.covered_by}”</span>}
        {!d.covered && <span className="abadge rejected">not yet in a rule</span>}
      </div>
      <div className="dmeta">{d.university || d.orgid || "—"}{d.created_at ? " · " + d.created_at.slice(0, 10) : ""}</div>
      {d.reasoning && <div className="dreason"><b>Agent reasoning:</b> {d.reasoning}</div>}
      <div className="dcomment">
        <textarea rows={2} placeholder="Why is this wrong? (note for the agent-improvement loop, e.g. 'payment gateway, not a student login')"
          value={comment} onChange={(e) => setComment(e.target.value)} />
        <div className="dactions">
          <button className="btn small" onClick={save} disabled={busy}>{saved ? "✓ Saved" : "Save note"}</button>
          <button className="btn ghost small" onClick={investigate} disabled={inv?.running}>
            {inv?.running ? "🔍 Re-running agent…" : "🔍 Investigate"}
          </button>
          <button className="btn ghost small" onClick={toggleMakeRule}>{mk ? "✕ Close" : "➕ Make a rule"}</button>
          <button className="btn ghost small danger" onClick={remove} disabled={busy}>Delete</button>
        </div>
      </div>

      {mk && (
        <div className="mkrule">
          <div className="mkhint">Turn this into a <b>global</b> rule now — applies to all future discovery. <b>Deny</b> drops it; <b>Flag</b> keeps but warns.</div>
          {mk.host && (
            <div className="mkrow">
              <span className="mklbl">Block host</span><code>{mk.host}</code>
              <span className="mkbtns">
                <button className="btn ghost small" onClick={() => makeRule("host", mk.host, "flag")}>Flag</button>
                <button className="btn ghost small danger" onClick={() => makeRule("host", mk.host, "deny")}>Deny</button>
              </span>
            </div>
          )}
          {mk.tokens.length > 0 && (
            <div className="mkrow">
              <span className="mklbl">Block path term</span>
              <span className="mktoks">
                {mk.tokens.map((t) => (
                  <span key={t} className="mktok">
                    <code>{t}</code>
                    <button className="tinybtn" title="Flag" onClick={() => makeRule("pattern", t, "flag")}>⚑</button>
                    <button className="tinybtn danger" title="Deny" onClick={() => makeRule("pattern", t, "deny")}>🚫</button>
                  </span>
                ))}
              </span>
            </div>
          )}
          {mkMsg && <div className={"mkmsg" + (mkMsg.startsWith("✓") ? " ok" : mkMsg.startsWith("error") ? " err" : "")}>{mkMsg === "saving" ? "Saving…" : mkMsg}</div>}
        </div>
      )}

      {inv && (
        <div className="invpanel">
          <div className="invhd">
            {inv.running
              ? <>Re-running the agent for <b>{d.university || d.host}</b> (raw — no suppression)…</>
              : hit
                ? <span className="invbad">⚠ Agent <b>still surfaces</b> this URL{hit.source ? <> (via <code>{hit.source}</code>)</> : null}{hit.flag ? <> — flagged “{hit.flag}”</> : null}</span>
                : <span className="invok">✓ Agent <b>no longer surfaces</b> this URL{inv.portals.length ? <> — it now returns {inv.portals.length} other portal(s)</> : ""}.</span>}
          </div>
          {hit?.reasoning && <div className="dreason"><b>Why it picked it:</b> {hit.reasoning}</div>}
          {inv.portals.length > 0 && (
            <div className="invports">
              {inv.portals.map((p, i) => (
                <div key={i} className={"invport" + (norm(p.url) === norm(d.url) ? " target" : "")}>
                  <span className="pill">{p.category || "Portal"}</span>
                  <a href={p.url} target="_blank" rel="noreferrer" className="mono lnk">{p.url}</a>
                  {p.source && <span className="dsrc">{p.source}</span>}
                </div>
              ))}
            </div>
          )}
          {inv.log.length > 0 && (
            <details className="logdetails">
              <summary>Full agent trace ({inv.log.length} lines)</summary>
              <div className="log">{inv.log.map((l, i) => <div key={i}>{l}</div>)}</div>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

export default function Training() {
  const [view, setView] = useState("rules"); // "rules" | "disputes"
  const [stats, setStats] = useState({});
  const [rules, setRules] = useState([]);
  const [disputes, setDisputes] = useState([]);
  const [dq, setDq] = useState("");
  const [mining, setMining] = useState(false);
  const [note, setNote] = useState("");

  async function refresh() {
    const [s, r] = await Promise.all([getTrainingStats(), getRules()]);
    setStats(s || {}); setRules(r.rules || []);
  }
  async function loadDisputes() {
    const d = await getDisputes({ q: dq, limit: 100 });
    setDisputes(d.disputes || []);
  }
  useEffect(() => { refresh(); }, []);
  useEffect(() => { if (view === "disputes") loadDisputes(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [view]);

  async function mine() {
    setMining(true); setNote("");
    try {
      const res = await mineRules();
      setNote(`Scanned ${res.disputes_scanned} disputes → ${res.proposed_new} new proposal(s), ${res.refreshed} refreshed.`);
      await refresh();
    } finally { setMining(false); }
  }

  const proposed = rules.filter((r) => r.status === "proposed");
  const active = rules.filter((r) => r.status === "active");
  const rejected = rules.filter((r) => r.status === "rejected");

  return (
    <div>
      <div className="tstats">
        <div className="tstat"><span className="n">{stats.disputes ?? 0}</span><span className="l">disputes</span></div>
        <div className="tstat"><span className="n">{stats.confirms ?? 0}</span><span className="l">confirmations</span></div>
        <div className="tstat"><span className="n">{stats.orgs_disputed ?? 0}</span><span className="l">universities</span></div>
        <div className="tstat"><span className="n">{stats.rules_active ?? 0}</span><span className="l">active rules</span></div>
        {view === "rules" && (
          <button className="btn" onClick={mine} disabled={mining} style={{ marginLeft: "auto" }}>
            {mining ? "🧞 Mining logs…" : "🧞 Mine rules from feedback"}
          </button>
        )}
      </div>

      <div className="seg" style={{ margin: "18px 0 6px" }}>
        <button className={view === "rules" ? "active" : ""} onClick={() => setView("rules")}>Learned rules</button>
        <button className={view === "disputes" ? "active" : ""} onClick={() => setView("disputes")}>Disputes log</button>
      </div>

      {note && view === "rules" && <p className="muted" style={{ marginTop: 10 }}>{note}</p>}

      {view === "disputes" ? (
        <div>
          <p className="tblurb">
            Every portal you marked wrong, with the agent&apos;s own reasoning for picking it. Add a
            <b> note per URL</b> explaining why it&apos;s wrong — these annotations feed the rule-miner and
            the periodic agent-improvement review. A dispute shows <b>covered by rule</b> once a learned
            rule already suppresses it.
          </p>
          <div className="row" style={{ margin: "6px 0 12px" }}>
            <input type="text" placeholder="Filter disputes by URL / university / note…" value={dq}
              onChange={(e) => setDq(e.target.value)} onKeyDown={(e) => e.key === "Enter" && loadDisputes()} />
            <button className="btn" onClick={loadDisputes}>Filter</button>
          </div>
          <p className="muted"><span className="count">{disputes.length}</span> disputed URL{disputes.length === 1 ? "" : "s"}</p>
          {disputes.map((d) => <DisputeRow key={d.id} d={d} onChanged={loadDisputes} />)}
          {disputes.length === 0 && <p className="muted" style={{ marginTop: 16 }}>No disputes yet — mark wrong portals with ✗ in Search / Universities / Discover.</p>}
        </div>
      ) : (
      <>
      <p className="tblurb">
        Genie mines your confirm/dispute log for portals that are wrong <b>across many universities</b> and
        <b> never confirmed anywhere</b> — safe global rules you approve here. <b>Deny</b> drops the portal from
        every future discovery; <b>Flag</b> keeps it but marks it likely-wrong.
      </p>

      {proposed.length > 0 && (
        <section className="rsec">
          <h3>Proposed <span className="count">({proposed.length})</span> — need your review</h3>
          {proposed.map((r) => <RuleCard key={r.id} rule={r} onChange={refresh} />)}
        </section>
      )}
      {active.length > 0 && (
        <section className="rsec">
          <h3>Active <span className="count">({active.length})</span></h3>
          {active.map((r) => <RuleCard key={r.id} rule={r} onChange={refresh} />)}
        </section>
      )}
      {rejected.length > 0 && (
        <section className="rsec">
          <h3 className="muted">Rejected <span className="count">({rejected.length})</span></h3>
          {rejected.map((r) => <RuleCard key={r.id} rule={r} onChange={refresh} />)}
        </section>
      )}
      {rules.length === 0 && (
        <p className="muted" style={{ marginTop: 20 }}>
          No rules yet. As you dispute wrong portals across universities, click <b>Mine rules</b> to
          surface global patterns Genie can learn from.
        </p>
      )}
      </>
      )}
    </div>
  );
}
