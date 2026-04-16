[English](./README.md) | 中文文档

# profiles/ — 环境配置 Profile

集中式、Profile 驱动的配置，支持多应用切换。所有模块（rca、chaos、dr-plan-generator、infra ETL）从单一 YAML profile 加载服务名映射、K8s 命名空间和资源标识符。

## 文件

| 文件 | 说明 |
|------|------|
| `profile_loader.py` | `EnvironmentProfile` 类 — 加载 YAML，提供点路径访问 + 辅助方法 |
| `petsite.yaml` | PetSite 应用 Profile（服务、K8s、DR 配置、基础设施） |

## 使用方法

```python
from profiles.profile_loader import EnvironmentProfile

profile = EnvironmentProfile()                      # 自动加载 petsite.yaml
ns = profile.k8s_namespace                           # "default"
region = profile.get("dr.source_region")             # "ap-northeast-1"
deploy = profile.get_deployment_name("petsite")      # "petsite-deployment"
```

## 添加新应用

1. 复制 `petsite.yaml` → `myapp.yaml`
2. 更新所有服务名、K8s deployment、tier 和基础设施引用
3. 设置 `PROFILE_PATH=profiles/myapp.yaml`
4. 所有模块自动使用新映射
