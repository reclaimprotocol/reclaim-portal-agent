/**
 * portal_type.js — detect a university portal's TYPE from its URL alone.
 *
 * Standalone & dependency-free. Drop into a verification UI to label a URL
 * the moment a user selects it — no network call, no page fetch needed.
 *
 * Tuned against the real "CONFIDENTIAL_Provider Activation → Universities"
 * column-G corpus (~1,800 distinct login URLs).
 *
 * Returns one of:
 *   "LMS/Moodle", "Examination Portal", "Fee Portal", "Library Portal",
 *   "Admission Portal", "Placement Portal", "ERP", "Hostel Portal",
 *   "Academic Portal", "Webmail", "Student Portal"  (last = default).
 *
 * Precedence (first match wins) is deliberate:
 *   1. Known platform host (exact registrable domain) -> its category
 *   2. LMS/Moodle score >= 2  (a learning.* host is LMS even if path has /exam)
 *   3. Webmail -> 4. Exam -> 5. Fee -> 6. Library -> 7. Admission ->
 *      8. Placement -> 9. Hostel -> 10. ERP -> 11. Academic ->
 *      12. Student-portal tokens -> 13. fallback ("Student Portal")
 *
 * Matching notes:
 *   - Short/ambiguous tokens (coe, ums, mis, sis, sim, ams, oas, fee, lib,
 *     sap, sso, auth) match a whole host LABEL only (a dotted segment), so
 *     `moderncoe.edu.in` / `scoe.*` don't false-trigger "exam".
 *   - Distinctive tokens (moodle, library, admission, placement, hostel,
 *     webmail, examination) match as substrings.
 *
 * Usage:
 *   detectPortalType("https://erp.sathyabama.ac.in/account/login")  // "ERP"
 *   detectPortalType("https://feeportal.sathyabama.ac.in/...")      // "Fee Portal"
 *   detectPortalType("https://coe.annauniv.edu/...")                // "Examination Portal"
 *   detectPortalType("https://x.ucanapply.com/...")                 // "Admission Portal"
 */

// 1. Known multi-tenant platforms: registrable domain (host === d || endsWith "."+d) -> category.
const PLATFORM_CATEGORIES = [
  // --- SIS / campus "student portal" platforms ---
  ["samarth.edu.in", "Student Portal"],
  ["digitaluniversity.ac", "Student Portal"],
  ["digitaluniversity.ac.in", "Student Portal"],
  ["digiicampus.com", "Student Portal"],
  ["sumsraj.com", "Student Portal"],
  ["mponline.gov.in", "Student Portal"],
  ["core-campus.in", "Student Portal"],
  ["campus365.io", "Student Portal"],
  ["campuspro.in", "Student Portal"],
  ["campuspro.com", "Student Portal"],
  ["linways.com", "Student Portal"],
  ["datavista.in", "Student Portal"],
  ["camu.in", "Student Portal"],
  ["mycamu.co.in", "Student Portal"],
  ["etlab.in", "Student Portal"],
  ["etlab.app", "Student Portal"],
  ["bihar-ums.com", "Student Portal"],
  ["uni1erp.in", "Student Portal"],
  ["aktu.ac.in", "Student Portal"],
  ["gndu.ac.in", "Student Portal"],
  // --- ERP-branded platforms ---
  ["edumarshal.com", "ERP"],
  ["mastersofterp.in", "ERP"],
  ["accsofterp.com", "ERP"],
  ["campx.in", "ERP"],
  ["vmedulife.com", "ERP"],
  ["dhi-edu.com", "ERP"],
  ["edupluscampus.com", "ERP"],
  ["myclassboard.com", "ERP"],
  // --- LMS ---
  ["moodle.live", "LMS/Moodle"],
  ["cognibot.in", "LMS/Moodle"],
  // --- Library ---
  ["knimbus.com", "Library Portal"],
  ["myloft.xyz", "Library Portal"],
  // --- Fee / payment gateways ---
  ["eduqfix.com", "Fee Portal"],
  ["feepayr.com", "Fee Portal"],
  ["billdesk.com", "Fee Portal"],
  // --- Admission ---
  ["ucanapply.com", "Admission Portal"],
  ["enrollonline.co.in", "Admission Portal"],
];

const LMS_HOST_TOKENS = ["moodle", "lms", "elearning", "learning", "vle", "lcms", "elearn"];
const LMS_THIRD_PARTY_HOSTS = [
  "cognibot.in", "talentlms.com", "classplusapp.com", "edmingle.com",
  "schoolyard.in", "blackboard.com", "canvaslms.com", "instructure.com",
  "brightspace.com", "moodlecloud.com",
];

// Whole-label tokens (a dotted host segment must EQUAL one of these).
const EXAM_LABELS    = ["coe", "exam", "exams", "examination", "result", "results", "hallticket", "admitcard"];
const FEE_LABELS     = ["fee", "fees", "feeportal", "feeadmin", "payment", "payments", "paydirect", "eazypay"];
const LIB_LABELS     = ["library", "lib", "opac", "libportal", "elibrary", "duls"];
const LIB_HOST_SUBSTR = ["koha"];   // kohacloud.*, *koha* -> library
const ADMISSION_LABELS = ["admission", "admissions", "apply", "enroll", "enrollonline", "ucanapply", "ucanassess"];
const PLACEMENT_LABELS = ["placement", "placements", "tnp", "career", "careers"];
const HOSTEL_LABELS  = ["hostel", "hostels"];
const ERP_LABELS     = ["erp", "sap", "ums", "iums", "mis", "sis", "sim", "ams", "oas", "academia", "accsoft", "mastersofterp"];
const WEBMAIL_LABELS = ["webmail", "owa", "roundcube", "zimbra", "mail", "email"];
const ACADEMIC_LABELS = ["academic", "academics", "grade", "grades", "records"];
const STUDENT_LABELS = ["student", "students", "studentportal", "studentlogin", "studentcorner",
                        "studentzone", "portal", "myportal", "sso", "ssologin", "auth", "uauth", "iam"];

// Path substrings (matched anywhere in the lowercased path).
const EXAM_PATHS = ["/exam", "/result", "/hall-ticket", "/hallticket", "/admit-card", "/admitcard", "/transcript", "/certificate", "/coe"];
const FEE_PATHS = ["/fee", "/fees", "/payment", "/onlinefee", "/feepayment"];
const LIB_PATHS = ["/library", "/opac", "/koha"];
const ADMISSION_PATHS = ["/admission", "/apply", "/enroll"];
const PLACEMENT_PATHS = ["/placement", "/career"];
const ERP_PATHS = ["/erp", "/ums", "/iums", "/mis"];
const ACADEMIC_PATHS = ["/academic", "/grade"];

/**
 * @param {string} rawUrl
 * @param {string} [fallback="Student Portal"]
 * @returns {string}
 */
function detectPortalType(rawUrl, fallback = "Student Portal") {
  let host = "", path = "", labels = [];
  try {
    const u = new URL(rawUrl);
    host = u.hostname.toLowerCase();
    path = (u.pathname || "").toLowerCase();
    labels = host.split(".");
  } catch {
    return fallback;
  }

  const labelEq = (toks) => labels.some((l) => toks.includes(l));
  const hostHas = (...toks) => toks.some((t) => host.includes(t));
  const pathHas = (segs) => segs.some((s) => path.includes(s));

  // 1. Known platform host.
  for (const [d, category] of PLATFORM_CATEGORIES) {
    if (host === d || host.endsWith("." + d)) return category;
  }

  // 2. LMS/Moodle (score >= 2).
  let lms = 0;
  if (path === "/login/index.php" || path.endsWith("/login/index.php")) lms += 2;
  if (path.includes("/moodle")) lms += 2;
  if (LMS_HOST_TOKENS.some((t) => host.includes(t))) lms += 2;
  if (LMS_THIRD_PARTY_HOSTS.some((h) => host === h || host.endsWith("." + h))) lms += 1;
  if (lms >= 2) return "LMS/Moodle";

  // 3. Webmail (before student/erp so mail.* doesn't read as a portal).
  if (labelEq(WEBMAIL_LABELS)) return "Webmail";

  // 4-11. Specific buckets, most-specific first.
  if (labelEq(EXAM_LABELS)      || pathHas(EXAM_PATHS))      return "Examination Portal";
  if (labelEq(FEE_LABELS)       || pathHas(FEE_PATHS))       return "Fee Portal";
  if (labelEq(LIB_LABELS)       || pathHas(LIB_PATHS) || LIB_HOST_SUBSTR.some((t)=>host.includes(t))) return "Library Portal";
  if (labelEq(ADMISSION_LABELS) || pathHas(ADMISSION_PATHS)) return "Admission Portal";
  if (labelEq(PLACEMENT_LABELS) || pathHas(PLACEMENT_PATHS)) return "Placement Portal";
  if (labelEq(HOSTEL_LABELS))                                return "Hostel Portal";
  // ERP: explicit labels/paths, OR any host label ending in "erp"
  // (dpuerp, ivyeduerp, rayaterp, sedcoerp — institution-hosted ERP installs).
  if (labelEq(ERP_LABELS) || pathHas(ERP_PATHS) || labels.some((l) => l.endsWith("erp")))
    return "ERP";
  if (labelEq(ACADEMIC_LABELS)  || pathHas(ACADEMIC_PATHS))  return "Academic Portal";

  // 12. Generic student-portal tokens (incl. SSO/auth front-ends).
  if (labelEq(STUDENT_LABELS) || pathHas(["/student", "/studentlogin", "/student-login"]))
    return "Student Portal";

  // 13. Default.
  return fallback;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { detectPortalType, PLATFORM_CATEGORIES };
}
