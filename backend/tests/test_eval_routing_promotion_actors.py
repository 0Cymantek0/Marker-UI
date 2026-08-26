"""Semantic actor-registry and offline-containment tests (invariant 25)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.eval.routing_promotion.actors import (
    ACTOR_IMPLEMENTATION_MODULE,
    ACTOR_REGISTRY_V1,
    ACTOR_SCHEMA_VERSION,
    ActorDeclaration,
    ActorRegistry,
    ROLE_BEST_SINGLE,
    ROLE_CANDIDATE,
    ROLE_FIXED_RULES,
)
from app.eval.verification_risk.baselines import BASELINE_NAMES
from app.eval.verification_risk.common import VerificationRiskError

BACKEND_APP = Path(__file__).resolve().parent.parent / "app"


class TestActorRegistry:
    def test_three_roles_bound_exactly_once_to_executable_policies(self):
        registry = ACTOR_REGISTRY_V1
        registry.validate()
        roles = {actor.role for actor in registry.actors}
        assert roles == {ROLE_CANDIDATE, ROLE_FIXED_RULES, ROLE_BEST_SINGLE}
        for actor in registry.actors:
            assert actor.policy_id in BASELINE_NAMES

    def test_candidate_is_the_evc_continuation_policy(self):
        candidate = ACTOR_REGISTRY_V1.actor_for(ROLE_CANDIDATE)
        assert candidate.policy_id == "dependency_aware_policy"

    def test_comparators_are_fixed_rules_and_best_single(self):
        fixed = ACTOR_REGISTRY_V1.actor_for(ROLE_FIXED_RULES)
        best = ACTOR_REGISTRY_V1.actor_for(ROLE_BEST_SINGLE)
        assert fixed.policy_id == "deterministic_source_native_only"
        assert best.policy_id == "best_single_witness"

    def test_every_mapping_is_justified_by_governing_masterplan_sections(self):
        for actor in ACTOR_REGISTRY_V1.actors:
            assert len(actor.masterplan_references) >= 2
            assert "23C.3 invariant 25" in " ".join(actor.masterplan_references)

    def test_identity_is_stable(self):
        registry = ACTOR_REGISTRY_V1
        first = registry.semantic_identity
        assert first.startswith("sha256:")
        assert registry.semantic_identity == first

    def test_rationale_edit_changes_identity(self):
        actor = ACTOR_REGISTRY_V1.actor_for(ROLE_CANDIDATE)
        tampered = replace(actor, mapping_rationale=actor.mapping_rationale + "x")
        assert tampered.semantic_identity != actor.semantic_identity


class TestActorValidation:
    def _registry_with(self, actor: ActorDeclaration) -> ActorRegistry:
        actors = tuple(
            actor if item.role == actor.role else item for item in ACTOR_REGISTRY_V1.actors
        )
        return ActorRegistry(schema_version=ACTOR_SCHEMA_VERSION, actors=actors)

    def test_unknown_policy_id_fails_closed(self):
        actor = ACTOR_REGISTRY_V1.actor_for(ROLE_CANDIDATE)
        broken = replace(actor, policy_id="hypothetical_learned_router")
        with pytest.raises(VerificationRiskError, match="not an executable baseline"):
            self._registry_with(broken).validate()

    def test_missing_role_fails_closed(self):
        registry = ActorRegistry(
            schema_version=ACTOR_SCHEMA_VERSION,
            actors=tuple(
                actor for actor in ACTOR_REGISTRY_V1.actors if actor.role != ROLE_BEST_SINGLE
            ),
        )
        with pytest.raises(VerificationRiskError, match="exactly 3 actors"):
            registry.validate()

    def test_role_without_rationale_fails_closed(self):
        actor = ACTOR_REGISTRY_V1.actor_for(ROLE_FIXED_RULES)
        broken = replace(actor, mapping_rationale=" ")
        with pytest.raises(VerificationRiskError, match="mapping rationale"):
            self._registry_with(broken).validate()

    def test_actor_for_unknown_role_raises(self):
        with pytest.raises(VerificationRiskError, match="no actor declared"):
            ACTOR_REGISTRY_V1.actor_for("oracle")


class TestOfflineContainment:
    def test_candidate_policy_has_no_production_serving_import(self):
        """Invariant 25 containment: the EVC-style candidate stays offline.

        The dependency-aware acceptance policy may be imported only by the
        offline evaluation package, its tests, and benchmark scripts.  Any
        serving/kernel module importing it would move routing authority out
        of shadow without passing this gate.
        """

        app_root = BACKEND_APP.resolve()
        offenders: list[str] = []
        for path in sorted(app_root.rglob("*.py")):
            rel = path.relative_to(app_root).as_posix()
            if rel.startswith("eval/"):
                continue
            source = path.read_text(encoding="utf-8")
            if "app.eval.verification_risk" in source or "dependency_aware" in source:
                offenders.append(rel)
        assert offenders == [], f"candidate policy escaped shadow scope: {offenders}"

    def test_declared_implementation_module_is_offline(self):
        assert ACTOR_IMPLEMENTATION_MODULE.startswith("app.eval.")
