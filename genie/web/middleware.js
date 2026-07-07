import { NextResponse } from "next/server";

// Gate the whole app behind HTTP Basic Auth so it's not open to anyone with
// the URL. Credentials come from env vars set on Vercel:
//   GENIE_SITE_USER / GENIE_SITE_PASS
// If either is unset (e.g. local dev), auth is disabled and the app is open.
export function middleware(req) {
  const user = process.env.GENIE_SITE_USER;
  const pass = process.env.GENIE_SITE_PASS;
  if (!user || !pass) return NextResponse.next(); // auth disabled

  const header = req.headers.get("authorization") || "";
  if (header.startsWith("Basic ")) {
    try {
      const [u, p] = atob(header.slice(6)).split(":");
      if (u === user && p === pass) return NextResponse.next();
    } catch {
      /* fall through to 401 */
    }
  }
  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Genie"' },
  });
}

// Run on every route except Next.js internals and the favicon.
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
