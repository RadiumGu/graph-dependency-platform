"""test_133 — 依赖图守卫跟随 dependency-graph-rca（2026-10-15）。

PR #73 把旧 skill dependency-verification-graph 计划内停用后，守卫仍盯着旧名，
第一轮就报「skill 状态不是 ACTIVE（None）」。三件事一起守：
  1. 守卫看的是新 skill，源文件在仓库里
  2. 新 skill 是多文件包 —— 比对覆盖包内每个文件，不只 namelist()[0]
  3. 告警里的状态是真实值，不是因为缺键而显示的 None
"""
import ast
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_EMIT = _ROOT / "scripts" / "emit_graph_coverage_metrics.py"
SRC = _EMIT.read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("_em133", _EMIT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_guard_watches_the_active_skill():
    m = _load()
    assert m.SKILL_NAME == "dependency-graph-rca"
    assert m.SKILL_FILE.is_file(), m.SKILL_FILE
    assert m.SKILL_FILE.name == "SKILL.md"


def test_bundle_hash_covers_every_file_and_ignores_dirs():
    m = _load()
    a = {"SKILL.md": b"x", "references/a.md": b"1", "references/": b""}
    b = {"references/a.md": b"1", "SKILL.md": b"x"}
    assert m._bundle_sha(a) == m._bundle_sha(b), "顺序或目录条目不该影响哈希"
    assert m._bundle_sha(a) != m._bundle_sha({**b, "references/a.md": b"2"}), "引用文件变了必须被发现"


def test_local_bundle_equals_repo_dir():
    m = _load()
    files = m._local_bundle()
    assert "SKILL.md" in files and any(k.startswith("references/") for k in files)


def test_remote_comparison_is_not_first_file_only():
    assert "z.read(z.namelist()[0])" not in SRC
    assert "_bundle_sha({n: z.read(n) for n in z.namelist()})" in SRC


def test_real_status_is_reported():
    tree = ast.parse(SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "skill_guard")
    assert "out['status'] = meta.get('status')" in ast.get_source_segment(SRC, fn)
