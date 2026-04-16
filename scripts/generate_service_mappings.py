#!/usr/bin/env python3
"""
scripts/generate_service_mappings.py — 从 profiles/petsite.yaml 生成 service_mappings.json

供 Lambda 函数使用（避免在 Lambda 中打包 pydantic/yaml 依赖）。
在 CDK deploy 之前运行，将 JSON 复制到 Lambda 目录。
"""

import json
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

import yaml

PROFILE_PATH = os.path.join(_PROJECT_ROOT, "profiles", "petsite.yaml")
LAMBDA_DIRS = [
    os.path.join(_PROJECT_ROOT, "infra", "lambda", "etl_deepflow"),
    os.path.join(_PROJECT_ROOT, "infra", "lambda", "etl_aws"),
]

def main():
    with open(PROFILE_PATH, encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    services = profile.get("services", {})
    
    # 生成 Lambda 需要的映射
    mappings = {
        "tier_map": {},
        "k8s_alias": {},      # k8s_label → neptune_name
        "neptune_to_k8s": {}, # neptune_name → k8s_deployment
        "namespace": profile.get("kubernetes", {}).get("namespace", "default"),
        "region": profile.get("aws_resources", {}).get("primary_region", "ap-northeast-1"),
    }

    for name, cfg in services.items():
        neptune = cfg.get("neptune_name", name)
        mappings["tier_map"][neptune] = cfg.get("tier", "Tier2")
        
        k8s_dep = cfg.get("k8s_deployment", name)
        k8s_label = cfg.get("k8s_label", k8s_dep)
        mappings["neptune_to_k8s"][neptune] = k8s_dep
        
        # k8s label → neptune (只在不同时)
        if k8s_label != neptune:
            mappings["k8s_alias"][k8s_label] = neptune
        if k8s_dep != neptune:
            mappings["k8s_alias"][k8s_dep] = neptune
        
        for alias in cfg.get("aliases", []):
            mappings["k8s_alias"][alias] = neptune

    output = json.dumps(mappings, indent=2, ensure_ascii=False)
    
    for d in LAMBDA_DIRS:
        out_path = os.path.join(d, "service_mappings.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"✅ Written: {out_path}")

    print(f"\nMappings: {len(mappings['tier_map'])} services, {len(mappings['k8s_alias'])} aliases")


if __name__ == "__main__":
    main()
