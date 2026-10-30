# Top500First manual portal entries: audit and discovery learnings

Updated: 2026-10-30

## Scope and evidence

Reviewed the `Top500First Portals` tab in spreadsheet `1S24-nC-ZhNPfFNE2WGVDmsj0NHJJU4x1W6i5dzY5iKU`, current Drive revision 2759. The tab has 46 non-empty Portal URL rows with blank run metadata (`Run Mode`, `Source Tab`, and `Source Row`). Those rows look manually entered, but the blank metadata alone does not prove that a prior agent run attempted each organization.

The `Top500First` source roster was checked by exact Org ID. Only two of the 46 IDs occur in that roster. On rechecking the portals themselves, both URLs are real university portals for the universities indicated by their hostnames; the issue is that the source Org ID on each manual row currently maps to a different institution:

| Org ID | Source roster identity / agent evidence | Manual row identity / URL | Assessment |
|---|---|---|---|
| 412840 | Source roster: `Universidade Santa Cecília (UNISANTA)`; fast log found `https://avaead.unisanta.br/login/index.php`. | Manual row: `Universidade De Santo Amaro (UNISA)`, labeled Brazil with `unisa.br`; portal `https://sbl-dtls.unisa.ac.za/login/index.php`. | The portal host is an official University of South Africa (UNISA) SBL/DTLS host, so the URL itself is legitimate. But the manual row says a Brazilian institution and the source Org ID maps to UNISANTA. The portal is not evidence of a missed UNISANTA URL. Resolve which UNISA the manual row intended and correct its Org ID/country/name before joining it into memory. |
| 417632 | Source roster: `Yüzüncü Yil Üniversitesi` (`yyu.edu.tr`); fast log found zero and the deep retry timed out on `yyu.edu.tr`. | Manual row: `Yüksek İhtisas Üniversitesi`; portal `https://yiuportal.yiu.edu.tr/login`. | The portal is genuinely linked from Yüksek İhtisas University’s official site and matches its `yiu.edu.tr` domain. However, source Org ID 417632 currently maps to Yüzüncü Yil (`yyu.edu.tr`), so the portal is not evidence of a missed Yüzüncü Yil URL. Resolve the Yüksek İhtisas Org ID before joining it into memory. |

The other 44 manual-signature IDs do not appear in the current `Top500First` source roster, and there is no matching agent output row for them in the inspected output. Their run status is therefore **unknown/not evidenced**, not “agent searched and missed.” Available discovery logs likewise provide no `RESULT` for those IDs. Portal discovery results above were checked in `/private/tmp/geniev3-top500first-fast-next50-resume.log` and `/private/tmp/geniev3-top500first-deep-zero-417632.log`; T&C backfill logs were not treated as portal-discovery evidence. Official-site verification confirmed Yüksek İhtisas’s YİU Portal link and UNISA South Africa's SBL-DTLS host.

Version history shows the blank-metadata rows appearing across revisions attributed to more than one editor. Drive revisions attribute a whole revision, not individual cells, so do not infer per-row authorship more precisely than the revision diff supports.

## Safe learnings for future runs

1. **Join by Org ID and verify the name/domain too.** Never map an output row to an organization by sheet position. Before accepting a manual URL, compare Org ID, canonical institution name, country, and institutional domain against the source roster. Flag conflicts instead of silently learning them.
2. **A blank run-metadata row is not a failed-agent result.** Mark it `manual / run provenance unknown` unless a discovery `RESULT` for the same Org ID is present.
3. **Search beyond the homepage.** For eligible rows, inspect linked student systems, direct login routes, institutional subdomains, and redirects to an SSO/IdP or LMS. Preserve the full actionable URL when query parameters carry the tenant/client/return destination; strip only session-specific values when storing reusable patterns.
4. **Use local-language and regional vocabulary as search seeds, not proof.** Manual entries include candidate cues such as Brazil `SIGAA`, `Magister`, `PortalSagres`, and `AOnline`; Turkey `OBS`, `UBYS`, `ÖYS/OYS`, and `derskayit`; Taiwan `ePortal`, `EIP`, and `iTouch`; Vietnam `qldt` and institution-branded SSO. These are useful query/filter candidates, but this audit did not establish same-Org-ID misses for them. Validate the login and institution association before accepting a portal.
5. **Do not promote a one-off host to a global vendor allowlist.** Host/path examples should become reusable platform patterns only after independently confirming the platform and observing it for multiple institutions. Keep ambiguous or conflicting examples quarantined.

## Status for the agent knowledge base

This review does **not** establish a verified manual-portal miss for the same source Org ID. It does verify the Yüksek İhtisas portal and the University of South Africa SBL-DTLS portal, but both manual rows need their source Org IDs reconciled before the agent can safely use them as organization-specific memory. Candidate vocabulary remains documented but has not been added to the runtime filter/whitelist, so it will not alter agent decisions. After the IDs are corrected, add only validated reusable patterns through the maintained KB source and tests.
