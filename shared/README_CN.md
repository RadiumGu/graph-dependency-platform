[English](./README.md) | 中文文档

# shared/ — 共享模块

所有子系统（rca、chaos、dr-plan-generator、infra ETL）使用的跨模块共享工具。

## 文件

| 文件 | 说明 |
|------|------|
| `service_registry.py` | `ServiceRegistry` — 集中式双向服务名映射 |

## ServiceRegistry

提供 Neptune 逻辑名、K8s deployment、K8s label、DeepFlow app name 之间的双向查找。

```python
from shared.service_registry import ServiceRegistry

registry = ServiceRegistry(services_dict)

registry.k8s_to_neptune("pay-for-adoption")     # → "payforadoption"
registry.neptune_to_k8s("petsearch")             # → "search-service"
registry.all_service_names()                      # → ["petsite", "petsearch", ...]
```

## 设计原则

- **单一数据源**：所有名称映射来自 `profiles/petsite.yaml`
- **向后兼容**：无法加载 profile 的模块回退到硬编码默认值
- **零配置**：现有代码无需修改即可继续工作
