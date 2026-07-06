"use client";
import { useState } from "react";

// University logo with graceful fallback: Clearbit → Google favicon → initials.
export default function UniLogo({ domain, name, size = 40 }) {
  const chain = domain
    ? [`https://logo.clearbit.com/${domain}`, `https://www.google.com/s2/favicons?sz=128&domain=${domain}`]
    : [];
  const [i, setI] = useState(0);
  const initials = (name || "?").split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  const style = { width: size, height: size };
  if (i >= chain.length) {
    return <div className="ulogo initials" style={{ ...style, fontSize: Math.round(size * 0.38) }}>{initials}</div>;
  }
  return <img className="ulogo" style={style} src={chain[i]} alt={name} onError={() => setI((n) => n + 1)} />;
}
