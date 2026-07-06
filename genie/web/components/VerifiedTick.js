"use client";
// Blue verified seal — university/portal is live in production (Verified Orgs).
export default function VerifiedTick({ size = 17, label = "" }) {
  const title = label || "Verified — portals live in production";
  return (
    <span className="vtick" title={title} aria-label="Verified">
      <svg width={size} height={size} viewBox="0 0 24 24" role="img">
        <path fill="#1d9bf0" d="M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81c-.66-1.31-1.91-2.19-3.34-2.19s-2.67.88-3.33 2.19c-1.4-.46-2.91-.2-3.92.81s-1.26 2.52-.8 3.91c-1.31.67-2.2 1.91-2.2 3.34s.89 2.67 2.2 3.34c-.46 1.39-.21 2.9.8 3.91s2.52 1.26 3.91.81c.67 1.31 1.91 2.19 3.34 2.19s2.68-.88 3.34-2.19c1.39.45 2.9.2 3.91-.81s1.27-2.52.81-3.91c1.31-.67 2.19-1.91 2.19-3.34z" />
        <path fill="#fff" d="M9.8 17.3l-4.2-4.1L7 11.8l2.8 2.7L17 7.4l1.4 1.4z" />
      </svg>
    </span>
  );
}
