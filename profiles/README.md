[中文文档](./README_CN.md) | English

# profiles/ — Environment Profiles

Centralized, profile-driven configuration for multi-application support. All modules (rca, chaos, dr-plan-generator, infra ETL) load service name mappings, K8s namespaces, and resource identifiers from a single YAML profile.

## Files

| File | Description |
|------|-------------|
| `profile_loader.py` | `EnvironmentProfile` class — loads YAML, provides dot-path access + helper methods |
| `petsite.yaml` | PetSite application profile (services, K8s, DR config, infrastructure) |

## Profile Structure (petsite.yaml)

```yaml
profile:
  name: petsite
  version: "1.0"

kubernetes:
  cluster_name: PetSite
  namespace: default
  context_source: source-eks
  context_target: target-eks

services:
  petsite:
    neptune_name: petsite
    k8s_deployment: petsite-deployment
    k8s_label: petsite
    tier: Tier0
    type: Microservice
    # ...

dr:
  default_scope: az
  source_region: ap-northeast-1
  target_region: us-west-2
  domain: petsite.example.com

parameter_store:
  keys:
    slack_interact_url: /petsite/slack/interact-url
    rca_rate_limit: /petsite/rca/rate-limit/{service}
```

## Usage

```python
from profiles.profile_loader import EnvironmentProfile

profile = EnvironmentProfile()                    # Auto-loads petsite.yaml
profile = EnvironmentProfile("path/to/other.yaml") # Load custom profile

# Dot-path access
ns = profile.k8s_namespace                         # "default"
region = profile.get("dr.source_region")            # "ap-northeast-1"
deploy = profile.get_deployment_name("petsite")     # "petsite-deployment"
```

## Adding a New Application

1. Copy `petsite.yaml` → `myapp.yaml`
2. Update all service names, K8s deployments, tiers, and infrastructure references
3. Set `PROFILE_PATH=profiles/myapp.yaml` (or pass to `EnvironmentProfile(path)`)
4. All modules will automatically use the new mappings
