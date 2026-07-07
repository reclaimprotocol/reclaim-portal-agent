"use client";
import { useEffect, useState } from "react";
import { API_BASE } from "../app/lib/api";
import { getToken } from "../app/lib/auth";
import Login from "./Login";

// Wraps the app. Fetches /auth/config: if sign-in is enabled and there's no
// session token, shows the Login screen; otherwise renders the app. If auth is
// disabled on the server (local dev), the app is open.
export default function AuthGate({ children }) {
  const [ready, setReady] = useState(false);
  const [cfg, setCfg] = useState(null);
  const [token, setTok] = useState(null);

  useEffect(() => {
    setTok(getToken());
    fetch(`${API_BASE}/auth/config`)
      .then((r) => r.json())
      .then(setCfg)
      .catch(() => setCfg({ enabled: false }))
      .finally(() => setReady(true));
  }, []);

  if (!ready) {
    return <div style={{ padding: 48, textAlign: "center", color: "#889" }}>Loading…</div>;
  }
  if (cfg && cfg.enabled && !token) {
    return <Login clientId={cfg.client_id} domain={cfg.domain} onLogin={setTok} />;
  }
  return children;
}
