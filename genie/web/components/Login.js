"use client";
import { useEffect, useRef, useState } from "react";
import { API_BASE } from "../app/lib/api";
import { setToken, setIdentity } from "../app/lib/auth";

// "Sign in with Google" screen. Loads Google Identity Services, renders the
// button, exchanges the Google credential for a Genie session token via the
// backend (which enforces the allowed email domain), then calls onLogin.
export default function Login({ clientId, domain, onLogin }) {
  const btnRef = useRef(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!clientId) return;
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.defer = true;
    script.onload = () => {
      if (!window.google || !btnRef.current) return;
      window.google.accounts.id.initialize({
        client_id: clientId,
        callback: async (resp) => {
          setError("");
          try {
            const r = await fetch(`${API_BASE}/auth/google`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ credential: resp.credential }),
            });
            const d = await r.json().catch(() => ({}));
            if (r.ok && d.token) {
              setToken(d.token);
              setIdentity(d.email || "", !!d.is_admin);
              onLogin(d.token);
            } else {
              setError(d.detail || "Sign-in failed. Try again.");
            }
          } catch {
            setError("Could not reach the server. Try again.");
          }
        },
      });
      window.google.accounts.id.renderButton(btnRef.current, {
        theme: "filled_blue", size: "large", text: "signin_with", shape: "pill",
      });
    };
    document.body.appendChild(script);
    return () => script.remove();
  }, [clientId, onLogin]);

  return (
    <main style={{ minHeight: "70vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ textAlign: "center", maxWidth: 380, padding: "0 24px" }}>
        <img src="/genie.png" alt="Genie" style={{ width: 72, height: 72, marginBottom: 16 }} />
        <h1 style={{ fontSize: 24, margin: "0 0 8px" }}>Sign in to Genie</h1>
        <p style={{ color: "#667", margin: "0 0 24px", fontSize: 15 }}>
          Use your <b>@{domain}</b> Google account.
        </p>
        <div ref={btnRef} style={{ display: "flex", justifyContent: "center" }} />
        {error && <p style={{ color: "#c0392b", marginTop: 16, fontSize: 14 }}>{error}</p>}
      </div>
    </main>
  );
}
