"""
Strands Agents L0 Spike — Smart Query 等价实现（不动生产代码）。

验证硬约束：
  1. BedrockModel 能否接受 global.anthropic.claude-sonnet-4-6
  2. BedrockModel 能否 region=ap-northeast-1
  3. @tool 能否调 neptune_client.results(cypher)
  4. execute_cypher 内部能否强制 query_guard.is_safe()
  5. Agent 响应能否拿到 tool-call trace
  6. 延迟基准

运行: source .venv/bin/activate && python spike.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import traceback
from pathlib import Path

import yaml

# --- 复用生产代码（sys.path，不拷贝） ---
ROOT = Path(__file__).resolve().parents[2]  # graph-dependency-platform/
sys.path.insert(0, str(ROOT / "rca"))
sys.path.insert(0, str(ROOT))  # profiles/

# profile 默认值
os.environ.setdefault("PROFILE_NAME", "petsite")

from neptune import neptune_client as nc  # noqa: E402
from neptune import query_guard  # noqa: E402
from neptune.schema_prompt import build_system_prompt  # noqa: E402
from profiles.profile_loader import EnvironmentProfile  # noqa: E402

# --- Strands ---
try:
    from strands import Agent, tool
    from strands.models import BedrockModel
    STRANDS_OK = True
    STRANDS_IMPORT_ERR = None
except Exception as e:  # pragma: no cover
    STRANDS_OK = False
    STRANDS_IMPORT_ERR = repr(e)


# =========================================================
# Tools — execute_cypher 内部强制 query_guard.is_safe()
# =========================================================

_LAST_CALLS: list[dict] = []


@tool
def get_schema_section(section: str = "all") -> str:
    """返回 Neptune 图 schema 的指定段。

    Args:
        section: "all" | "nodes" | "edges"
    """
    profile = EnvironmentProfile()
    text = profile.neptune_graph_schema_text
    _LAST_CALLS.append({"tool": "get_schema_section", "section": section, "chars": len(text)})
    return text[:6000]


@tool
def validate_cypher(cypher: str) -> str:
    """校验 openCypher 查询是否安全（只读、跳数限制），返回 'OK' 或原因。"""
    safe, reason = query_guard.is_safe(cypher)
    _LAST_CALLS.append({"tool": "validate_cypher", "safe": safe, "reason": reason})
    return "OK" if safe else f"UNSAFE: {reason}"


@tool
def execute_cypher(cypher: str) -> str:
    """执行只读 openCypher 查询并返回 JSON 结果（最多 LIMIT 200）。

    内部强制 query_guard.is_safe() 拦截写操作。
    """
    safe, reason = query_guard.is_safe(cypher)
    if not safe:
        _LAST_CALLS.append({"tool": "execute_cypher", "blocked": True, "reason": reason})
        return f"ERROR: guard blocked unsafe cypher — {reason}"
    cypher2 = query_guard.ensure_limit(cypher)
    try:
        rows = nc.results(cypher2)
    except Exception as e:
        _LAST_CALLS.append({"tool": "execute_cypher", "error": repr(e)[:200]})
        return f"ERROR: execution failed — {e!r}"
    _LAST_CALLS.append({"tool": "execute_cypher", "cypher": cypher2[:200], "rows": len(rows)})
    return json.dumps(rows, ensure_ascii=False, default=str)[:4000]


# =========================================================
# Agent 构造 + 模型候选
# =========================================================

MODEL_CANDIDATES = [
    "global.anthropic.claude-sonnet-4-6",     # 主目标：Global inference profile
    "apac.anthropic.claude-sonnet-4-5",       # 降级 1：APAC profile
    "us.anthropic.claude-sonnet-4-5",         # 降级 2：US profile
    "anthropic.claude-3-5-sonnet-20241022-v2:0",  # 降级 3：区域模型
]

REGION = "ap-northeast-1"


def build_agent(model_id: str) -> Agent:
    profile = EnvironmentProfile()
    sys_prompt = build_system_prompt(profile) + (
        "\n\n## Agent 调用规则\n"
        "1. 先思考问题涉及哪些节点/关系。如不确定，调用 get_schema_section。\n"
        "2. 生成 Cypher 后调用 validate_cypher。\n"
        "3. 校验通过后调用 execute_cypher 取结果。\n"
        "4. 如果结果为空且你怀疑关系名写错了，换一个常见关系名重试最多 1 次。\n"
        "5. 最后用 2-4 句中文总结结果，直接给结论。"
    )
    model = BedrockModel(model_id=model_id, region_name=REGION)
    return Agent(
        model=model,
        tools=[get_schema_section, validate_cypher, execute_cypher],
        system_prompt=sys_prompt,
    )


def probe_model_support() -> dict:
    """逐个探测候选 model_id 在 ap-northeast-1 的可用性。"""
    results = {}
    for mid in MODEL_CANDIDATES:
        try:
            agent = build_agent(mid)
            t0 = time.time()
            resp = agent("ping (仅回复 pong)")
            dt = time.time() - t0
            text = getattr(resp, "message", None) or str(resp)
            results[mid] = {"ok": True, "latency_s": round(dt, 2), "preview": str(text)[:200]}
        except Exception as e:
            results[mid] = {"ok": False, "error": repr(e)[:400]}
    return results


# =========================================================
# 主流程
# =========================================================

def load_goldens() -> list[str]:
    p = Path(__file__).parent / "golden_questions.yaml"
    if not p.exists():
        return [
            "petsite 依赖哪些数据库？",
            "Tier0 服务有哪些？",
            "petsite 的完整上下游依赖路径",
        ]
    with p.open() as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict) and "questions" in data:
        return [q if isinstance(q, str) else q.get("q", "") for q in data["questions"]]
    return list(data) if isinstance(data, list) else []


def run_question(agent: Agent, q: str) -> dict:
    global _LAST_CALLS
    _LAST_CALLS = []
    t0 = time.time()
    err = None
    try:
        resp = agent(q)
        text = getattr(resp, "message", None) or str(resp)
    except Exception as e:
        err = f"{type(e).__name__}: {e!r}"
        text = ""
    dt = time.time() - t0
    return {
        "question": q,
        "latency_s": round(dt, 2),
        "error": err,
        "tool_calls": list(_LAST_CALLS),
        "n_tool_calls": len(_LAST_CALLS),
        "summary_preview": str(text)[:500],
    }


def main():
    out: dict = {"strands_import_ok": STRANDS_OK, "strands_import_err": STRANDS_IMPORT_ERR}
    if not STRANDS_OK:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print("[1/3] probing model candidates in", REGION)
    probe = probe_model_support()
    out["model_probe"] = probe
    print(json.dumps(probe, ensure_ascii=False, indent=2))

    chosen = next((m for m, r in probe.items() if r.get("ok")), None)
    out["chosen_model"] = chosen
    if not chosen:
        print("NO_MODEL_AVAILABLE")
        Path(__file__).parent.joinpath("spike_result.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2)
        )
        return

    print(f"[2/3] using model: {chosen}")
    agent = build_agent(chosen)

    print("[3/3] running goldens")
    runs = []
    for q in load_goldens():
        print(" -", q)
        try:
            r = run_question(agent, q)
        except Exception as e:
            r = {"question": q, "error": repr(e), "traceback": traceback.format_exc()[:1000]}
        runs.append(r)
        print("   latency", r.get("latency_s"), "tool_calls", r.get("n_tool_calls"))

    latencies = [r["latency_s"] for r in runs if r.get("latency_s")]
    out["runs"] = runs
    out["p50_latency_s"] = round(statistics.median(latencies), 2) if latencies else None

    # 验证 guard 拦截（硬约束 #4）
    print("[bonus] guard-bypass attempt — DELETE test")
    _LAST_CALLS.clear()
    blocked = execute_cypher("MATCH (n) DETACH DELETE n")  # type: ignore
    out["guard_block_test"] = {"response": blocked, "intercepted": "UNSAFE" in blocked or "blocked" in blocked}

    Path(__file__).parent.joinpath("spike_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str)
    )
    print("\nwrote spike_result.json")


if __name__ == "__main__":
    main()
