import "./globals.css";
import Link from "next/link";
import AuthGate from "../components/AuthGate";

export const metadata = {
  title: "Genie — student login portal finder",
  description: "Search known university login portals, or discover new ones with the Reclaim agent.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>
        <nav className="nav">
          <Link href="/" className="brand">
            <img src="/genie.png" alt="Genie" className="logo-img" />
            <img src="/genie-wordmark.png" alt="Genie" className="brand-wordmark" />
          </Link>
          <div className="navright">
            <Link href="/insights" className="navlink">📊 Quick Insights</Link>
            <span className="muted">Reclaim Protocol</span>
          </div>
        </nav>
        <AuthGate>{children}</AuthGate>
      </body>
    </html>
  );
}
