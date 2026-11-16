"""Per-directory-page coverage declarations with verifiable stability boundaries.

Discovery reads a tree of directory pages (portal -> dataset -> version).  A
"missing" judgement about an object is only meaningful when the parent listing
page was fully read in one consistent round.  This module records, for every
directory page, a coverage declaration binding the page URL, its parent page,
the digest of the content that was read, every raw link candidate with its
outcome, and the digests of the child pages that were visited.  A declaration
is only usable for absence proofs when it is both complete (no candidate was
dropped for budget, failure, or invalid-link reasons) and stable (a later
confirmation re-read returned the identical digest).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .errors import BudgetExceeded, DiscoveryError, TransportError
from .util import sha256_bytes

# Link decision outcomes.
ACCEPTED = "accepted"
BUDGET_SKIPPED = "budget_skipped"
CHANNEL_FILTERED = "channel_filtered"
FETCH_FAILED = "fetch_failed"
INVALID_LINK = "invalid_link"
DUPLICATE = "duplicate"

# Outcomes that make the parent listing incomplete (absence cannot be proven).
INCOMPLETE_OUTCOMES = frozenset({BUDGET_SKIPPED, FETCH_FAILED, INVALID_LINK})

# Stability states for a declaration.
UNVERIFIED = "unverified"
CONFIRMED = "confirmed"
REJECTED = "rejected"

LEVELS = ("portal", "dataset", "version")


@dataclass(frozen=True)
class LinkDecision:
    ref: str
    url: str
    outcome: str
    reason: str

    def as_dict(self) -> dict:
        return {"ref": self.ref, "url": self.url, "outcome": self.outcome, "reason": self.reason}


@dataclass
class PageEvidence:
    """One recorded read of a directory page (text kept only in memory)."""

    level: str
    page_url: str
    parent_url: str | None
    fetch_seq: int
    fetched_at: str
    content_digest: str
    text: str = ""
    links: list[dict] = field(default_factory=list)


@dataclass
class CoverageDeclaration:
    """A verifiable statement about one directory listing page."""

    level: str
    subject: str
    page_url: str
    parent_url: str | None
    content_digest: str
    first_fetch_seq: int
    fetched_at: str
    candidates: int = 0
    accepted: int = 0
    decisions: list[LinkDecision] = field(default_factory=list)
    children: dict[str, str] = field(default_factory=dict)
    stability: str = UNVERIFIED
    verified_fetch_seq: int | None = None
    source: PageEvidence | None = None

    @property
    def complete(self) -> bool:
        return not any(d.outcome in INCOMPLETE_OUTCOMES for d in self.decisions)

    @property
    def usable(self) -> bool:
        return self.complete and self.stability == CONFIRMED


class CoverageTracker:
    """Accumulates page evidence and declarations for one discovery round."""

    def __init__(self, clock, on_event: Callable[[str, str, dict], None]) -> None:
        self.clock = clock
        self.on_event = on_event
        self.evidence: list[PageEvidence] = []
        self.declarations: list[CoverageDeclaration] = []
        self._first_digest: dict[str, str] = {}

    def _emit(self, code: str, subject: str, detail: dict) -> None:
        self.on_event(code, subject, detail)

    def record_page(
        self, level: str, page_url: str, parent_url: str | None, text: str, fetch_seq: int
    ) -> PageEvidence:
        digest = sha256_bytes(text.encode("utf-8"))
        first = self._first_digest.get(page_url)
        if first is None:
            self._first_digest[page_url] = digest
        elif first != digest:
            self._emit(
                "page.changed",
                page_url,
                {"first_digest": first, "later_digest": digest, "fetch_seq": fetch_seq},
            )
        evidence = PageEvidence(
            level, page_url, parent_url, fetch_seq, self.clock.now(), digest, text
        )
        self.evidence.append(evidence)
        return evidence

    def begin(self, level: str, subject: str, evidence: PageEvidence) -> CoverageDeclaration:
        declaration = CoverageDeclaration(
            level=level,
            subject=subject,
            page_url=evidence.page_url,
            parent_url=evidence.parent_url,
            content_digest=evidence.content_digest,
            first_fetch_seq=evidence.fetch_seq,
            fetched_at=evidence.fetched_at,
            source=evidence,
        )
        self.declarations.append(declaration)
        return declaration

    def decide(
        self, declaration: CoverageDeclaration, ref: str, url: str, outcome: str, reason: str = ""
    ) -> None:
        declaration.decisions.append(LinkDecision(ref, url, outcome, reason))

    def close(self, declaration: CoverageDeclaration) -> None:
        declaration.candidates = len(declaration.decisions)
        declaration.accepted = sum(1 for d in declaration.decisions if d.outcome == ACCEPTED)
        if declaration.source is not None:
            declaration.source.links = [d.as_dict() for d in declaration.decisions]

    def attach_children(self) -> None:
        """Bind each declaration to the first-read digests of its child pages."""
        for declaration in self.declarations:
            for decision in declaration.decisions:
                digest = self._first_digest.get(decision.url)
                if digest is not None and decision.url != declaration.page_url:
                    declaration.children[decision.url] = digest

    def verify_stability(self, fetch: Callable[[CoverageDeclaration], PageEvidence]) -> None:
        """Confirm-or-reject policy: one re-read per page; a mismatch rejects.

        Pages are never stitched across rounds: a changed page invalidates the
        declaration for this round instead of adopting newer content.
        """
        for declaration in self.declarations:
            try:
                evidence = fetch(declaration)
            except BudgetExceeded:
                remaining = sum(1 for d in self.declarations if d.stability == UNVERIFIED)
                self._emit(
                    "coverage.verify_budget",
                    declaration.page_url,
                    {"unverified": remaining},
                )
                break
            except (DiscoveryError, TransportError) as exc:
                declaration.stability = REJECTED
                self._emit(
                    "coverage.stability_rejected", declaration.page_url, {"reason": str(exc)}
                )
                continue
            if evidence.content_digest == declaration.content_digest:
                declaration.stability = CONFIRMED
                declaration.verified_fetch_seq = evidence.fetch_seq
            else:
                declaration.stability = REJECTED
                self._emit(
                    "coverage.stability_rejected",
                    declaration.page_url,
                    {
                        "reason": "page content changed during traversal",
                        "first_digest": declaration.content_digest,
                        "later_digest": evidence.content_digest,
                    },
                )
