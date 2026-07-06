export const API_BASE =
  process.env.NEXT_PUBLIC_GENIE_API || "http://localhost:8799";

// Shared-secret API key. NOTE: NEXT_PUBLIC_* is embedded in the browser
// bundle, so this deters casual abuse but is NOT a true secret. For real
// protection, proxy calls through a Next.js route handler that holds the
// key server-side.
export const API_KEY = process.env.NEXT_PUBLIC_GENIE_API_KEY || "";

// fetch wrapper that injects the API key header and merges any caller headers.
async function afetch(url, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  return fetch(url, { ...opts, headers });
}

// append ?key= for URLs consumed without headers (SSE EventSource, downloads).
function withKey(url) {
  if (!API_KEY) return url;
  return url + (url.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(API_KEY);
}

export async function searchPortals(q, limit = 20, country = "", state = "") {
  const p = new URLSearchParams({ q, limit });
  if (country) p.set("country", country);
  if (state) p.set("state", state);
  const r = await afetch(`${API_BASE}/search?${p.toString()}`);
  if (!r.ok) throw new Error(`search failed: ${r.status}`);
  return r.json();
}

export async function getInsights() {
  const r = await afetch(`${API_BASE}/insights`);
  if (!r.ok) throw new Error(`insights failed: ${r.status}`);
  return r.json();
}

export async function getCountries() {
  const r = await afetch(`${API_BASE}/countries`);
  if (!r.ok) return { countries: [] };
  return r.json();
}

export async function getStates(country = "India") {
  const r = await afetch(`${API_BASE}/states?country=${encodeURIComponent(country)}`);
  if (!r.ok) return { states: [] };
  return r.json();
}

export async function browsePortals({ offset = 0, limit = 50, category = "", q = "", country = "", state = "" } = {}) {
  const p = new URLSearchParams({ offset, limit });
  if (category) p.set("category", category);
  if (q) p.set("q", q);
  if (country) p.set("country", country);
  if (state) p.set("state", state);
  const r = await afetch(`${API_BASE}/portals?${p.toString()}`);
  if (!r.ok) throw new Error(`browse failed: ${r.status}`);
  return r.json();
}

export async function updateUniversityWebsite(orgid, website) {
  const r = await afetch(`${API_BASE}/university/website`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ orgid, website }),
  });
  if (!r.ok) throw new Error(`website update failed: ${r.status}`);
  return r.json();
}

export async function getUniversity(orgid) {
  const r = await afetch(`${API_BASE}/university?orgid=${encodeURIComponent(orgid)}`);
  if (!r.ok) throw new Error(`university failed: ${r.status}`);
  return r.json();
}

export async function browseUniversities({ offset = 0, limit = 40, country = "", state = "", q = "", onlyMissing = false } = {}) {
  const p = new URLSearchParams({ offset, limit });
  if (country) p.set("country", country);
  if (state) p.set("state", state);
  if (q) p.set("q", q);
  if (onlyMissing) p.set("only_missing", "true");
  const r = await afetch(`${API_BASE}/universities?${p.toString()}`);
  if (!r.ok) throw new Error(`universities failed: ${r.status}`);
  return r.json();
}

export async function confirmPortal({ orgid, url, category = "", university = "", domain = "" }) {
  const r = await afetch(`${API_BASE}/confirm`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ orgid, url, category, university, domain }),
  });
  if (!r.ok) throw new Error(`confirm failed: ${r.status}`);
  return r.json();
}

export async function disputePortal({ orgid, url, reason = "", category = "", source = "", reasoning = "" }) {
  const r = await afetch(`${API_BASE}/dispute`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ orgid, url, reason, category, source, reasoning }),
  });
  if (!r.ok) throw new Error(`dispute failed: ${r.status}`);
  return r.json();
}

export async function getDisputes({ offset = 0, limit = 50, q = "" } = {}) {
  const p = new URLSearchParams({ offset, limit });
  if (q) p.set("q", q);
  const r = await afetch(`${API_BASE}/training/disputes?${p.toString()}`);
  if (!r.ok) return { disputes: [], total: 0 };
  return r.json();
}

export async function updateDisputeComment(id, comment) {
  const r = await afetch(`${API_BASE}/training/disputes/${id}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ comment }),
  });
  if (!r.ok) throw new Error(`comment failed: ${r.status}`);
  return r.json();
}

export async function deleteDispute(id) {
  const r = await afetch(`${API_BASE}/training/disputes/${id}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`delete failed: ${r.status}`);
  return r.json();
}

export async function getTrainingStats() {
  const r = await afetch(`${API_BASE}/training/stats`);
  if (!r.ok) return {};
  return r.json();
}

export async function mineRules() {
  const r = await afetch(`${API_BASE}/training/mine`, { method: "POST" });
  if (!r.ok) throw new Error(`mine failed: ${r.status}`);
  return r.json();
}

export async function getRules(status = "") {
  const p = status ? `?status=${encodeURIComponent(status)}` : "";
  const r = await afetch(`${API_BASE}/training/rules${p}`);
  if (!r.ok) return { rules: [] };
  return r.json();
}

export async function createRule({ rule_type, pattern, action = "deny" }) {
  const r = await afetch(`${API_BASE}/training/rules/create`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rule_type, pattern, action }),
  });
  if (!r.ok) throw new Error(`create rule failed: ${r.status}`);
  return r.json();
}

export async function getRuleTokens(url) {
  const r = await afetch(`${API_BASE}/training/tokens?url=${encodeURIComponent(url)}`);
  if (!r.ok) return { host: "", tokens: [] };
  return r.json();
}

export async function updateRule(id, { status, action } = {}) {
  const r = await afetch(`${API_BASE}/training/rules/${id}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, action }),
  });
  if (!r.ok) throw new Error(`rule update failed: ${r.status}`);
  return r.json();
}

export async function getCategories() {
  const r = await afetch(`${API_BASE}/categories`);
  if (!r.ok) return { categories: [] };
  return r.json();
}

export async function updatePortalCategory(id, category) {
  const r = await afetch(`${API_BASE}/portals/${id}/category`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category }),
  });
  if (!r.ok) throw new Error(`category update failed: ${r.status}`);
  return r.json();
}

export async function getMetricsBatch(urls) {
  if (!urls || urls.length === 0) return { metrics: [] };
  const r = await afetch(`${API_BASE}/metrics/batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls }),
  });
  if (!r.ok) return { metrics: [] };
  return r.json();
}

export async function getMetrics(url) {
  const r = await afetch(`${API_BASE}/metrics?url=${encodeURIComponent(url)}`);
  if (!r.ok) return null;
  return r.json();
}

export async function startDiscover(url, includeAffiliated, { name = "", orgid = "", suppress = true } = {}) {
  const r = await afetch(`${API_BASE}/discover`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, include_affiliated: includeAffiliated, name, orgid, suppress }),
  });
  if (!r.ok) throw new Error(`discover failed: ${r.status}`);
  return r.json(); // { job_id }
}

export function streamUrl(jobId) {
  return withKey(`${API_BASE}/stream/${jobId}`);
}

export async function lookupDb(url) {
  const r = await afetch(`${API_BASE}/lookup?url=${encodeURIComponent(url)}`);
  if (!r.ok) return { found: false, portals: [] };
  return r.json();
}

export function exportUrl({ country = "", state = "", category = "" } = {}) {
  const p = new URLSearchParams();
  if (country) p.set("country", country);
  if (state) p.set("state", state);
  if (category) p.set("category", category);
  const qs = p.toString();
  return withKey(`${API_BASE}/export${qs ? "?" + qs : ""}`);
}
