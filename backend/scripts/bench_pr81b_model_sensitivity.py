"""PR81B VLM model-sensitivity benchmark orchestrator.

Two modes:

* ``--live``: for every declared model — run the capability probe, then
  the full PR81A benchmark (all lanes plus the PR81B ablation lanes)
  as a subprocess with that model as the single-model chain, one replay
  cache and one evidence artifact per model. Transient gateway gaps heal
  by re-running the same command: the runner is cache-first (auto mode)
  and only refills missing calls.
* default (aggregate-only): read the committed per-model artifacts,
  apply the PR81B confirmation rule, and write the matrix artifact.
  Nothing touches the network.

The API key is read from ``PR81A_VLM_API_KEY`` and passed to the
subprocess environment only; it is never written to any artifact,
cache, or log line.

Usage:
  PR81A_VLM_API_KEY=<key> python scripts/bench_pr81b_model_sensitivity.py --live
  python scripts/bench_pr81b_model_sensitivity.py            # aggregate
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.eval.pr81a.corpus import load_corpus  # noqa: E402
from app.eval.pr81a.vlm import OPENROUTER_BASE_URL, VlmClient  # noqa: E402
from app.eval.pr81b.decision import evaluate_confirmation  # noqa: E402
from app.eval.pr81b.probe import run_capability_probe  # noqa: E402
from app.eval.pr81a.visual_store import PageRenderStore  # noqa: E402

MATRIX_SCHEMA = "marker.pr81b_model_sensitivity.v1"
MEASUREMENTS = BACKEND.parent / "docs" / "reference" / "measurements"
CORPUS_ROOT = BACKEND / "eval_data" / "pr81a"
PR81A_RUNNER = BACKEND / "scripts" / "bench_pr81a_visual_retrieval.py"
#: model id -> artifact/cache tag; short, filesystem-safe, stable.
#: Model ids are the user-declared exact gateway ids — never rewritten.
MODEL_TAGS: dict[str, str] = {
    "oc/mimo-v2.5-free": "mimo",
    "kr/claude-sonnet-4.5": "sonnet",
    "kr/claude-haiku-4.5": "haiku",
    "cx/gpt-5.6-luna": "gptluna",
    "free/bbl/gemini-3.0-flash": "gemflash",
}


def _model_artifact_path(tag: str) -> Path:
    return MEASUREMENTS / f"pr81b-model-{tag}.json"


def _model_cache_path(tag: str) -> Path:
    return MEASUREMENTS / f"pr81b-vlm-cache-{tag}.json"


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _retry_knobs() -> dict:
    return {
        "max_retries": int(os.environ.get("PR81A_VLM_MAX_RETRIES", "6")),
        "retry_backoff": float(os.environ.get("PR81A_VLM_RETRY_BACKOFF", "8.0")),
    }


def run_probe_for_model(model: str, base_url: str, api_key: str, cache_dir: Path) -> dict:
    corpus = load_corpus(CORPUS_ROOT)
    with tempfile.TemporaryDirectory(prefix="pr81b-probe-") as tmp:
        store = PageRenderStore(Path(tmp) / "renders")
        client = VlmClient(
            [model],
            api_key=api_key,
            base_url=base_url,
            cache_path=cache_dir / f"pr81b-probe-cache-{MODEL_TAGS[model]}.json",
            mode="auto",
            **_retry_knobs(),
        )
        result = run_capability_probe(corpus, store, client)
    result["model"] = model
    return result


def run_model_benchmark(model: str, base_url: str, api_key: str, index_root: Path, lean: bool) -> int:
    tag = MODEL_TAGS[model]
    index_dir = index_root / f"pr81b-indexes-{tag}"
    index_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PR81A_VLM_BASE_URL": base_url,
        "PR81A_VLM_MODELS": model,
        "PR81A_VLM_API_KEY": api_key,
    }
    command = [
        sys.executable,
        "-X",
        "utf8",
        str(PR81A_RUNNER),
        "--live",
        "--write",
        "--ablations",
    ]
    if lean:
        command.append("--lean-lanes")
    command += [
        "--output",
        str(_model_artifact_path(tag)),
        "--vlm-cache",
        str(_model_cache_path(tag)),
        "--index-dir",
        str(index_dir),
    ]
    completed = subprocess.run(command, cwd=BACKEND, env=env)
    return completed.returncode


def merge_probe_into_artifact(model: str, probe: dict) -> None:
    tag = MODEL_TAGS[model]
    path = _model_artifact_path(tag)
    artifact = json.loads(path.read_text(encoding="utf-8"))
    artifact["capability_probe"] = probe
    path.write_text(json.dumps(artifact, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def aggregate(models: list[str]) -> dict:
    per_model: dict[str, dict] = {}
    for model in models:
        path = _model_artifact_path(MODEL_TAGS[model])
        if not path.is_file():
            raise SystemExit(f"missing per-model artifact: {path}")
        per_model[model] = json.loads(path.read_text(encoding="utf-8"))
    confirmation = evaluate_confirmation(per_model)
    matrix = {
        "schema_version": MATRIX_SCHEMA,
        "benchmark": "PR81B VLM model-sensitivity matrix over the PR81A experiment",
        "git_sha": _git_sha(),
        "models": models,
        "confirmation": confirmation,
        "summary": {
            model: {
                "capability_probe_passed": confirmation["models"][model]["capability_probe_passed"],
                "per_model_outcome": confirmation["models"][model]["per_model_outcome"],
                "baseline_visual_hard_rate": confirmation["models"][model]["baseline_visual_hard_rate"],
                "hybrid_visual_hard_rate": confirmation["models"][model]["hybrid_visual_hard_rate"],
                "hybrid_gain_vs_baseline": confirmation["models"][model]["hybrid_gain_vs_baseline"],
                "holds": confirmation["models"][model]["holds"],
                "ablation_deltas": confirmation["models"][model]["ablation_deltas"],
            }
            for model in models
        },
    }
    return matrix


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run probes + benchmarks live")
    parser.add_argument(
        "--lean",
        action="store_true",
        help="run benchmarks with the reduced PR81B lane set (drops lexical-text, "
        "dense visual lanes, and the joint ablation; halves live VLM calls "
        "while keeping the decision rule and attribution computable)",
    )
    parser.add_argument(
        "--models",
        default=",".join(MODEL_TAGS),
        help="comma-separated model ids (default: all declared PR81B models)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PR81A_VLM_BASE_URL", OPENROUTER_BASE_URL),
    )
    parser.add_argument(
        "--probe-cache-dir",
        type=Path,
        default=BACKEND.parent / "lab" / "pr81b-probe-caches",
        help="scratch location for probe replay caches (not committed evidence)",
    )
    parser.add_argument(
        "--index-root",
        type=Path,
        default=BACKEND.parent / "lab" / "pr81b-indexes",
        help="scratch location for live npz generation (committed PR81A npz files are never overwritten)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=MEASUREMENTS / "pr81b-model-sensitivity.json",
    )
    parser.add_argument(
        "--probe-output",
        type=Path,
        default=MEASUREMENTS / "pr81b-capability-probe.json",
    )
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    unknown = [m for m in models if m not in MODEL_TAGS]
    if unknown:
        raise SystemExit(f"no declared tag for model(s): {unknown}")

    if args.live:
        api_key = os.environ.get("PR81A_VLM_API_KEY")
        if not api_key:
            raise SystemExit("--live requires PR81A_VLM_API_KEY in the environment")
        args.probe_cache_dir.mkdir(parents=True, exist_ok=True)
        probe_table: dict[str, dict] = {}
        failures: dict[str, str] = {}
        for model in models:
            tag = MODEL_TAGS[model]
            print(f"[pr81b] capability probe: {model}", flush=True)
            probe = run_probe_for_model(model, args.base_url, api_key, args.probe_cache_dir)
            probe_table[model] = probe
            print(
                f"[pr81b] probe {tag}: {probe['correct']}/{probe['total']} correct, "
                f"passed={probe['passed']}",
                flush=True,
            )
            if not probe["passed"]:
                failures[model] = "capability probe failed"
                continue
            print(f"[pr81b] benchmark: {model}", flush=True)
            code = run_model_benchmark(model, args.base_url, api_key, args.index_root, lean=args.lean)
            if code != 0:
                failures[model] = f"benchmark exit {code}"
                continue
            merge_probe_into_artifact(model, probe)
        args.probe_output.parent.mkdir(parents=True, exist_ok=True)
        args.probe_output.write_text(
            json.dumps({"models": probe_table}, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.probe_output}")
        if failures:
            print(f"[pr81b] models not completed: {failures}")
        completed = [m for m in models if m not in failures]
        if not completed:
            return 1
    else:
        completed = models

    matrix = aggregate(completed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(matrix, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.output}")
    print(
        json.dumps(
            {
                "outcome": matrix["confirmation"]["outcome"],
                "attribution": matrix["confirmation"]["attribution"],
                "holders": matrix["confirmation"]["holders"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
