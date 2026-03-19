"""
tests/test_rca.py - Unit tests for pure-function logic in rca_engine

Covered:
  - rca_engine.step4_score   (confidence scoring)
  - fault_classifier.classify (severity grading)
  - playbook_engine.match     (playbook selection)

Run with:
  python -m pytest tests/ -v
"""
import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Bootstrap: make "rca_engine" importable as a package from the repo root
# without needing to install it.
# ---------------------------------------------------------------------------
# Add the PARENT of rca_engine/ to sys.path so that "import rca_engine"
# resolves to the rca_engine/ *package* (via __init__.py), not rca_engine.py the file.
#
# Run from parent dir:  cd /home/ubuntu/tech && python3 -m unittest rca_engine.tests.test_rca -v
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # rca_engine/
PARENT_OF_REPO = os.path.dirname(REPO_ROOT)  # parent containing rca_engine/
if PARENT_OF_REPO not in sys.path:
    sys.path.insert(0, PARENT_OF_REPO)
# Also add rca_engine/ itself so absolute imports (from config import X) work
# like they do in Lambda flat packaging
if REPO_ROOT not in sys.path:
    sys.path.append(REPO_ROOT)
# Remove rca_engine/ from front of sys.path to avoid rca_engine.py shadowing the package
while REPO_ROOT in sys.path[:3]:
    sys.path.remove(REPO_ROOT)
    sys.path.append(REPO_ROOT)  # keep it at the end

# ---------------------------------------------------------------------------
# Stub heavy dependencies so tests can run without AWS credentials or
# installed packages like `boto3`, `kubernetes`, etc.
# ---------------------------------------------------------------------------

def _make_stub(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod

for _pkg in ['boto3', 'botocore', 'botocore.auth', 'botocore.awsrequest',
             'botocore.credentials', 'botocore.signers', 'botocore.model',
             'kubernetes', 'kubernetes.client', 'kubernetes.config', 'requests']:
    if _pkg not in sys.modules:
        _make_stub(_pkg)

# Pre-stub rca_engine sub-modules BEFORE importing the package.
# The package directory IS rca_engine/, so we need careful import ordering.
# rca_engine/rca_engine.py does "from .config import CANONICAL" at module level.

# Step 1: Ensure rca_engine is recognized as a package (not the .py file)
import importlib

# Step 2: Stub neptune_client, neptune_queries, eks_auth at module level
# before any rca_engine submodule tries to import them.
# Stub at BOTH rca_engine.X (package import) and X (absolute import) levels
# because Lambda uses absolute imports but tests run as package.
_nc_stub = _make_stub('rca_engine.neptune_client')
_nc_stub.results = MagicMock(return_value=[])
_nc_stub.query = MagicMock(return_value={'results': []})
sys.modules['neptune_client'] = _nc_stub

_nq_stub = _make_stub('rca_engine.neptune_queries')
_nq_stub.q1_blast_radius = MagicMock(return_value={'capabilities': [], 'services': []})
_nq_stub.q4_service_info = MagicMock(return_value={'priority': 'Tier1', 'tier': 1})
_nq_stub.q5_similar_incidents = MagicMock(return_value=[])
_nq_stub.q8_log_source = MagicMock(return_value='')
sys.modules['neptune_queries'] = _nq_stub

_eks_stub = _make_stub('rca_engine.eks_auth')
_eks_stub.get_eks_token = MagicMock(return_value='fake-token')
_eks_stub.get_k8s_endpoint = MagicMock(return_value=('https://fake-endpoint', 'fake-token'))
_eks_stub.write_ca = MagicMock(return_value='/tmp/fake-ca.crt')
sys.modules['eks_auth'] = _eks_stub

# Also stub config at top-level so absolute "from config import" works
# (config.py is a real file, just ensure it's importable both ways)

# Step 3: Now import rca_engine submodules we actually need for testing
from rca_engine.config import CANONICAL  # noqa: E402
from rca_engine import rca_engine as rca_mod  # noqa: E402
from rca_engine import fault_classifier  # noqa: E402
from rca_engine import playbook_engine  # noqa: E402


# ===========================================================================
# Tests for rca_engine.step4_score
# ===========================================================================

class TestStep4Score(unittest.TestCase):

    def setUp(self):
        # Import lazily so stubs are already in place
        from rca_engine import rca_engine as re_mod
        self.step4_score = re_mod.step4_score

    def _error_service(self, name, first_error='2026-01-01T00:00:00', count=10, rate=60.0):
        return {'service': name, 'first_error': first_error,
                'error_count': count, 'error_rate_pct': rate}

    def test_earliest_service_gets_40_points(self):
        error_svcs = [
            self._error_service('svc-a', '2026-01-01T00:00:00'),
            self._error_service('svc-b', '2026-01-01T00:05:00'),
        ]
        results = self.step4_score(error_svcs, [], [], 'svc-a')
        scores = {r['service']: r['score'] for r in results}
        self.assertGreaterEqual(scores['svc-a'], 40)
        self.assertLess(scores['svc-b'], scores['svc-a'])

    def test_no_upstream_error_adds_20_points(self):
        error_svcs = [self._error_service('svc-a')]
        graph = [{'service': 'svc-a', 'has_upstream_error': False, 'upstream_services': []}]
        results = self.step4_score(error_svcs, [], graph, 'svc-a')
        self.assertEqual(results[0]['service'], 'svc-a')
        self.assertIn(40 + 20, [results[0]['score']])  # earliest + no upstream

    def test_cloudtrail_change_adds_30_points(self):
        error_svcs = [self._error_service('svc-a')]
        changes = [{'event': 'UpdateFunctionCode', 'resource': 'svc-a', 'time': '2026-01-01T00:00'}]
        results = self.step4_score(error_svcs, changes, [], 'svc-a')
        self.assertGreaterEqual(results[0]['score'], 40 + 30)

    def test_empty_error_services_returns_fallback(self):
        results = self.step4_score([], [], [], 'fallback-svc')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['service'], 'fallback-svc')
        self.assertAlmostEqual(results[0]['confidence'], 0.3)

    def test_results_sorted_by_score_descending(self):
        error_svcs = [
            self._error_service('svc-b', '2026-01-01T00:05:00'),
            self._error_service('svc-a', '2026-01-01T00:00:00'),
        ]
        results = self.step4_score(error_svcs, [], [], 'svc-a')
        scores = [r['score'] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))


# ===========================================================================
# Tests for fault_classifier.classify
# ===========================================================================

class TestFaultClassifier(unittest.TestCase):

    def setUp(self):
        from rca_engine import fault_classifier
        self.classify = fault_classifier.classify

    def _signal(self, value=0.5, threshold=0.05):
        return {'source': 'manual', 'metric': 'error_rate',
                'value': value, 'threshold': threshold}

    def test_tier0_service_multiple_caps_is_p0(self):
        _nq_stub.q4_service_info.return_value = {'priority': 'Tier0', 'tier': 0}
        _nq_stub.q1_blast_radius.return_value = {
            'capabilities': [
                {'name': 'cap1', 'priority': 'Tier0'},
                {'name': 'cap2', 'priority': 'Tier0'},
            ],
            'services': [],
        }
        result = self.classify('petsite', self._signal())
        self.assertEqual(result['severity'], 'P0')
        self.assertEqual(result['strategy'], 'Diagnose-First')

    def test_tier0_service_single_cap_is_p1(self):
        _nq_stub.q4_service_info.return_value = {'priority': 'Tier0', 'tier': 0}
        _nq_stub.q1_blast_radius.return_value = {
            'capabilities': [{'name': 'cap1', 'priority': 'Tier0'}],
            'services': [],
        }
        result = self.classify('petsite', self._signal())
        self.assertEqual(result['severity'], 'P1')

    def test_tier2_service_no_tier0_caps_is_p2(self):
        _nq_stub.q4_service_info.return_value = {'priority': 'Tier2', 'tier': 2}
        _nq_stub.q1_blast_radius.return_value = {
            'capabilities': [{'name': 'cap1', 'priority': 'Tier2'}],
            'services': [],
        }
        result = self.classify('some-service', self._signal(value=0.1))
        self.assertEqual(result['severity'], 'P2')

    def test_high_error_rate_upgrades_p2_to_p1(self):
        _nq_stub.q4_service_info.return_value = {'priority': 'Tier2', 'tier': 2}
        _nq_stub.q1_blast_radius.return_value = {'capabilities': [], 'services': []}
        result = self.classify('some-service', self._signal(value=0.9))
        self.assertEqual(result['severity'], 'P1')

    def test_result_contains_required_keys(self):
        _nq_stub.q4_service_info.return_value = {}
        _nq_stub.q1_blast_radius.return_value = {'capabilities': [], 'services': []}
        result = self.classify('svc', self._signal())
        for key in ('severity', 'strategy', 'affected_service', 'service_info',
                    'affected_capabilities', 'affected_services', 'signal'):
            self.assertIn(key, result)


# ===========================================================================
# Tests for playbook_engine.match
# ===========================================================================

class TestPlaybookMatch(unittest.TestCase):

    def setUp(self):
        from rca_engine import playbook_engine
        self.match = playbook_engine.match

    def _ctx(self, service='test-svc', severity='P1', metric='error_rate', value=0.5,
             fault_boundary=None):
        svc_info = {}
        if fault_boundary:
            svc_info['fault_boundary'] = fault_boundary
        return {
            'affected_service': service,
            'severity': severity,
            'signal': {'metric': metric, 'value': value},
            'service_info': svc_info,
        }

    def test_alb_5xx_spike_matched(self):
        ctx = self._ctx(metric='alb_5xx_rate', value=0.3)
        result = self.match(ctx)
        self.assertEqual(result['matched_playbook'], 'alb_5xx_spike')

    def test_db_connection_exhausted_matched(self):
        ctx = self._ctx(metric='db_timeout_errors', value=0.95)
        result = self.match(ctx)
        self.assertEqual(result['matched_playbook'], 'db_connection_exhausted')

    def test_crashloop_matched(self):
        ctx = self._ctx(metric='pod_status', value='CrashLoopBackOff')
        result = self.match(ctx)
        self.assertEqual(result['matched_playbook'], 'crashloop')

    def test_single_az_matched(self):
        ctx = self._ctx(metric='az_availability', fault_boundary='az')
        result = self.match(ctx)
        self.assertEqual(result['matched_playbook'], 'single_az_down')

    def test_no_match_returns_dynamic(self):
        ctx = self._ctx(metric='unknown_metric', value=0.0)
        result = self.match(ctx)
        self.assertIsNone(result['matched_playbook'])
        self.assertFalse(result['can_auto_exec'])

    def test_p0_disables_auto_exec_for_low_risk(self):
        ctx = self._ctx(metric='db_timeout_errors', value=0.95, severity='P0')
        result = self.match(ctx)
        self.assertFalse(result['can_auto_exec'])

    def test_result_has_required_keys(self):
        ctx = self._ctx()
        result = self.match(ctx)
        for key in ('matched_playbook', 'steps', 'can_auto_exec', 'risk', 'mode'):
            self.assertIn(key, result)


if __name__ == '__main__':
    unittest.main()
