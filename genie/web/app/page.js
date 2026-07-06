"use client";
import { useEffect, useState } from "react";
import Search from "../components/Search";
import Discover from "../components/Discover";
import Browse from "../components/Browse";
import Curate from "../components/Curate";
import Training from "../components/Training";
import ProfileProvider from "../components/ProfileContext";
import { getCountries, getStates, exportUrl } from "./lib/api";

export default function Home() {
  const [tab, setTab] = useState("search");
  const [country, setCountry] = useState("India");
  const [countries, setCountries] = useState([]);
  const [states, setStates] = useState([]);
  const [state, setState] = useState("");

  useEffect(() => { getCountries().then((d) => setCountries(d.countries || [])); }, []);
  useEffect(() => {
    setState("");
    getStates(country).then((d) => setStates(d.states || []));
  }, [country]);

  return (
    <ProfileProvider>
    <main className="wrap">
      <div className="eyebrow">● Student login portals · powered by the Reclaim agent</div>
      <h1 className="hero">
        Find any university&apos;s <span className="accent">login portals.</span>
      </h1>

      {/* country selector — applies to Search & Browse */}
      <div className="countrybar">
        <span className="cb-label">Country</span>
        {(countries.length ? countries : [{ country: "India", count: 0 }]).map((c) => (
          <button
            key={c.country}
            className={"cpill" + (country === c.country ? " active" : "")}
            onClick={() => setCountry(c.country)}
            disabled={c.count === 0 && c.country !== "India"}
            title={c.count === 0 ? "list coming soon" : `${(c.universities ?? c.count).toLocaleString()} universities · ${(c.portals ?? 0).toLocaleString()} portals`}
          >
            {c.country} <span className="cnt">{(c.count ?? 0).toLocaleString()}</span>
          </button>
        ))}
        {states.length > 0 && (
          <select className="state-sel" value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">All states</option>
            {states.map((s) => <option key={s.state} value={s.state}>{s.state} ({s.count})</option>)}
          </select>
        )}
        <a className="cpill dl" href={exportUrl({ country, state })} download
           title={`Download ${country}${state ? " · " + state : ""} portals as CSV`}>
          ⬇ Download CSV
        </a>
      </div>

      <div className="seg">
        <button className={tab === "search" ? "active" : ""} onClick={() => setTab("search")}>Search DB</button>
        <button className={tab === "browse" ? "active" : ""} onClick={() => setTab("browse")}>Browse DB</button>
        <button className={tab === "curate" ? "active" : ""} onClick={() => setTab("curate")}>Universities</button>
        <button className={tab === "discover" ? "active" : ""} onClick={() => setTab("discover")}>Discover live</button>
        <button className={tab === "training" ? "active" : ""} onClick={() => setTab("training")}>Training</button>
      </div>

      <div className="hint">
        <span className="hint-t">Traffic chip</span>
        <span><span className="mbadge pr hi">PR</span> <b>OpenPageRank</b> — domain authority 0–10; <b>higher = stronger</b> (more-linked / established).</span>
      </div>

      <div className="card">
        {tab === "search" && <Search country={country} state={state} />}
        {tab === "browse" && <Browse country={country} state={state} />}
        {tab === "curate" && <Curate country={country} state={state} />}
        {tab === "discover" && <Discover />}
        {tab === "training" && <Training />}
      </div>
    </main>
    </ProfileProvider>
  );
}
