"use client";
import { useEffect, useState } from "react";
import { getEmail, logout } from "../app/lib/auth";

// Shows who's signed in + a Log out button. Reads from localStorage after
// mount to avoid a server/client hydration mismatch.
export default function UserBadge() {
  const [email, setEmail] = useState("");
  useEffect(() => { setEmail(getEmail()); }, []);
  if (!email) return null;
  return (
    <div className="userbadge">
      <span className="ub-dot" />
      <span className="ub-email" title={email}>{email}</span>
      <button className="ub-logout" onClick={logout} title="Sign out">Log out</button>
    </div>
  );
}
