"""
sample_for_golden_learning.py — 用 DirectBedrockLearning 采样建立 Golden baseline。

抗 SIGPIPE + 场景级 try/except + 进度写文件（retro § 6 Top 1）。

用法:
  cd /home/ubuntu/tech/graph-dependency-platform
  PYTHONPATH=rca:chaos/code python3 chaos/code/agents/sample_for_golden_learning.py
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import yaml

signal.signal(signal.SIGPIPE, signal.SIG_DFL)

_HERE = os.path.dirname(os.path.abspath(__file__))
_CHAOS_CODE = os.path.join(_HERE, "..")
_RCA = os.path.join(_HERE, "..", "..", "..", "rca")
for p in (_CHAOS_CODE, _RCA):
    ap = os.path.abspath(p)
    if ap not in sys.path:
        sys.path.insert(0, ap)

PROJ_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
FIXTURES_DIR = os.path.join(PROJ_ROOT, "experiments", "strands-poc", "fixtures")
SCENARIOS_PATH = os.path.join(PROJ_ROOT, "tests", "golden", "learning", "scenarios.yaml")
SAMPLES_DIR = os.path.join(PROJ_ROOT, "experiments", "strands-poc", "samples", "learning")
os.makedirs(SAMPLES_DIR, exist_ok=True)

# logging: file + console
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
fh = logging.FileHandler(os.path.join(SAMPLES_DIR, "run.log"))
fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logger = logging.getLogger("sample_learning")
logger.addHandler(fh)

from agents.learning_direct import DirectBedrockLearning  # noqa: E402


def load_fixture(fixture_file: str) -> list[dict]:
    path = os.path.join(FIXTURES_DIR, fixture_file)
    with open(path) as f:
        data = json.load(f)
    return data.get("experiments", [])


def main():
    with open(SCENARIOS_PATH) as f:
        scenarios = yaml.safe_load(f)

    engine = DirectBedrockLearning()
    # Resume support: load existing samples
    output_path = os.path.join(SAMPLES_DIR, "all_samples.json")
    if os.path.exists(output_path):
        with open(output_path) as f:
            all_samples = json.load(f)
        logger.info(f"Resuming: {len(all_samples)} scenarios already sampled")
    else:
        all_samples = {}
    total = len(scenarios)

    for idx, sc in enumerate(scenarios, 1):
        sid = sc["id"]
        if sid in all_samples:
            logger.info(f"[{idx}/{total}] {sid}: already sampled, skipping")
            continue
        try:
            experiments = load_fixture(sc["fixture"])
            logger.info(f"[{idx}/{total}] {sid}: {len(experiments)} experiments")

            # analyze (pure python, no cost)
            analysis = engine.analyze(experiments)

            if sc.get("expect_empty_analysis"):
                logger.info(f"✅ {sid} — empty analysis (expected)")
                all_samples[sid] = {
                    "scenario": sc,
                    "analysis_summary": {"total": 0, "gaps": 0},
                    "recommendation_samples": [],
                }
                continue

            # sample recommendations (LLM call, has cost)
            n_samples = 5 if sc.get("bucket") in ("error_input", "boundary") else 3
            samples = []
            for i in range(n_samples):
                try:
                    rec_result = engine.generate_recommendations(analysis)
                    recs = rec_result.get("recommendations", [])
                    samples.append({
                        "run": i + 1,
                        "recommendation_count": len(recs),
                        "categories": [r.get("category", "") for r in recs],
                        "token_usage": rec_result.get("token_usage"),
                        "latency_ms": rec_result.get("latency_ms"),
                        "error": rec_result.get("error"),
                    })
                    logger.info(f"  sample {i+1}/{n_samples}: {len(recs)} recs, {rec_result.get('latency_ms')}ms")
                except Exception as e:
                    logger.error(f"  sample {i+1}/{n_samples} failed: {e}")
                    samples.append({"run": i + 1, "error": str(e)})

            all_samples[sid] = {
                "scenario": sc,
                "analysis_summary": {
                    "total": analysis.get("report", None) and analysis["report"].total_experiments,
                    "gaps": len(analysis.get("gaps", [])),
                    "services": list(analysis.get("service_stats", {}).keys()),
                },
                "recommendation_samples": samples,
            }

            # Write progress + incremental save after each scenario
            progress_path = os.path.join(SAMPLES_DIR, "progress.json")
            with open(progress_path, "w") as f:
                json.dump({"completed": idx, "total": total, "last": sid}, f)
            with open(output_path, "w") as f:
                json.dump(all_samples, f, ensure_ascii=False, indent=2, default=str)

            logger.info(f"✅ {sid} done, {len(samples)} samples")

        except Exception as e:
            logger.error(f"❌ {sid} failed: {e}")
            all_samples[sid] = {"scenario": sc, "error": str(e)}
            continue

    # Write all samples
    output_path = os.path.join(SAMPLES_DIR, "all_samples.json")
    with open(output_path, "w") as f:
        json.dump(all_samples, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"🎉 All samples written to {output_path}")


if __name__ == "__main__":
    main()
