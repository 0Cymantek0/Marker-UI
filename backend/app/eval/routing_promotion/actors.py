"""Semantic actor registry for the invariant 25 promotion comparison.

Invariant 25 (masterplan 23C.3) requires a candidate EVC-style routing
policy to beat *deterministic fixed rules* and the *best available single
engine* on genuinely held-out data.  Names alone cannot establish that the
compared executables are those actors; this module declares the mapping
from each masterplan role to an executable policy, why the semantics match,
and where the implementation lives, and fails closed if a declared policy
is not executable in the current tree.

Mapping summary (full rationale embedded per actor below):

- candidate: ``dependency_aware_policy`` — the evidence-driven continuation
  policy that decides accept/abstain from observed witness results using
  empirical risk bands and conservative upper-loss gates (masterplan 7B.2
  level 3 "EVC-based continuation from observed validator results", 7C.1).
- fixed rules: ``deterministic_source_native_only`` — the deterministic
  native-first rule (masterplan 7B.4 "deterministic native-vs-OCR rule").
- best single engine: ``best_single_witness`` — the declared strongest
  single engine witness (masterplan 7B.4 "fixed best-single-engine route").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.eval.verification_risk.baselines import BASELINE_NAMES
from app.eval.verification_risk.common import VerificationRiskError
from app.eval.verification_risk.identity import _identity

ACTOR_SCHEMA_VERSION = "marker.routing_promotion.actors.v1"

ROLE_CANDIDATE = "candidate"
ROLE_FIXED_RULES = "fixed_rules"
ROLE_BEST_SINGLE = "best_single"
ROLE_VOCABULARY: tuple[str, ...] = (ROLE_CANDIDATE, ROLE_FIXED_RULES, ROLE_BEST_SINGLE)

#: Offline containment: the executable policy implementations live in the
#: offline evaluation package; the production kernel maintains its own
#: conservative verification gate (``app/kernel/verification_risk``) and
#: imports none of the candidate policy.  A dedicated test re-proves this.
ACTOR_IMPLEMENTATION_MODULE = "app.eval.verification_risk.baselines"


@dataclass(frozen=True)
class ActorDeclaration:
    """One masterplan role bound to an executable policy."""

    role: str
    policy_id: str
    implementation_module: str
    instantiation: str
    mapping_rationale: str
    masterplan_references: tuple[str, ...]

    def validate(self) -> None:
        if self.role not in ROLE_VOCABULARY:
            raise VerificationRiskError(f"unknown actor role {self.role!r}")
        if self.policy_id not in BASELINE_NAMES:
            raise VerificationRiskError(
                f"actor {self.role!r} policy {self.policy_id!r} is not an executable baseline"
            )
        if not self.implementation_module.strip():
            raise VerificationRiskError(f"actor {self.role!r} lacks an implementation module")
        if not self.instantiation.strip():
            raise VerificationRiskError(f"actor {self.role!r} lacks an instantiation rule")
        if not self.mapping_rationale.strip():
            raise VerificationRiskError(f"actor {self.role!r} lacks a mapping rationale")
        if len(self.masterplan_references) < 2:
            raise VerificationRiskError(
                f"actor {self.role!r} must cite at least two governing masterplan sections"
            )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "policy_id": self.policy_id,
            "implementation_module": self.implementation_module,
            "instantiation": self.instantiation,
            "mapping_rationale": self.mapping_rationale,
            "masterplan_references": list(self.masterplan_references),
        }

    @property
    def semantic_identity(self) -> str:
        return _identity(self.semantic_payload())


@dataclass(frozen=True)
class ActorRegistry:
    """The three invariant-25 actors, exactly once each."""

    schema_version: str
    actors: tuple[ActorDeclaration, ...]

    def validate(self) -> None:
        if self.schema_version != ACTOR_SCHEMA_VERSION:
            raise VerificationRiskError(f"unsupported actor schema {self.schema_version!r}")
        if len(self.actors) != len(ROLE_VOCABULARY):
            raise VerificationRiskError(
                f"actor registry must contain exactly {len(ROLE_VOCABULARY)} actors"
            )
        roles = [actor.role for actor in self.actors]
        if sorted(roles) != sorted(ROLE_VOCABULARY):
            raise VerificationRiskError(
                f"actor registry roles {sorted(roles)!r} do not match {ROLE_VOCABULARY!r}"
            )
        for actor in self.actors:
            actor.validate()
        policies = [actor.policy_id for actor in self.actors]
        if len(set(policies)) != len(policies):
            raise VerificationRiskError("each actor must bind a distinct executable policy")

    def actor_for(self, role: str) -> ActorDeclaration:
        for actor in self.actors:
            if actor.role == role:
                return actor
        raise VerificationRiskError(f"no actor declared for role {role!r}")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "actors": [actor.semantic_payload() for actor in self.actors],
        }

    @property
    def semantic_identity(self) -> str:
        return _identity(self.semantic_payload())


ACTOR_REGISTRY_V1 = ActorRegistry(
    schema_version=ACTOR_SCHEMA_VERSION,
    actors=(
        ActorDeclaration(
            role=ROLE_CANDIDATE,
            policy_id="dependency_aware_policy",
            implementation_module=ACTOR_IMPLEMENTATION_MODULE,
            instantiation=(
                "evaluate_baselines(corpus, slice_id=...) -> baselines"
                "[BASELINE_NAMES[4]] with upstream dependency-aware defaults "
                "(dependency-diverse selection; empirical gate min_samples=5, "
                "max_joint_error_upper=0.6 frozen as the candidate configuration)"
            ),
            mapping_rationale=(
                "The dependency-aware acceptance policy is the only executable "
                "expected-value-of-evidence continuation policy in the tree: it "
                "decides accept vs abstain from observed witness results using "
                "empirical joint-error risk bands, conservative upper-loss gates, "
                "and authority rules, exactly the bounded EVC continuation "
                "semantics the masterplan describes. It is not a trained model; "
                "masterplan 7C.1 makes empirical risk bands the launch-time "
                "EVC estimator whose promotion must be gated by this comparison."
            ),
            masterplan_references=(
                "7B.2 level 3: EVC-based continuation from observed validator results",
                "7C.1: a learned risk/EVC estimator is promoted only after shadow "
                "evaluation proves lower catastrophic, cost, and routing regret "
                "than simple rules",
                "23C.3 invariant 25: EVC-style routing remains shadow/offline "
                "until it beats fixed rules and best-single-engine baselines",
            ),
        ),
        ActorDeclaration(
            role=ROLE_FIXED_RULES,
            policy_id="deterministic_source_native_only",
            implementation_module=ACTOR_IMPLEMENTATION_MODULE,
            instantiation=(
                "evaluate_baselines(corpus, slice_id=...) -> baselines"
                "[BASELINE_NAMES[0]]; deterministic vote over source-native / "
                "deterministic / human-reviewed witnesses only"
            ),
            mapping_rationale=(
                "The source-native-only policy is the deterministic fixed rule of "
                "the continuation layer: prefer the cheap source-of-truth path and "
                "abstain where it is absent. This is the simpler policy the "
                "masterplan biases toward; promotion of the candidate requires a "
                "material utility gain over it, not a tie."
            ),
            masterplan_references=(
                "7B.4: deterministic native-vs-OCR rule is a router baseline that "
                "must always remain runnable",
                "7A.3 / 14C.5: if fixed rules provide at least 98% of the "
                "candidate's utility, keep the rules",
                "23C.3 invariant 25: fixed rules are a required comparator",
            ),
        ),
        ActorDeclaration(
            role=ROLE_BEST_SINGLE,
            policy_id="best_single_witness",
            implementation_module=ACTOR_IMPLEMENTATION_MODULE,
            instantiation=(
                "evaluate_baselines(corpus, slice_id=...) -> baselines"
                "[BASELINE_NAMES[1]]; the strongest single engine witness, "
                "declared per population by corpus metadata "
                "baseline_best_single_witness and fail-closed if undeclared"
            ),
            mapping_rationale=(
                "The best-single-witness policy is the best available single "
                "engine comparator: one declared engine's raw accept decisions "
                "with no diversity logic. The population, not the evaluator, "
                "declares which engine is strongest, and the declaration is part "
                "of the frozen population identity."
            ),
            masterplan_references=(
                "7B.4: fixed best-single-engine route is a baseline that must "
                "always remain runnable",
                "14C.5: router evaluation reports best fixed-rule and "
                "best-single-engine baselines",
                "23C.3 invariant 25: best single engine is a required comparator",
            ),
        ),
    ),
)
