"""
test_11_schema_consistency.py — Sprint 0 Schema 一致性检查

Tests: S0-01 ~ S0-07

S0-01/02/03: 需要真实 Neptune 连接，标记 @pytest.mark.neptune
S0-04/05/06/07: 离线静态检查，无标记
"""
import importlib
import importlib.util
import os
import re
import subprocess
import sys
import types

import pytest

PROJECT_ROOT = "/home/ubuntu/tech/graph-dependency-platform"
ETL_DIR = os.path.join(PROJECT_ROOT, "infra", "lambda", "etl_aws")
INFRA_DIR = os.path.join(PROJECT_ROOT, "infra")
RCA_DIR = os.path.join(PROJECT_ROOT, "rca")

# ── Schema parse helpers ──────────────────────────────────────────────────────


def _load_schema_module():
    """Load rca/neptune/schema_prompt.py as a module."""
    if RCA_DIR not in sys.path:
        sys.path.insert(0, RCA_DIR)
    from neptune import schema_prompt  # noqa: F401
    return schema_prompt


def _schema_node_labels() -> set:
    """Parse node labels from GRAPH_SCHEMA (lines: '- LabelName: ...')."""
    mod = _load_schema_module()
    # Only the section before "## 边类型" contains node definitions
    node_section = mod.GRAPH_SCHEMA.split("## 边类型")[0]
    # Match "- PascalCaseLabel:" — allow digits (e.g. EC2Instance, S3Bucket)
    labels = set(re.findall(r"^- ([A-Z][A-Za-z0-9]+):", node_section, re.MULTILINE))
    return labels


def _schema_edge_types() -> set:
    """Parse edge types from GRAPH_SCHEMA ('[: EdgeType ]' patterns).

    Handles both single edges [:EdgeType] and compound [:A|:B] forms.
    The bracket content may contain colons (as prefix on each part after '|').
    """
    mod = _load_schema_module()
    # Capture full bracket content including colons and pipes: [: ... ]
    raw = re.findall(r"\[:([\w|:]+)\]", mod.GRAPH_SCHEMA)
    edges: set = set()
    for item in raw:
        for part in item.split("|"):
            part = part.lstrip(":")
            if part:
                edges.add(part)
    return edges


# ── S0-01: Node label consistency ─────────────────────────────────────────────


@pytest.mark.neptune
def test_s0_01_node_labels_match_schema(neptune_rca):
    """S0-01: schema_prompt.py 静态节点标签 vs Neptune 实际标签完全匹配。

    Intent: detect label drift between ETL writes and schema documentation.
    Failure modes:
      - Missing in Neptune → schema defines a label that was never written.
      - Extra in Neptune   → Neptune has labels not documented in schema.
    """
    schema_labels = _schema_node_labels()

    rows = neptune_rca.results(
        "MATCH (n) RETURN DISTINCT labels(n) AS label LIMIT 500"
    )
    actual_labels: set = set()
    for row in rows:
        val = row.get("label", [])
        if isinstance(val, list):
            actual_labels.update(val)
        elif isinstance(val, str):
            actual_labels.add(val)

    missing_in_neptune = schema_labels - actual_labels
    extra_in_neptune = actual_labels - schema_labels

    print(f"\nSchema labels  ({len(schema_labels)}): {sorted(schema_labels)}")
    print(f"Neptune labels ({len(actual_labels)}): {sorted(actual_labels)}")
    if missing_in_neptune:
        print(f"[MISSING] In schema, absent in Neptune: {sorted(missing_in_neptune)}")
    if extra_in_neptune:
        print(f"[EXTRA]   In Neptune, absent in schema: {sorted(extra_in_neptune)}")

    assert not missing_in_neptune, (
        f"Labels defined in schema_prompt.py but not present in Neptune: "
        f"{sorted(missing_in_neptune)}"
    )
    assert not extra_in_neptune, (
        f"Labels present in Neptune but not defined in schema_prompt.py: "
        f"{sorted(extra_in_neptune)}"
    )


# ── S0-02: Edge type consistency ──────────────────────────────────────────────


@pytest.mark.neptune
def test_s0_02_edge_types_match_schema(neptune_rca):
    """S0-02: schema_prompt.py 静态边类型 vs Neptune 实际边类型完全匹配。

    Intent: detect relationship type drift between ETL writes and documentation.
    """
    schema_edges = _schema_edge_types()

    rows = neptune_rca.results(
        "MATCH ()-[r]->() RETURN DISTINCT type(r) AS rel_type LIMIT 500"
    )
    actual_edges = {row["rel_type"] for row in rows if row.get("rel_type")}

    missing_in_neptune = schema_edges - actual_edges
    extra_in_neptune = actual_edges - schema_edges

    print(f"\nSchema edges  ({len(schema_edges)}): {sorted(schema_edges)}")
    print(f"Neptune edges ({len(actual_edges)}): {sorted(actual_edges)}")
    if missing_in_neptune:
        print(f"[MISSING] In schema, absent in Neptune: {sorted(missing_in_neptune)}")
    if extra_in_neptune:
        print(f"[EXTRA]   In Neptune, absent in schema: {sorted(extra_in_neptune)}")

    assert not missing_in_neptune, (
        f"Edge types defined in schema_prompt.py but not present in Neptune: "
        f"{sorted(missing_in_neptune)}"
    )
    assert not extra_in_neptune, (
        f"Edge types present in Neptune but not defined in schema_prompt.py: "
        f"{sorted(extra_in_neptune)}"
    )


# ── S0-03: FEW_SHOT_EXAMPLES executability ───────────────────────────────────


@pytest.mark.neptune
def test_s0_03_few_shot_examples_executable(neptune_rca):
    """S0-03: FEW_SHOT_EXAMPLES 中所有 openCypher 示例可执行且返回非空。

    Intent: catch broken example queries — syntax errors or references to
    non-existent labels/properties indicate schema drift in the NL-query layer.

    Execution errors → hard failure (schema/syntax problem).
    Empty results    → warning only (may be a data-availability issue).
    """
    mod = _load_schema_module()
    examples = mod.FEW_SHOT_EXAMPLES

    # Provide default params for parameterised examples
    default_params = {"since": "2026-01-01"}

    execution_errors: list = []
    empty_results: list = []

    for ex in examples:
        question = ex["q"]
        cypher = ex["cypher"]
        try:
            rows = neptune_rca.results(cypher, default_params)
            if not rows:
                empty_results.append(question)
        except Exception as exc:
            execution_errors.append(
                {
                    "question": question,
                    "cypher": cypher,
                    "error": str(exc),
                }
            )

    # Warn about empty results (data gap, not a schema error)
    if empty_results:
        print(f"\n⚠️  Queries with empty results ({len(empty_results)}):")
        for q in empty_results:
            print(f"   - {q}")

    # Hard-fail on execution errors (syntax / schema mismatch)
    assert not execution_errors, (
        f"{len(execution_errors)} few-shot example(s) failed to execute:\n"
        + "\n".join(
            f"  Q: {e['question']}\n"
            f"  Cypher: {e['cypher']}\n"
            f"  Error: {e['error']}"
            for e in execution_errors
        )
    )


# ── S0-04: ETL node label static scan ────────────────────────────────────────


def test_s0_04_etl_node_labels_in_schema():
    """S0-04: ETL 代码中使用的节点标签与 schema_prompt.py 一致（静态扫描）。

    Intent: ensure every label the ETL writes to Neptune is documented in the
    schema prompt, so the NL-query layer knows about it.

    Scan strategy:
      1. upsert_vertex('Label', ...)  — direct string literal
      2. vertex_label = 'Label'       — variable assigned before upsert_vertex
      3. hasLabel('Label')            — Gremlin string queries embedded in ETL
    """
    schema_labels = _schema_node_labels()

    etl_files: list = []
    for root, _dirs, files in os.walk(ETL_DIR):
        for fn in files:
            if fn.endswith(".py"):
                etl_files.append(os.path.join(root, fn))

    found_labels: set = set()
    # Pattern 1: upsert_vertex('LabelName', ...) — allow digits (EC2Instance, S3Bucket)
    p_upsert = re.compile(r"""upsert_vertex\s*\(\s*['"]([A-Z][A-Za-z0-9]+)['"]""")
    # Pattern 2: vertex_label = 'LabelName'  (conditional ternary assignments)
    p_var = re.compile(r"""[a-zA-Z_]*[Ll]abel\s*=\s*['"]([A-Z][A-Za-z0-9]+)['"]""")
    # NOTE: NOT scanning hasLabel() — it applies to both vertices and edges in
    # Gremlin strings, producing false positives (e.g. g.E().hasLabel('Serves')).

    for fpath in etl_files:
        with open(fpath) as fh:
            content = fh.read()
        for pat in (p_upsert, p_var):
            for m in pat.finditer(content):
                found_labels.add(m.group(1))

    etl_labels = found_labels

    unknown_labels = etl_labels - schema_labels

    print(f"\nETL node labels ({len(etl_labels)}): {sorted(etl_labels)}")
    print(f"Schema labels   ({len(schema_labels)}): {sorted(schema_labels)}")
    if unknown_labels:
        print(f"[UNKNOWN] In ETL, not in schema: {sorted(unknown_labels)}")

    assert not unknown_labels, (
        f"ETL code uses node labels not defined in schema_prompt.py: "
        f"{sorted(unknown_labels)}"
    )


# ── S0-05: ETL edge type static scan ─────────────────────────────────────────


def test_s0_05_etl_edge_types_in_schema():
    """S0-05: ETL 代码中使用的边类型与 schema_prompt.py 一致（静态扫描）。

    Scan strategy:
      1. upsert_edge(..., ..., 'EdgeType', ...)  — third positional arg
      2. addE('EdgeType')                        — Gremlin string queries
      3. OPS_TOOL_EDGES tuples in business_config.json
    """
    schema_edges = _schema_edge_types()

    etl_files: list = []
    for root, _dirs, files in os.walk(ETL_DIR):
        for fn in files:
            if fn.endswith(".py"):
                etl_files.append(os.path.join(root, fn))

    found_edges: set = set()
    # Pattern 1: upsert_edge(vid1, vid2, 'EdgeType', ...) — third arg
    p_upsert = re.compile(
        r"""upsert_edge\s*\([^,]+,[^,]+,\s*['"]([A-Za-z]+)['"]"""
    )
    # Pattern 2: addE('EdgeType') in Gremlin embedded strings
    p_adde = re.compile(r"""addE\s*\(\s*['"]([A-Za-z]+)['"]""")
    # Pattern 3: hasLabel('EdgeType') — actually an edge-check pattern inE/outE
    p_ine = re.compile(r"""(?:inE|outE)\s*\(\s*['"]([A-Za-z]+)['"]""")

    for fpath in etl_files:
        with open(fpath) as fh:
            content = fh.read()
        for pat in (p_upsert, p_adde, p_ine):
            for m in pat.finditer(content):
                found_edges.add(m.group(1))

    # Also read OPS_TOOL_EDGES from business_config.json (index 2 is edge label)
    import json

    bc_path = os.path.join(ETL_DIR, "business_config.json")
    if os.path.exists(bc_path):
        with open(bc_path) as fh:
            bc = json.load(fh)
        for edge_tuple in bc.get("ops_tool_edges", []):
            if len(edge_tuple) >= 3:
                found_edges.add(edge_tuple[2])
        for _svc, deps in bc.get("microservice_infra_deps", {}).items():
            for dep in deps:
                if isinstance(dep, dict) and dep.get("edge"):
                    found_edges.add(dep["edge"])

    # Filter to valid edge label format (CamelCase or PascalCase)
    found_edges = {e for e in found_edges if e and e[0].isupper()}

    unknown_edges = found_edges - schema_edges

    print(f"\nETL edge types ({len(found_edges)}): {sorted(found_edges)}")
    print(f"Schema edges   ({len(schema_edges)}): {sorted(schema_edges)}")
    if unknown_edges:
        print(f"[UNKNOWN] In ETL, not in schema: {sorted(unknown_edges)}")

    assert not unknown_edges, (
        f"ETL code uses edge types not defined in schema_prompt.py: "
        f"{sorted(unknown_edges)}"
    )


# ── S0-06: CDK synth ──────────────────────────────────────────────────────────


def test_s0_06_cdk_synth():
    """S0-06: CDK synth 成功（退出码 0）。

    Intent: confirm the IaC stack definition compiles cleanly; catches
    TypeScript type errors and missing construct props before deployment.
    """
    result = subprocess.run(
        ["cdk", "synth", "--quiet"],
        cwd=INFRA_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.stdout:
        print(f"\nCDK synth stdout (first 500 chars): {result.stdout[:500]}")
    if result.returncode != 0:
        print(f"CDK synth stderr:\n{result.stderr[:2000]}")

    assert result.returncode == 0, (
        f"cdk synth exited with code {result.returncode}.\n"
        f"stderr: {result.stderr[:2000]}"
    )


# ── S0-07: Import cycle check ─────────────────────────────────────────────────


def test_s0_07_no_circular_imports():
    """S0-07: 主要模块无循环依赖（import 不抛 ImportError）。

    Intent: guarantee the key modules can be loaded in isolation without
    triggering circular-import errors.  Non-import runtime errors (e.g. missing
    env vars, missing AWS creds) are acceptable and treated as warnings.
    """
    modules_to_check = [
        (
            "rca.neptune.schema_prompt",
            os.path.join(RCA_DIR, "neptune", "schema_prompt.py"),
        ),
        (
            "rca.neptune.neptune_queries",
            os.path.join(RCA_DIR, "neptune", "neptune_queries.py"),
        ),
        (
            "rca.neptune.query_guard",
            os.path.join(RCA_DIR, "neptune", "query_guard.py"),
        ),
        (
            "rca.neptune.nl_query",
            os.path.join(RCA_DIR, "neptune", "nl_query.py"),
        ),
        (
            "rca.core.rca_engine",
            os.path.join(RCA_DIR, "core", "rca_engine.py"),
        ),
        (
            "rca.core.graph_rag_reporter",
            os.path.join(RCA_DIR, "core", "graph_rag_reporter.py"),
        ),
        (
            "rca.actions.incident_writer",
            os.path.join(RCA_DIR, "actions", "incident_writer.py"),
        ),
        (
            "rca.search.incident_vectordb",
            os.path.join(RCA_DIR, "search", "incident_vectordb.py"),
        ),
        (
            "chaos.neptune_sync",
            os.path.join(PROJECT_ROOT, "chaos", "code", "neptune_sync.py"),
        ),
    ]

    import_errors: list = []
    skipped: list = []
    warned: list = []

    for mod_name, mod_path in modules_to_check:
        if not os.path.exists(mod_path):
            skipped.append(mod_name)
            print(f"  SKIP {mod_name}: file not found")
            continue
        try:
            spec = importlib.util.spec_from_file_location(mod_name, mod_path)
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            print(f"  OK   {mod_name}")
        except ImportError as exc:
            import_errors.append((mod_name, f"ImportError: {exc}"))
            print(f"  FAIL {mod_name}: ImportError: {exc}")
        except Exception as exc:
            # Runtime errors during module load are acceptable (missing creds, etc.)
            warned.append((mod_name, f"{type(exc).__name__}: {exc}"))
            print(f"  WARN {mod_name}: {type(exc).__name__}: {exc}")

    if skipped:
        print(f"\nSkipped (not yet implemented): {skipped}")
    if warned:
        print(f"\nNon-import warnings (acceptable):")
        for name, msg in warned:
            print(f"  {name}: {msg}")

    assert not import_errors, (
        "Circular or broken imports detected:\n"
        + "\n".join(f"  {name}: {err}" for name, err in import_errors)
    )
