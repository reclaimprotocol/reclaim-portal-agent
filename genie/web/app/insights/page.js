"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { getInsights, browsePortals, getCategories, updatePortalCategory } from "../lib/api";
import { countryFlag } from "../lib/flags";

const MPAGE = 50;

// Modal: portals in one category, each with a dropdown to re-categorize (persists to DB).
function CategoryModal({ cat, cats, onClose, onChanged }) {
  const [data, setData] = useState(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState({});

  async function load(off = 0) {
    setLoading(true);
    try {
      const d = await browsePortals({ offset: off, limit: MPAGE, category: cat });
      setData(d); setOffset(off);
    } finally { setLoading(false); }
  }
  useEffect(() => { load(0); /* eslint-disable-next-line */ }, [cat]);
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function change(p, newCat) {
    if (newCat === cat) return;
    setBusy((b) => ({ ...b, [p.id]: true }));
    try {
      await updatePortalCategory(p.id, newCat);
      // it left this category → drop from the list, adjust total
      setData((d) => ({ ...d, total: Math.max(0, (d.total || 1) - 1), portals: d.portals.filter((x) => x.id !== p.id) }));
      onChanged();
    } finally { setBusy((b) => ({ ...b, [p.id]: undefined })); }
  }

  const total = data?.total ?? 0;
  const page = Math.floor(offset / MPAGE) + 1;
  const pages = Math.max(1, Math.ceil(total / MPAGE));

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" style={{ maxWidth: 860 }} onClick={(e) => e.stopPropagation()}>
        <button className="modal-x" onClick={onClose} aria-label="Close">✕</button>
        <h2 style={{ marginTop: 0 }}>{cat} <span className="count">({total.toLocaleString()})</span></h2>
        <p className="muted" style={{ marginTop: 0 }}>Review the portals in this category. If one is wrong, pick the right category — it saves to the DB immediately.</p>
        <div style={{ overflowX: "auto" }}>
          <table className="tbl">
            <thead><tr><th>University</th><th>Portal URL</th><th>Category</th></tr></thead>
            <tbody>
              {(data?.portals || []).map((p) => (
                <tr key={p.id}>
                  <td>{p.university || "—"}<div className="mono" style={{ color: "var(--rc-muted)", fontSize: 11 }}>{p.domain}</div></td>
                  <td><a href={p.portal_url} target="_blank" rel="noreferrer" className="mono lnk">{p.portal_url}</a>{p.portal_verified && <span className="livechip" title="Live in production">● live</span>}</td>
                  <td>
                    <select className="state-sel" value={cat} disabled={busy[p.id]}
                      onChange={(e) => change(p, e.target.value)}>
                      {cats.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {(data?.portals || []).length === 0 && <p className="muted">No portals left in this category on this page.</p>}
        <div className="pager" style={{ marginTop: 14 }}>
          <button className="btn ghost" disabled={offset === 0 || loading} onClick={() => load(Math.max(0, offset - MPAGE))}>← Prev</button>
          <span className="muted">page {page} of {pages}</span>
          <button className="btn ghost" disabled={offset + MPAGE >= total || loading} onClick={() => load(offset + MPAGE)}>Next →</button>
        </div>
      </div>
    </div>
  );
}

function Stat({ n, label, sub, accent }) {
  return (
    <div className={"kpi" + (accent ? " accent" : "")}>
      <div className="kpi-n">{typeof n === "number" ? n.toLocaleString() : n}</div>
      <div className="kpi-l">{label}</div>
      {sub && <div className="kpi-s">{sub}</div>}
    </div>
  );
}

function Bar({ value, max }) {
  const pct = max ? Math.round((value / max) * 100) : 0;
  return <div className="ibar"><div className="ibar-fill" style={{ width: `${pct}%` }} /></div>;
}

export default function InsightsPage() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [cats, setCats] = useState([]);
  const [openCat, setOpenCat] = useState(null);
  const [dirty, setDirty] = useState(false);

  function loadInsights() { getInsights().then(setD).catch((e) => setErr(String(e.message || e))); }
  useEffect(() => { loadInsights(); getCategories().then((r) => setCats(r.categories || [])); }, []);

  function closeModal() {
    setOpenCat(null);
    if (dirty) { loadInsights(); setDirty(false); }
  }

  if (err) return <main className="wrap"><p style={{ color: "var(--rc-red)" }}>{err}</p><Link className="navlink" href="/">← Back</Link></main>;
  if (!d) return <main className="wrap"><p className="muted">Loading insights…</p></main>;

  const u = d.universities, p = d.portals;
  const catMax = Math.max(...d.by_category.map((c) => c.count), 1);

  return (
    <main className="wrap">
      <div className="ulist-bar">
        <div>
          <div className="eyebrow">● Dashboard</div>
          <h1 className="hero" style={{ fontSize: "clamp(24px,3vw,34px)" }}>Quick Insights</h1>
        </div>
        <Link className="navlink" href="/">← Back to Genie</Link>
      </div>

      {/* headline KPIs */}
      <div className="kpigrid">
        <Stat n={u.total} label="Universities in DB" />
        <Stat n={u.with_portal} label="With ≥1 portal" sub={`${u.coverage_pct}% coverage`} />
        <Stat n={u.zero_portal} label="Zero portals" sub="need discovery" />
        <Stat n={u.live} label="Live in production" accent sub="verified universities (sheet)" />
        <Stat n={u.live_mapped_in_db} label="Mapped in our DB" sub={`of ${u.live.toLocaleString()} live`} />
        <Stat n={p.total} label="Login portals (DB)" sub={`${p.avg_per_covered_uni} avg / covered uni`} />
        <Stat n={p.live} label="Live login portals" accent sub="in production (sheet)" />
      </div>

      <p className="tblurb" style={{ marginTop: 4 }}>
        <b>Live</b> numbers come from the Verified Orgs sheet (the source of truth): <b>{u.live.toLocaleString()}</b> live
        universities and <b>{p.live.toLocaleString()}</b> live login URLs. Of the {u.live.toLocaleString()} live
        universities, <b>{u.live_mapped_in_db.toLocaleString()}</b> are matched to a record in our DB by org id.
      </p>

      {/* country breakdown */}
      <h3 className="upsec">By country</h3>
      <div style={{ overflowX: "auto" }}>
        <table className="tbl">
          <thead><tr><th>Country</th><th>Universities</th><th>With portal</th><th>Portals</th></tr></thead>
          <tbody>
            {d.by_country.map((c) => (
              <tr key={c.country}>
                <td><b>{c.country}</b> <span className="flag">{countryFlag(c.country)}</span></td>
                <td>{c.universities.toLocaleString()}</td>
                <td>{c.with_portal.toLocaleString()}</td>
                <td>{c.portals.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* categories */}
      <h3 className="upsec">Portals by category <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>— click to review / fix</span></h3>
      {d.by_category.map((c) => (
        <div className="irow" key={c.category}>
          <button className="irow-l uni-link" onClick={() => setOpenCat(c.category)} title="Review portals in this category">{c.category}</button>
          <Bar value={c.count} max={catMax} />
          <div className="irow-n">{c.count.toLocaleString()}</div>
        </div>
      ))}

      {openCat && <CategoryModal cat={openCat} cats={cats} onClose={closeModal} onChanged={() => setDirty(true)} />}
    </main>
  );
}
