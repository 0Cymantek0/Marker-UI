"""Invariant 25 held-out routing-promotion evidence (V3.2).

Public surface:

- :data:`ROUTING_PROMOTION_CONTRACT` — frozen, content-hashed decision
  contract bound before final holdout outcomes were consumed.
- :data:`ACTOR_REGISTRY_V1` — semantic actor declarations mapping the
  masterplan's candidate / fixed-rules / best-single roles to executable
  policies.
- :func:`build_final_holdout_corpus` — the independent final evaluation
  population with machine-checkable development-evidence exclusion.
- :func:`evaluate_promotion` — the fail-closed paired comparison gate.

Nothing in this package grants production authority; the candidate policy
remains shadow/offline unless the gate's frozen criteria are met, and no
code path here flips any serving behavior.
"""

from __future__ import annotations

from .actors import (
    ACTOR_REGISTRY_V1,
    ACTOR_SCHEMA_VERSION,
    ActorDeclaration,
    ActorRegistry,
    ROLE_BEST_SINGLE,
    ROLE_CANDIDATE,
    ROLE_FIXED_RULES,
)
from .contract import (
    DECISION_INSUFFICIENT_EVIDENCE,
    DECISION_INVALID_EVIDENCE,
    DECISION_PROMOTE,
    DECISION_SHADOW,
    DECISION_VOCABULARY,
    REASON_VOCABULARY,
    ROUTING_PROMOTION_CONTRACT,
    ROUTING_PROMOTION_CONTRACT_SCHEMA_VERSION,
    PromotionContract,
    catastrophic_exposure_floor,
)

__all__ = [
    "ACTOR_REGISTRY_V1",
    "ACTOR_SCHEMA_VERSION",
    "ActorDeclaration",
    "ActorRegistry",
    "DECISION_INSUFFICIENT_EVIDENCE",
    "DECISION_INVALID_EVIDENCE",
    "DECISION_PROMOTE",
    "DECISION_SHADOW",
    "DECISION_VOCABULARY",
    "PromotionContract",
    "REASON_VOCABULARY",
    "ROLE_BEST_SINGLE",
    "ROLE_CANDIDATE",
    "ROLE_FIXED_RULES",
    "ROUTING_PROMOTION_CONTRACT",
    "ROUTING_PROMOTION_CONTRACT_SCHEMA_VERSION",
    "catastrophic_exposure_floor",
]
