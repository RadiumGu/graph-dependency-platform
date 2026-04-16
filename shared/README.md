[中文文档](./README_CN.md) | English

# shared/ — Shared Modules

Cross-module shared utilities used by all subsystems (rca, chaos, dr-plan-generator, infra ETL).

## Files

| File | Description |
|------|-------------|
| `service_registry.py` | `ServiceRegistry` — centralized bidirectional service name mapping |

## ServiceRegistry

Provides bidirectional lookups between Neptune logical names, K8s deployments, K8s labels, and DeepFlow app names.

```python
from shared.service_registry import ServiceRegistry

registry = ServiceRegistry(services_dict)  # From profile["services"]

# Lookups
registry.k8s_to_neptune("pay-for-adoption")     # → "payforadoption"
registry.neptune_to_k8s("petsearch")             # → "search-service"
registry.neptune_to_deployment("petsite")         # → "petsite-deployment"
registry.get_tier("petsite")                      # → "Tier0"
registry.get_type("petsite")                      # → "Microservice"
registry.all_service_names()                      # → ["petsite", "petsearch", ...]
```

## Design Principles

- **Single source of truth**: all name mappings come from `profiles/petsite.yaml`
- **Backward compatible**: modules that can't load profile fall back to hardcoded defaults
- **Zero-config for existing code**: `ServiceRegistry` is optional — modules can also use `EnvironmentProfile` directly
