"""
tests/test_18_unit_shared.py — Sprint 5 Shared Modules Unit Tests

Test IDs:
  S5-01: profile_loader — YAML loading and attribute access
  S5-02: profile_loader — missing field defaults handling
  S5-03: service_registry — service name mapping all directions
  S5-04: service_registry — alias resolution
  S5-05: service_registry — unknown service name doesn't crash
"""

import os
import sys
import tempfile

import pytest

PROJECT_ROOT = "/home/ubuntu/tech/graph-dependency-platform"
PROFILES_DIR = os.path.join(PROJECT_ROOT, "profiles")
SHARED_DIR = os.path.join(PROJECT_ROOT, "shared")

for _p in [PROFILES_DIR, SHARED_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

PETSITE_YAML = os.path.join(PROFILES_DIR, "petsite.yaml")

try:
    from profile_loader import EnvironmentProfile

    _PROFILE_AVAILABLE = True
except ImportError:
    _PROFILE_AVAILABLE = False

try:
    from service_registry import ServiceRegistry

    _REGISTRY_AVAILABLE = True
except ImportError:
    _REGISTRY_AVAILABLE = False


# ─── S5-01 ───────────────────────────────────────────────────────────────────


def test_s5_01_profile_loader_yaml_load():
    """S5-01: profile_loader — YAML loading and attribute access."""
    if not _PROFILE_AVAILABLE:
        pytest.skip("profile_loader not available")
    if not os.path.exists(PETSITE_YAML):
        pytest.skip("petsite.yaml not found")

    profile = EnvironmentProfile(PETSITE_YAML)

    assert profile.name == "PetSite"
    assert profile.domain == "petsite.example.com"
    assert profile.health_endpoint == "/health"
    assert profile.k8s_namespace == "petadoptions"
    assert isinstance(profile.dns_ttl_normal, int)
    assert profile.dns_ttl_normal == 300
    assert profile.dns_ttl_pre_switchover == 60
    assert profile.alarm_prefix == "petsite"
    # Dotted path access
    assert profile.get("profile.name") == "PetSite"
    assert profile.get("dns.ttl_normal") == 300
    # health_check_command substitutes placeholders
    cmd = profile.health_check_command
    assert "petsite.example.com" in cmd
    assert "/health" in cmd
    # deployment_map fallback (no entry → original name)
    assert profile.get_deployment_name("unknown-svc") == "unknown-svc"


# ─── S5-02 ───────────────────────────────────────────────────────────────────


def test_s5_02_profile_loader_missing_field_defaults():
    """S5-02: profile_loader — missing field defaults handling."""
    if not _PROFILE_AVAILABLE:
        pytest.skip("profile_loader not available")

    minimal_yaml = "profile:\n  name: MinimalTest\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        f.write(minimal_yaml)
        tmp_path = f.name

    try:
        profile = EnvironmentProfile(tmp_path)
        assert profile.name == "MinimalTest"
        # All properties must return defaults, not raise
        assert profile.domain == ""
        assert profile.health_endpoint == "/health"
        assert profile.k8s_namespace == "default"
        assert profile.dns_ttl_normal == 300
        assert profile.dns_ttl_pre_switchover == 60
        assert profile.alarm_prefix == "app"
        # get() on missing nested key returns provided default
        assert profile.get("does.not.exist", "fallback") == "fallback"
        assert profile.get("also.missing") is None
        # deployment_map: no map defined → return as-is
        assert profile.get_deployment_name("petsite") == "petsite"
    finally:
        os.unlink(tmp_path)


# ─── S5-03 ───────────────────────────────────────────────────────────────────


def test_s5_03_service_registry_all_directions():
    """S5-03: service_registry — service name mapping all directions."""
    if not _REGISTRY_AVAILABLE:
        pytest.skip("service_registry not available")

    services = {
        "petsearch": {
            "tier": "Tier0",
            "k8s_deployment": "search-service",
            "neptune_name": "petsearch",
            "deepflow_app": "search-service",
            "cloudwatch": {
                "namespace": "AWS/ApplicationELB",
                "dimension_name": "TargetGroup",
                "dimension_value": "search-service",
            },
        },
        "payforadoption": {
            "tier": "Tier0",
            "k8s_deployment": "pay-for-adoption",
            "neptune_name": "payforadoption",
        },
    }
    reg = ServiceRegistry(services)

    # Neptune → K8s
    assert reg.neptune_to_k8s("petsearch") == "search-service"
    assert reg.neptune_to_k8s("payforadoption") == "pay-for-adoption"

    # K8s → Neptune
    assert reg.k8s_to_neptune("search-service") == "petsearch"
    assert reg.k8s_to_neptune("pay-for-adoption") == "payforadoption"

    # Tier
    assert reg.get_tier("petsearch") == "Tier0"
    assert reg.get_tier("payforadoption") == "Tier0"

    # DeepFlow app
    assert reg.get_deepflow_app("petsearch") == "search-service"

    # CloudWatch config
    cw = reg.get_cloudwatch_config("petsearch")
    assert cw["namespace"] == "AWS/ApplicationELB"
    assert cw["dimension_value"] == "search-service"

    # All service names
    names = reg.all_service_names()
    assert "petsearch" in names
    assert "payforadoption" in names
    assert len(names) == 2


# ─── S5-04 ───────────────────────────────────────────────────────────────────


def test_s5_04_service_registry_alias_resolution():
    """S5-04: service_registry — alias resolution."""
    if not _REGISTRY_AVAILABLE:
        pytest.skip("service_registry not available")

    services = {
        "pethistory": {
            "tier": "Tier1",
            "k8s_deployment": "pethistory-deployment",
            "neptune_name": "pethistory",
            "aliases": ["petadoptionshistory", "pethistory-service"],
        }
    }
    reg = ServiceRegistry(services)

    # Aliases → Neptune standard name
    assert reg.resolve("petadoptionshistory") == "pethistory"
    assert reg.resolve("pethistory-service") == "pethistory"

    # Neptune standard name → itself
    assert reg.resolve("pethistory") == "pethistory"

    # K8s deployment → Neptune standard name
    assert reg.resolve("pethistory-deployment") == "pethistory"

    # Alias also accessible via k8s_to_neptune
    assert reg.k8s_to_neptune("petadoptionshistory") == "pethistory"

    # Tier lookup via alias
    assert reg.get_tier("petadoptionshistory") == "Tier1"


# ─── S5-05 ───────────────────────────────────────────────────────────────────


def test_s5_05_service_registry_unknown_service():
    """S5-05: service_registry — unknown service name doesn't crash."""
    if not _REGISTRY_AVAILABLE:
        pytest.skip("service_registry not available")

    services = {
        "petsite": {
            "tier": "Tier0",
            "k8s_deployment": "petsite-deployment",
            "neptune_name": "petsite",
        }
    }
    reg = ServiceRegistry(services)

    # resolve: unknown → returns the name unchanged
    assert reg.resolve("completely-unknown-svc") == "completely-unknown-svc"

    # neptune_to_k8s: unknown → returns the name unchanged
    assert reg.neptune_to_k8s("ghost-svc") == "ghost-svc"

    # k8s_to_neptune: unknown → returns the name unchanged
    assert reg.k8s_to_neptune("ghost-deploy") == "ghost-deploy"

    # get_tier: unknown → falls back to "Tier2"
    assert reg.get_tier("ghost-svc") == "Tier2"

    # get_deepflow_app: unknown → returns the name unchanged
    assert reg.get_deepflow_app("ghost-svc") == "ghost-svc"

    # get_cloudwatch_config: unknown → empty dict
    assert reg.get_cloudwatch_config("ghost-svc") == {}

    # all_service_names: only registered names
    assert "ghost-svc" not in reg.all_service_names()
