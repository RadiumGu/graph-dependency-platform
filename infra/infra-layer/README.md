# infra-layer — 基础设施层图谱扩展

Neptune 知识图谱基础设施层扩展工具集。

## 目录结构

```
infra-layer/
├── README.md
└── tools/
    ├── scan-service-db-mapping.py  服务-DB映射扫描脚本
    └── service-db-mapping.json     扫描结果
```

## 已实现

**Neptune ETL 扩展**（`../lambda/etl_aws/neptune_etl_aws.py` Step 8b）：
- `collect_eks_pods()` — K8s API 采集 Pod，EC2 API 查 AZ
- `find_vertex_by_name()` — 节点查找工具函数
- `SERVICE_DB_MAPPING` — 服务→DB 静态映射表
- Pod 节点 + `RunsOn` 边（Service → Pod → AZ）
- Database 节点 + `ConnectsTo` 边（Service → Database）

**扫描结果（2026-02-28）**：
```
pethistory → serviceseks2-databaseb269d8bb (adoptions, postgres)
```

**Lambda 模块**（`infra_collector.py`）已独立到 [rca_engine](../../rca_engine/) 仓库。

## 过程文档

见 `~/tech/rca/docs/infra-layer-modeling.md`
