"""Shared data models — the contract every Genie surface (API, MCP, CLI) uses."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Portal:
    url: str
    category: str = ""          # Student Portal / ERP / LMS / Library / ...
    source: str = ""            # rule / claude / affiliation / db ...
    reasoning: str = ""
    affiliated_from: str = ""   # parent-university domain, if this came via affiliation
    flag: str = ""              # learned-rule warning (pattern that flagged it), if any
    verified: bool = False      # this exact portal URL is live in production (Verified Orgs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UniversityPortals:
    orgid: str
    university: str
    domain: str
    portals: list[Portal] = field(default_factory=list)
    verified: bool = False      # university is in Verified Orgs (portals live in prod)

    def to_dict(self) -> dict[str, Any]:
        return {"orgid": self.orgid, "university": self.university, "domain": self.domain,
                "verified": self.verified, "portals": [p.to_dict() for p in self.portals]}


@dataclass
class ProgressEvent:
    """Streamed during a live discovery run."""
    kind: str                    # "log" | "portal" | "result" | "error" | "done"
    message: str = ""
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "data": self.data}
