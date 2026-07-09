"use client";
import { useEffect, useState } from "react";
import { getAdminActivity } from "../app/lib/api";

// Admin-only dashboard: who signed in, and what they searched (with results).
// The /admin/activity endpoint is server-side gated to admin emails, so a
// non-admin who reaches this just sees an error.
export default function Admin() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true); setErr("");
    getAdminActivity()
      .then(setData)
      .catch((e) => setErr(String(e.message || e)))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, []);

  const fmt = (t) => (t ? t.replace("T", " ").replace(/(\+00:00|Z)$/, " UTC") : "");

  if (loading) return <p className="muted">Loading activity…</p>;
  if (err) return <p className="muted">Couldn&apos;t load activity — {err} (admins only).</p>;

  const logins = data?.logins || [];
  const searches = data?.searches || [];

  return (
    <div className="admin">
      <div className="admin-head">
        <h3>Activity</h3>
        <button className="btn ghost" onClick={load}>↻ Refresh</button>
      </div>

      <h4 className="admin-sub">Searches ({searches.length})</h4>
      <div className="scrollx">
        <table className="admin-tbl">
          <thead>
            <tr><th>When (UTC)</th><th>User</th><th>Searched</th><th>#</th><th>Result portals</th></tr>
          </thead>
          <tbody>
            {searches.map((s, i) => (
              <tr key={i}>
                <td className="nowrap">{fmt(s.created_at)}</td>
                <td>{s.email || "—"}</td>
                <td className="mono">{s.query_url}</td>
                <td className="num">{s.result_count}</td>
                <td>
                  {(s.results || []).length === 0
                    ? <span className="muted">none</span>
                    : (s.results || []).map((u, j) => (
                        <a key={j} className="admin-url" href={u} target="_blank" rel="noreferrer">{u}</a>
                      ))}
                </td>
              </tr>
            ))}
            {searches.length === 0 && <tr><td colSpan={5} className="muted">No searches yet.</td></tr>}
          </tbody>
        </table>
      </div>

      <h4 className="admin-sub">Logins ({logins.length})</h4>
      <div className="scrollx">
        <table className="admin-tbl">
          <thead><tr><th>When (UTC)</th><th>User</th></tr></thead>
          <tbody>
            {logins.map((l, i) => (
              <tr key={i}><td className="nowrap">{fmt(l.created_at)}</td><td>{l.email}</td></tr>
            ))}
            {logins.length === 0 && <tr><td colSpan={2} className="muted">No logins yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
