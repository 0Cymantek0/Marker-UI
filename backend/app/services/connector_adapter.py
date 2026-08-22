"""Provider-neutral connector adapter contract (PR71B, amendment 16B.7).

External notifications are hints. A connector adapter maps ONE
provider's real change-delivery mechanics — Drive-style change feeds
with page tokens, Graph-style delta queries, webhook-plus-poll — onto
the small vocabulary below, and nothing provider-specific leaks past
this boundary into the convergence core.

What the core demands of every adapter (the failure-mode floor derived
from real provider behavior):

* changes may be **duplicated** — the same logical change can arrive
  again under the same or a different delivery identity;
* arrival order is not causal order — deliveries can be **out of
  order**, so a comparable provider sequence (``provider_seq``) must be
  supplied when the provider exposes one, and ``ordering="none"``
  declared honestly when it does not;
* removal is explicit — deletion and loss-of-access both surface as
  ``removed`` (the core treats both as security-relevant);
* incremental state can become untrustworthy — an expired/invalid
  token, a provider reset, a detected gap, or the adapter's own
  inability to prove continuity surfaces as an explicit
  :class:`InvalidStreamSignal`, never as silently-continued polling;
* reconciliation must be a deterministic full/bounded scan — the same
  ``full_scan`` contract that powers tests must be implementable by a
  real provider's enumeration API.

The scripted :class:`ScriptedProvider` below is the deterministic
reference provider: it reproduces every failure mode above on demand so
the convergence proofs do not depend on a real SaaS account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ORDERING_SEQUENCED",
    "ORDERING_NONE",
    "ProviderChange",
    "ChangePage",
    "ScanPage",
    "ItemSnapshot",
    "InvalidStreamSignal",
    "InvalidReason",
    "ConnectorAdapter",
    "ScriptedProvider",
]

#: The provider exposes a comparable per-item sequence number.
ORDERING_SEQUENCED = "sequenced"
#: The provider cannot prove revision order for this change class.
ORDERING_NONE = "none"

#: Why incremental state can no longer be trusted (P5 vocabulary).
INVALID_REASON_TOKEN_EXPIRED = "token_expired"
INVALID_REASON_TOKEN_INVALID = "token_invalid"
INVALID_REASON_PROVIDER_RESET = "provider_reset"
INVALID_REASON_GAP_DETECTED = "gap_detected"
INVALID_REASON_CONTINUITY_UNPROVEN = "continuity_unproven"

InvalidReason = str


class InvalidStreamSignal(Exception):
    """Incremental continuation is untrustworthy; reconcile instead.

    Raised by adapters from ``fetch_changes`` (and embedded in pages via
    ``ChangePage.invalid_reason`` where a page-object style fits the
    provider better). The convergence core never responds by silently
    adopting a later token — it parks the stream in
    ``reconciliation_required`` with this reason recorded durably.
    """

    def __init__(self, reason: InvalidReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"invalid stream continuation: {reason}: {detail}")


@dataclass(frozen=True)
class ProviderChange:
    """One normalized provider change event.

    ``event_id`` is the stable provider event identity (delivery GUID,
    change id) used for durable receipt dedupe. ``item_id`` is the
    stable provider item identity that survives moves/renames.
    ``revision`` is the provider's own revision token (ETag, version,
    generation) pinned to the fetched bytes when ``content`` is set.
    """

    event_id: str
    item_id: str
    kind: str  # content_changed | policy_changed | removed | restored | moved
    revision: str | None = None
    seq: int | None = None
    content: bytes | None = None
    suffix: str = ".pdf"
    media_type: str | None = None
    policy_facts: Mapping[str, Any] = field(default_factory=dict)
    new_location: str | None = None
    ordering: str = ORDERING_SEQUENCED

    def __post_init__(self) -> None:
        if self.kind not in (
            "content_changed",
            "policy_changed",
            "removed",
            "restored",
            "moved",
        ):
            raise ValueError(f"unknown provider change kind: {self.kind!r}")
        if self.ordering not in (ORDERING_SEQUENCED, ORDERING_NONE):
            raise ValueError(f"unknown ordering capability: {self.ordering!r}")
        if self.kind == "content_changed" and self.content is None:
            raise ValueError("content_changed requires fetched content bytes")
        if self.kind == "moved" and not self.new_location:
            raise ValueError("moved requires new_location")
        if self.ordering == ORDERING_SEQUENCED and self.seq is None:
            raise ValueError(
                "sequenced changes must carry the provider's comparable seq"
            )


@dataclass(frozen=True)
class ChangePage:
    """One page of incremental changes plus its continuation state.

    ``next_cursor`` names the provider checkpoint AFTER this page. It is
    only safe to durably adopt when ``complete`` is True — a truncated
    page (pagination not exhausted) must resume, not skip.
    """

    changes: tuple[ProviderChange, ...]
    next_cursor: str | None
    complete: bool = True
    invalid_reason: InvalidReason | None = None
    invalid_detail: str = ""
    page_seq: int | None = None


@dataclass(frozen=True)
class ScanPage:
    """One page of a reconciliation scan (authoritative current truth)."""

    changes: tuple[ProviderChange, ...]
    resume_token: str | None  # None only on the final page
    final: bool = False
    fresh_cursor: str | None = None  # the post-reset checkpoint (final page)


@dataclass(frozen=True)
class ItemSnapshot:
    """Authoritative current provider truth for one item (T6 route)."""

    item_id: str
    present: bool
    revision: str | None = None
    seq: int | None = None
    content: bytes | None = None
    suffix: str = ".pdf"
    media_type: str | None = None
    policy_facts: Mapping[str, Any] = field(default_factory=dict)
    location: str | None = None


class ConnectorAdapter:
    """Base class / structural contract for provider adapters.

    Subclass and implement the three methods; the convergence core
    (:mod:`app.services.connector_ingestion`) never talks to a provider
    except through this shape.
    """

    provider_name: str = "abstract"

    async def fetch_changes(self, cursor: str | None) -> ChangePage:
        """Incremental changes after *cursor* (``None`` = from now/full)."""
        raise NotImplementedError

    async def fetch_item(self, item_id: str) -> ItemSnapshot | None:
        """Current authoritative truth for one item (ordering-free route)."""
        raise NotImplementedError

    async def full_scan(self, resume: str | None) -> ScanPage:
        """Deterministic reconciliation scan, page-wise restartable."""
        raise NotImplementedError


class ScriptedProvider(ConnectorAdapter):
    """Deterministic reference provider for convergence proofs.

    The test harness scripts a sequence of pages per poll round; the
    provider hands them out one per ``fetch_changes`` call and remembers
    which checkpoint each round ended at. Failure modes (invalid token,
    reset, gaps, reordering, duplicates) are scripted explicitly — the
    provider never invents nondeterminism.
    """

    provider_name = "scripted"

    def __init__(self, *, account: str = "acct") -> None:
        self.account = account
        self._rounds: list[ChangePage] = []
        self._issued_cursors: set[str] = set()
        self._item_state: dict[str, ItemSnapshot] = {}
        self._scan_by_resume: dict[str | None, ScanPage] = {}
        self._invalid_signal: tuple[str, str] | None = None
        self.fetch_calls = 0
        self.item_fetches: list[str] = []

    # ------------------------------------------------------------------
    # scripting surface (tests only)
    # ------------------------------------------------------------------

    def script_round(self, page: ChangePage) -> None:
        self._rounds.append(page)
        if page.next_cursor:
            self._issued_cursors.add(page.next_cursor)

    def script_invalid_signal(self, reason: str, detail: str = "") -> None:
        """Make the next ``fetch_changes`` raise InvalidStreamSignal."""
        self._invalid_signal = (reason, detail)

    def seed_item(self, snapshot: ItemSnapshot) -> None:
        self._item_state[snapshot.item_id] = snapshot

    def script_scan(self, pages: list[ScanPage]) -> None:
        """Key scan pages by resume token, restart-position-safe.

        Page ``i``'s ``resume_token`` names page ``i+1``; ``None``
        before the first page. A process restarted mid-scan replays the
        same deterministic page for any durably-recorded resume token.
        """
        self._scan_by_resume = {None: pages[0]} if pages else {}
        for previous, page in zip(pages, pages[1:]):
            self._scan_by_resume[previous.resume_token] = page

    def cursor_is_known(self, cursor: str | None) -> bool:
        return cursor is None or cursor == "" or cursor in self._issued_cursors

    # ------------------------------------------------------------------
    # adapter contract
    # ------------------------------------------------------------------

    async def fetch_changes(self, cursor: str | None) -> ChangePage:
        self.fetch_calls += 1
        if self._invalid_signal is not None:
            reason, detail = self._invalid_signal
            self._invalid_signal = None
            raise InvalidStreamSignal(reason, detail)
        if not self.cursor_is_known(cursor):
            raise InvalidStreamSignal(
                INVALID_REASON_TOKEN_INVALID,
                f"cursor {cursor!r} was never issued by this provider",
            )
        if not self._rounds:
            return ChangePage(changes=(), next_cursor=cursor, complete=True)
        return self._rounds.pop(0)

    async def fetch_item(self, item_id: str) -> ItemSnapshot | None:
        self.item_fetches.append(item_id)
        return self._item_state.get(item_id)

    async def full_scan(self, resume: str | None) -> ScanPage:
        page = self._scan_by_resume.get(resume)
        if page is None:
            # A scan past its script is an empty final page: durable
            # idempotence for repeated reconciliation (T18).
            return ScanPage(changes=(), resume_token=None, final=True, fresh_cursor=resume)
        return page
