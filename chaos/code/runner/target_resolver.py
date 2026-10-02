"""
target_resolver.py - 运行时目标解析器

将 YAML 中的逻辑服务名（service_name + resource_type）解析为真实 AWS ARN（FIS）
或验证 K8s Pod 存在（Chaos Mesh）。

优先级（FIS）：本地缓存 (TTL=1h) → Neptune 图谱（OpenCypher + Gremlin + SigV4）→ AWS API 兜底

支持的 resource_type（FIS）：
  lambda:function  — service_name 匹配 Lambda 函数名片段
  rds:cluster      — service_name 匹配 Microservice Neptune 节点名，走 DependsOn 边
  eks:nodegroup    — service_name 为 EKS 集群名（如 PetSite）
  ec2:subnet       — service_name 为 AZ 名（如 ap-northeast-1a）
  ec2:volume       — service_name 为 AZ 名，查找挂载到 EKS 节点的 EBS 卷
  ec2:instance     — service_name 匹配 EC2 实例标签 Name

缓存文件（审计留底）：
  targets-fis.json       — FIS 实验目标（ARN）
  targets-chaosmesh.json — Chaos Mesh 实验目标（Pod 列表）
"""
from __future__ import annotations

import glob as _glob
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

import boto3
import yaml as _yaml

from .config import REGION, ACCOUNT_ID, SERVICE_TO_K8S_LABEL
from .neptune_client import query_opencypher, query_gremlin

if TYPE_CHECKING:
    from .experiment import Experiment

logger = logging.getLogger(__name__)

# ─── 缓存文件路径 ──────────────────────────────────────────────────────────────

_BASE          = os.path.join(os.path.dirname(__file__), "..")
FIS_CACHE_FILE = os.path.join(_BASE, "targets-fis.json")
CM_CACHE_FILE  = os.path.join(_BASE, "targets-chaosmesh.json")
# 向后兼容旧路径
CACHE_FILE     = FIS_CACHE_FILE

CACHE_TTL = 3600  # 1 小时


class TargetResolver:
    """
    运行时目标解析器。
    - FIS:        resolve(service_name, resource_type) → ARN string
    - Chaos Mesh: resolve_chaosmesh_target(service, namespace) → dict
    - 批量:       resolve_all_experiments(experiments_dir) → {"fis": ..., "chaosmesh": ...}
    """

    def __init__(self, tags: dict = None):
        self._tags: dict             = tags or {}
        self._fis_cache: dict        = {}
        self._fis_cache_loaded: bool = False
        self._cm_cache: dict         = {}
        self._cm_cache_loaded: bool  = False

    # ─── Tag 过滤 ──────────────────────────────────────────────────────────────

    def _match_tags(self, resource_arn: str) -> bool:
        """检查资源 tag 是否匹配所有 filter tag（resourcegroupstaggingapi）"""
        if not self._tags:
            return True
        try:
            client = boto3.client("resourcegroupstaggingapi", region_name=REGION)
            resp = client.get_resources(ResourceARNList=[resource_arn])
            mappings = resp.get("ResourceTagMappingList", [])
            if not mappings:
                return False
            tags = {t["Key"]: t["Value"] for t in mappings[0].get("Tags", [])}
            return all(tags.get(k) == v for k, v in self._tags.items())
        except Exception as e:
            logger.warning(f"Tag 检查失败（放行）: {resource_arn}: {e}")
            return True

    # ─── Neptune 查询（委托给 neptune_client.py）─────────────────────────────

    def _neptune_query(self, cypher: str) -> list[dict]:
        """OpenCypher 查询，委托给统一 neptune_client。"""
        return query_opencypher(cypher)

    def _neptune_gremlin_query(self, gremlin: str) -> list:
        """Gremlin 查询，委托给统一 neptune_client。"""
        return query_gremlin(gremlin)

    # ─── Neptune 解析层 ────────────────────────────────────────────────────────

    def _resolve_from_neptune(self, service_name: str, resource_type: str) -> Optional[str]:
        try:
            if resource_type == "lambda:function":
                rows = self._neptune_query(
                    f"MATCH (m:Microservice)-[:DependsOn]->(l:LambdaFunction) "
                    f"WHERE m.name CONTAINS '{service_name}' OR l.name CONTAINS '{service_name}' "
                    f"RETURN l.arn AS arn LIMIT 1"
                )
                if rows and rows[0].get("arn"):
                    return rows[0]["arn"]

            elif resource_type == "rds:cluster":
                # 先试 OpenCypher DependsOn 边
                rows = self._neptune_query(
                    f"MATCH (m:Microservice {{name: '{service_name}'}})-[:DependsOn]->(r:RDSCluster) "
                    f"RETURN r.arn AS arn LIMIT 1"
                )
                if rows and rows[0].get("arn"):
                    return rows[0]["arn"]

                # OpenCypher 无结果时改用 Gremlin（DependsOn 边 OpenCypher 可能查不到）
                vals = self._neptune_gremlin_query(
                    f"g.V().has('Microservice','name','{service_name}')"
                    f".out('DependsOn').hasLabel('RDSCluster').values('arn')"
                )
                if vals:
                    return str(vals[0])

            elif resource_type == "eks:nodegroup":
                rows = self._neptune_query(
                    f"MATCH (e:EKSCluster {{name: '{service_name}'}}) RETURN e.arn AS arn LIMIT 1"
                )
                if rows and rows[0].get("arn"):
                    arn = rows[0]["arn"]
                    if ":nodegroup/" in arn:
                        return arn

            elif resource_type == "ec2:subnet":
                rows = self._neptune_query(
                    f"MATCH (s:Subnet {{availability_zone: '{service_name}'}}) "
                    f"RETURN s.arn AS arn LIMIT 1"
                )
                if rows and rows[0].get("arn"):
                    return rows[0]["arn"]

        except Exception as e:
            logger.warning(f"Neptune 查询失败，将走 AWS API 兜底: {e}")

        return None

    # ─── AWS API 兜底 ──────────────────────────────────────────────────────────

    def _resolve_from_aws(self, service_name: str, resource_type: str) -> Optional[str]:
        try:
            if resource_type == "lambda:function":
                return self._find_lambda_arn(service_name)
            elif resource_type == "rds:cluster":
                return self._find_rds_cluster_arn(service_name)
            elif resource_type == "eks:nodegroup":
                return self._find_eks_nodegroup_arn(service_name)
            elif resource_type == "ec2:subnet":
                return self._find_subnet_arn(service_name)
            elif resource_type == "ec2:volume":
                return self._find_ebs_volume_arn(service_name)
            elif resource_type == "ec2:instance":
                return self._find_instance_arn(service_name)
        except Exception as e:
            logger.error(f"AWS API 解析失败 [{resource_type}/{service_name}]: {e}")
        return None

    def _find_lambda_arn(self, service_name: str) -> Optional[str]:
        lam = boto3.client("lambda", region_name=REGION)
        paginator = lam.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page["Functions"]:
                if service_name.lower() in fn["FunctionName"].lower():
                    if self._tags and not self._match_tags(fn["FunctionArn"]):
                        continue
                    return fn["FunctionArn"]
        return None

    def _find_rds_cluster_arn(self, service_name: str) -> Optional[str]:
        rds = boto3.client("rds", region_name=REGION)
        resp = rds.describe_db_clusters()
        clusters = resp.get("DBClusters", [])

        # 排除 grafana / neptune 等非应用集群（避免误匹配 grafana-aurora-mysql）
        EXCLUDE = ("grafana", "neptune")
        candidates = [
            c for c in clusters
            if not any(x in c["DBClusterIdentifier"].lower() for x in EXCLUDE)
        ]

        # 先按服务名片段精确匹配（候选集中）
        for c in candidates:
            if service_name.lower() in c["DBClusterIdentifier"].lower():
                if self._tags and not self._match_tags(c["DBClusterArn"]):
                    continue
                return c["DBClusterArn"]

        # 优先返回 petsite / serviceseks2 相关集群
        PREFER = ("petsite", "serviceseks2")
        for c in candidates:
            cid = c["DBClusterIdentifier"].lower()
            if any(x in cid for x in PREFER) and c.get("Status") == "available":
                if self._tags and not self._match_tags(c["DBClusterArn"]):
                    continue
                return c["DBClusterArn"]

        # 兜底：第一个可用的非排除集群
        for c in candidates:
            if c.get("Status") == "available":
                if self._tags and not self._match_tags(c["DBClusterArn"]):
                    continue
                return c["DBClusterArn"]
        return None

    def _find_eks_nodegroup_arn(self, cluster_name: str) -> Optional[str]:
        eks = boto3.client("eks", region_name=REGION)
        resp = eks.list_nodegroups(clusterName=cluster_name)
        groups = resp.get("nodegroups", [])
        if not groups:
            return None
        for ng in groups:
            detail = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng)
            arn = detail["nodegroup"]["nodegroupArn"]
            if self._tags and not self._match_tags(arn):
                continue
            return arn
        return None

    def _find_subnet_arn(self, az: str) -> Optional[str]:
        ec2 = boto3.client("ec2", region_name=REGION)
        # 优先找 EKS 工作子网（带 kubernetes.io 标签）
        for tag_key in ["kubernetes.io/cluster/PetSite", "kubernetes.io/role/internal-elb"]:
            resp = ec2.describe_subnets(Filters=[
                {"Name": "availabilityZone", "Values": [az]},
                {"Name": "tag-key",          "Values": [tag_key]},
            ])
            subnets = resp.get("Subnets", [])
            for s in subnets:
                arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:subnet/{s['SubnetId']}"
                if self._tags and not self._match_tags(arn):
                    continue
                return arn
        # 兜底：AZ 内第一个子网
        resp = ec2.describe_subnets(Filters=[
            {"Name": "availabilityZone", "Values": [az]},
        ])
        subnets = resp.get("Subnets", [])
        for s in subnets:
            arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:subnet/{s['SubnetId']}"
            if self._tags and not self._match_tags(arn):
                continue
            return arn
        return None

    def _find_ebs_volume_arn(self, az: str) -> Optional[str]:
        ec2 = boto3.client("ec2", region_name=REGION)
        # 优先找挂载到 EKS 节点的卷（带 eks:cluster-name 标签）
        resp = ec2.describe_volumes(Filters=[
            {"Name": "availability-zone",    "Values": [az]},
            {"Name": "status",               "Values": ["in-use"]},
            {"Name": "tag:eks:cluster-name", "Values": ["PetSite"]},
        ])
        vols = resp.get("Volumes", [])
        if not vols:
            # 兜底：AZ 内任意已挂载卷
            resp = ec2.describe_volumes(Filters=[
                {"Name": "availability-zone", "Values": [az]},
                {"Name": "status",            "Values": ["in-use"]},
            ])
            vols = resp.get("Volumes", [])
        for v in vols:
            arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:volume/{v['VolumeId']}"
            if self._tags and not self._match_tags(arn):
                continue
            return arn
        return None

    def _find_instance_arn(self, service_name: str) -> Optional[str]:
        ec2 = boto3.client("ec2", region_name=REGION)
        resp = ec2.describe_instances(Filters=[
            {"Name": "tag:Name",            "Values": [f"*{service_name}*"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/{inst['InstanceId']}"
                if self._tags and not self._match_tags(arn):
                    continue
                return arn
        return None

    # ─── FIS 缓存层 ───────────────────────────────────────────────────────────

    def _load_fis_cache(self):
        if self._fis_cache_loaded:
            return
        self._fis_cache_loaded = True

        # 向后兼容：也检查旧的 targets.json
        old_cache = os.path.join(os.path.dirname(__file__), "..", "targets.json")
        for path in (FIS_CACHE_FILE, old_cache):
            if not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
                resolved_at = data.get("resolved_at", "")
                ttl         = data.get("ttl_seconds", CACHE_TTL)
                if resolved_at:
                    age = time.time() - datetime.fromisoformat(resolved_at).timestamp()
                    if age > ttl:
                        logger.info(f"FIS 目标缓存已过期（{age:.0f}s > {ttl}s），忽略")
                        continue
                self._fis_cache = data.get("targets", {})
                logger.debug(f"已加载 {len(self._fis_cache)} 条 FIS 缓存目标（来自 {path}）")
                return
            except Exception as e:
                logger.warning(f"FIS 缓存加载失败（忽略）: {e}")

    def _save_fis_cache(self):
        data = {
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": CACHE_TTL,
            "targets":     self._fis_cache,
        }
        try:
            with open(os.path.abspath(FIS_CACHE_FILE), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"FIS 缓存保存失败（非致命）: {e}")

    # ─── Chaos Mesh 缓存层 ────────────────────────────────────────────────────

    def _load_cm_cache(self):
        if self._cm_cache_loaded:
            return
        self._cm_cache_loaded = True
        if not os.path.exists(CM_CACHE_FILE):
            return
        try:
            with open(CM_CACHE_FILE) as f:
                data = json.load(f)
            self._cm_cache = data.get("targets", {})
            logger.debug(f"已加载 {len(self._cm_cache)} 条 Chaos Mesh 缓存目标")
        except Exception as e:
            logger.warning(f"Chaos Mesh 缓存加载失败（忽略）: {e}")

    def _save_cm_cache(self):
        data = {
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "targets":     self._cm_cache,
        }
        try:
            with open(os.path.abspath(CM_CACHE_FILE), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Chaos Mesh 缓存保存失败（非致命）: {e}")

    # ─── Chaos Mesh Pod 解析 ──────────────────────────────────────────────────

    def resolve_chaosmesh_target(self, service: str, namespace: str = "default") -> dict:
        """
        解析 Chaos Mesh 目标（K8s Pod），写入 targets-chaosmesh.json，返回：
        {
            "service": "petsearch",
            "namespace": "default",
            "label_selector": "app=petsearch",
            "pods": [
                {"name": "petsearch-xxx", "status": "Running", "ip": "10.x.x.x", "node": "ip-..."}
            ],
            "replicas": 2,
            "resolved_at": "2026-03-20T..."
        }
        """
        self._load_cm_cache()
        cache_key = f"{service}:{namespace}"

        # 逻辑名 → K8s app label 映射
        k8s_label = SERVICE_TO_K8S_LABEL.get(service, service)

        pods_raw     = self._kubectl_get_pods(k8s_label, namespace)
        replicas_raw = self._kubectl_get_replicas(k8s_label, namespace)

        # 这条路径的 namespace 由调用方精确传入（runner / chaos_mcp / 内部批量），
        # 本来就工作正常，所以保持 pods/replicas 的原有类型不变，避免波及
        # 三个调用方。但 kubectl 失败不能继续伪装成「0 个 Pod」／「0 副本」——
        # 新增 kubectl_failed 让想区分的调用方能区分，不想区分的照旧。
        #
        # ⚠️ 两个查询**都**要看：pods 成功而 replicas 失败时，replicas 退回 0
        # 就是「服务被缩容到零」这一强陈述，与本方法要消除的缺陷同形。
        # 只看 pods_raw 会在这里留下一个和原 bug 一样的洞。
        kubectl_failed = pods_raw is None or replicas_raw is None
        pods     = pods_raw if pods_raw is not None else []
        replicas = replicas_raw if replicas_raw is not None else 0

        entry = {
            "service":        service,
            "k8s_app_label":  k8s_label,
            "namespace":      namespace,
            "label_selector": f"app={k8s_label}",
            "pods":           pods,
            "replicas":       replicas,
            "kubectl_failed": kubectl_failed,
            "resolved_at":    datetime.now(timezone.utc).isoformat(),
        }

        # 保留已有的 experiments 关联列表
        if cache_key in self._cm_cache and "experiments" in self._cm_cache[cache_key]:
            entry["experiments"] = self._cm_cache[cache_key]["experiments"]

        self._cm_cache[cache_key] = entry
        self._save_cm_cache()

        logger.info(
            f"Chaos Mesh 目标已解析: {service}/{namespace} → "
            f"{len(pods)} pods, replicas={replicas}"
        )
        return entry

    def _kubectl_get_pods(self, service: str, namespace: str | None) -> list[dict] | None:
        """kubectl get pods -l app=<service> → [{name, namespace, status, ip, node}]

        ## 返回 None 与返回 [] 是两件不同的事（2026-10-02 修）

            None  查询**失败** —— kubectl 不可用／非 0 退出／输出不可解析。
                  **什么也没陈述**，下游不得据此推断集群状态。
            []    查询**成功**且该服务确实没有 Pod。这是关于集群的事实陈述。

        原实现两种情况都返回 `[]`，而且**不检查 returncode** ——
        kubectl 失败时 `r.stdout` 为空，`json.loads("{}")` 得到 `{}`，
        `items` 取到 `[]`，于是「查询失败」与「真的 0 个 Pod」完全同形。
        这是本项目反复踩的「失败长得像成功」，这次的代价见
        `get_infra_snapshot` 的 docstring。

        ## namespace=None 表示跨所有 namespace

        本方法有两类调用方，对 namespace 的需求相反：

          · `resolve_chaosmesh_target` —— 精确定位注入目标，**必须**限定
            namespace，跨 namespace 匹配可能打到同名 label 的无关 Pod；
          · `get_infra_snapshot` —— 了解服务当前状态，服务列表本身可能
            跨 namespace，限定任何单一值都是猜。

        所以 namespace 由调用方决定，None 走 `--all-namespaces`。
        返回的每个 Pod 都带 `namespace` 字段，便于调用方回推实际位置。
        """
        ns_args = ["--all-namespaces"] if namespace is None else ["-n", namespace]
        ns_label = namespace or "ALL"
        try:
            r = subprocess.run(
                ["kubectl", "get", "pods", *ns_args,
                 "-l", f"app={service}", "-o", "json"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                logger.warning(
                    f"kubectl get pods 非 0 退出 [{service}/{ns_label}] "
                    f"rc={r.returncode}: {(r.stderr or '').strip()[:200]}")
                return None
            items = json.loads(r.stdout or "{}").get("items", [])
            pods = []
            for pod in items:
                meta   = pod.get("metadata", {})
                status = pod.get("status", {})
                phase  = status.get("phase", "Unknown")
                if phase in ("Succeeded", "Completed"):
                    continue
                pods.append({
                    "name":      meta.get("name", ""),
                    "namespace": meta.get("namespace", ""),
                    "status":    phase,
                    "ip":        status.get("podIP", ""),
                    "node":      pod.get("spec", {}).get("nodeName", ""),
                })
            return pods
        except Exception as e:
            logger.warning(f"kubectl get pods 失败 [{service}/{ns_label}]: {e}")
            return None

    def _kubectl_get_replicas(self, service: str, namespace: str) -> int | None:
        """按 spec.selector.matchLabels.app 匹配 deployment → spec.replicas

        ## 为什么按 spec.selector 匹配，而不是名字、也不是 metadata.labels

        这里连踩两层，都由实测揭示（2026-10-02）：

        **第一层：deployment 名 ≠ app label。** 原实现是
        `kubectl get deployment <service>`，拿 app label 当名字用：

            NAME                    spec.selector.app   REPLICAS
            pay-for-adoption        pay-for-adoption    2     ← 一致
            petsite-deployment      petsite             2     ← 不一致
            pethistory-deployment   pethistory          2     ← 不一致

        于是 `petsite` / `pethistory` 必然 NotFound，而原实现把 NotFound
        折成 `0` —— 「这个服务被缩容到零副本」。

        **第二层：`-l app=X` 也不行。** `-l` 过滤的是 deployment 自己的
        `metadata.labels`，而实测这两个 deployment **没有** app label：

            NAME                    metadata.labels.app   spec.selector.app
            petsite-deployment      （缺失）               petsite
            pethistory-deployment   （缺失）               pethistory

        `spec.selector.matchLabels` 是 K8s 的**必填**字段，一定存在；而且它
        才是「这个 deployment 管哪些 Pod」的权威定义 —— 与 `_kubectl_get_pods`
        的 `-l app=<service>` 正好是同一个 label 的两端。所以列出 namespace 下
        全部 deployment 再按它匹配，是唯一可靠的口径。deployment 数量是个位数，
        全量列举的代价可忽略。

        ## 与 _kubectl_get_pods 同一纪律：None 表示查不到，不是 0
        """
        try:
            r = subprocess.run(
                ["kubectl", "get", "deployment", "-n", namespace, "-o", "json"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                logger.warning(
                    f"kubectl get deployment 非 0 退出 [{namespace}] "
                    f"rc={r.returncode}: {(r.stderr or '').strip()[:200]}")
                return None
            items = json.loads(r.stdout or "{}").get("items", [])
            matched = [
                it for it in items
                if (((it.get("spec") or {}).get("selector") or {})
                    .get("matchLabels", {}).get("app") == service)
            ]
            if not matched:
                logger.warning(
                    f"{namespace} 下没有 deployment 的 spec.selector 匹配 "
                    f"app={service} —— 返回 None 而不是 0，「查不到」不是「零副本」")
                return None
            if len(matched) > 1:
                names = [m.get("metadata", {}).get("name", "?") for m in matched]
                logger.warning(
                    f"app={service} 在 {namespace} 匹配到 {len(matched)} 个 "
                    f"deployment {names}，取第一个")
            val = (matched[0].get("spec") or {}).get("replicas")
            return int(val) if isinstance(val, int) else None
        except Exception as e:
            logger.warning(f"kubectl get deployment 失败 [{service}/{namespace}]: {e}")
            return None

    # ─── 公共接口（FIS ARN 解析）──────────────────────────────────────────────

    def resolve(self, service_name: str, resource_type: str) -> Optional[str]:
        """
        解析 FIS 目标：service_name + resource_type → ARN。
        优先级：FIS 缓存 → Neptune → AWS API
        """
        self._load_fis_cache()
        cache_key = f"{resource_type}:{service_name}"

        if cache_key in self._fis_cache:
            logger.debug(f"FIS 缓存命中: {cache_key}")
            return self._fis_cache[cache_key]["arn"]

        arn    = self._resolve_from_neptune(service_name, resource_type)
        source = "neptune"
        if not arn:
            arn    = self._resolve_from_aws(service_name, resource_type)
            source = "aws-api"

        if arn:
            self._fis_cache[cache_key] = {"arn": arn, "resolved_from": source}
            self._save_fis_cache()
            logger.info(f"ARN 已解析（{source}）: {cache_key} → {arn}")
        else:
            logger.warning(f"无法解析 ARN: {cache_key}")

        return arn

    def refresh(self):
        """清除所有本地缓存（FIS + Chaos Mesh），强制重新解析"""
        self._fis_cache        = {}
        self._fis_cache_loaded = True
        self._cm_cache         = {}
        self._cm_cache_loaded  = True
        old_cache = os.path.join(os.path.dirname(__file__), "..", "targets.json")
        for path in (FIS_CACHE_FILE, CM_CACHE_FILE, old_cache):
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"已清除缓存文件: {path}")

    def resolve_experiment(self, experiment: "Experiment") -> None:
        """
        填充 Experiment.fault.extra_params 中缺失的 ARN 字段。
        读取 extra_params.service_name + extra_params.resource_type，
        解析后写入对应的 ARN key（function_arn / cluster_arn / nodegroup_arn 等）。
        失败时仅记录警告，不抛出异常。
        """
        extra = experiment.fault.extra_params
        if not extra:
            return

        service_name  = extra.get("service_name")
        resource_type = extra.get("resource_type")
        if not service_name or not resource_type:
            return

        try:
            arn = self.resolve(service_name, resource_type)
        except Exception as e:
            logger.warning(f"实验 {experiment.name}: ARN 解析异常（跳过）: {e}")
            return

        if not arn:
            logger.warning(
                f"实验 {experiment.name}: 无法解析 {resource_type}/{service_name}，"
                f"将使用 YAML 中已有 ARN（如有）"
            )
            return

        # 按 resource_type 写入对应的 ARN key
        if resource_type == "lambda:function":
            extra["function_arn"] = arn
        elif resource_type == "rds:cluster":
            extra["cluster_arn"] = arn
        elif resource_type == "eks:nodegroup":
            extra["nodegroup_arn"] = arn
        elif resource_type == "ec2:subnet":
            extra["subnet_arn"] = arn
        elif resource_type == "ec2:volume":
            extra.setdefault("volume_arns", [])
            if arn not in extra["volume_arns"]:
                extra["volume_arns"].append(arn)
        elif resource_type == "ec2:instance":
            extra["instance_arn"] = arn

        logger.info(f"实验 {experiment.name}: {resource_type} → {arn}")

    def resolve_all_experiments(self, experiments_dir: str) -> dict:
        """
        批量解析所有实验目标，按 backend 分别写入：
        - targets-fis.json       (FIS ARN 审计)
        - targets-chaosmesh.json (Chaos Mesh Pod 审计)
        返回 {"fis": {...}, "chaosmesh": {...}}
        """
        self._load_fis_cache()
        self._load_cm_cache()

        fis_results: dict = {}
        cm_results:  dict = {}

        pattern    = os.path.join(experiments_dir, "**", "*.yaml")
        yaml_files = sorted(_glob.glob(pattern, recursive=True))

        for path in yaml_files:
            try:
                with open(path) as f:
                    d = _yaml.safe_load(f)
            except Exception as e:
                logger.warning(f"跳过 YAML（读取失败）: {path}: {e}")
                continue

            if not d or not d.get("enabled", True):
                continue

            backend  = d.get("backend", "chaosmesh")
            target   = d.get("target", {})
            exp_name = d.get("name", os.path.basename(path))

            if backend == "fis":
                extra     = (d.get("fault") or {}).get("extra_params") or {}
                svc_name  = extra.get("service_name", "")
                res_type  = extra.get("resource_type", "")
                if not svc_name or not res_type:
                    continue

                cache_key = f"{res_type}:{svc_name}"
                arn       = self.resolve(svc_name, res_type)
                source    = self._fis_cache.get(cache_key, {}).get("resolved_from", "unknown")

                fis_results[cache_key] = {
                    "arn":           arn,
                    "resolved_from": source,
                    "experiment":    exp_name,
                }
                logger.debug(f"FIS 目标: {cache_key} → {arn} ({source})")

            else:
                # Chaos Mesh：解析 Pod 目标
                svc = target.get("service", "")
                ns  = target.get("namespace", "default")
                if not svc:
                    continue

                cache_key = f"{svc}:{ns}"
                try:
                    entry = self.resolve_chaosmesh_target(svc, ns)
                except Exception as e:
                    logger.warning(f"Chaos Mesh 目标解析失败 [{svc}/{ns}]: {e}")
                    k8s_label = SERVICE_TO_K8S_LABEL.get(svc, svc)
                    entry = {
                        "service": svc, "namespace": ns,
                        "k8s_app_label": k8s_label,
                        "label_selector": f"app={k8s_label}",
                        "pods": [], "replicas": 0,
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                    }

                if cache_key not in cm_results:
                    cm_results[cache_key] = entry.copy()
                    cm_results[cache_key].setdefault("experiments", [])

                if exp_name not in cm_results[cache_key]["experiments"]:
                    cm_results[cache_key]["experiments"].append(exp_name)

                # 同步回 cm_cache（带 experiments 列表）
                self._cm_cache[cache_key] = cm_results[cache_key]

        # 写入最终 Chaos Mesh 缓存（包含 experiments 字段）
        self._save_cm_cache()

        return {"fis": fis_results, "chaosmesh": cm_results}

    # ─── 基础设施快照（供 HypothesisAgent 使用）────────────────────────────────

    def get_infra_snapshot(self, services: list[str], namespace: str | None = None) -> dict:
        """
        为 HypothesisAgent 提供实时基础设施快照。

        对每个逻辑服务名，解析当前 K8s Pod 状态 + FIS 可用资源，
        返回轻量级摘要，供 LLM 生成更精准的假设。

        ## namespace 默认值从 "default" 改成 None（2026-10-02 修）

        原签名是 `namespace: str = "default"`，而唯一的调用方
        （`runner/neptune_helpers.py` 的 `query_infra_snapshot`）**不传这个参数** ——
        于是它永远只查 `default`。实测集群里业务 Pod 全在 `petadoptions`：

            petadoptions   pay-for-adoption-dfdff688f-7lhq7   2/2  Running  13d
            petadoptions   pay-for-adoption-dfdff688f-pxj4j   2/2  Running  13d

        图谱侧同样证实：`petadoptions` 21 个活跃 Running Pod，`default` 只有 4 个
        且都不是业务服务。所以 `running_pods` **恒为 0**。

        后果不是某个数字难看，而是 agent 被喂了一个假事实。
        `hypothesis_strands.py` 的 prompt 规则 4 要求「对每个目标服务调用
        query_infra_snapshot」、规则 5 要求「没有 running Pod 的服务不要提
        pod-kill 类故障」—— 于是 agent 在两种反应间摇摆：

            2026-09-27 cron   判断无从下手 → 返回 0 个假设 → golden S002 失败
            同日手工复跑       生成 8 个，但全部标注「在服务恢复运行后执行」，
                              并在输出里写下「当前所有服务 running_pods = 0」

        同一个错误输入，两次不同反应。**不稳定的不是模型，是它面对矛盾输入
        （Tier1 服务、有 active 依赖边、却一个 Pod 都没有）的行为天然不确定。**

        这个默认值是从已删除的 `DirectBedrockHypothesis._query_infra_snapshot()`
        抽取代码时丢的（见 `neptune_helpers.py` 的 docstring）。

        快照用途跨 namespace（服务列表本身可能分布在多个 namespace，限定任何
        单一值都是猜），所以默认 None；而 `resolve_chaosmesh_target` 刻意保留
        精确 namespace —— 它定位注入目标，跨 namespace 可能打到无关 Pod。

        ## running_pods=None 与 running_pods=0 必须分开

        kubectl 查询失败时写 `query_failed: True` 且三个计数都是 None，
        不再退回 0。理由与 `_kubectl_get_pods` 返回 None 一致：
        查不到什么也没陈述，而「0 个 Pod」是关于集群的强陈述。

        返回格式：
        {
            "petsite": {
                "k8s": {"namespace": "petadoptions", "replicas": 2,
                        "running_pods": 2, "total_pods": 2, "nodes": ["ip-10-..."]},
                "aws_resources": {"lambda:function": "arn:aws:...", ...},
            },
            "someservice": {
                "k8s": {"query_failed": True, "running_pods": None, ...},
                "aws_resources": {...},
            },
        }
        """
        snapshot = {}
        self._load_fis_cache()

        for service in services:
            entry: dict = {"k8s": None, "aws_resources": {}}

            # K8s Pod 状态
            k8s_label = SERVICE_TO_K8S_LABEL.get(service, service)
            _unknown = {
                "app_label":    k8s_label,
                "query_failed": True,
                "namespace":    None,
                "replicas":     None,
                "running_pods": None,
                "total_pods":   None,
                "nodes":        [],
            }
            try:
                pods = self._kubectl_get_pods(k8s_label, namespace)
                if pods is None:
                    entry["k8s"] = _unknown
                else:
                    running = [p for p in pods if p.get("status") == "Running"]
                    nodes = list({p.get("node", "") for p in running if p.get("node")})
                    # replicas 必须用 Pod 实际所在的 namespace 查。跨 namespace
                    # 查询时调用方传的是 None，此时只有 Pod 自己知道它在哪。
                    ns_actual = namespace or next(
                        (p.get("namespace") for p in pods if p.get("namespace")), None)
                    replicas = (self._kubectl_get_replicas(k8s_label, ns_actual)
                                if ns_actual else None)
                    entry["k8s"] = {
                        "app_label":    k8s_label,
                        "namespace":    ns_actual,
                        "replicas":     replicas,
                        "running_pods": len(running),
                        "total_pods":   len(pods),
                        "nodes":        nodes,
                    }
            except Exception as e:
                # 原来是 logger.debug —— 这类失败会让 agent 拿到假事实，
                # 不该只在 debug 级别可见。
                logger.warning(f"K8s 快照查询异常 {service}: {e}")
                entry["k8s"] = _unknown

            # FIS 可用资源（从缓存中查找关联的 ARN）
            for cache_key, cached in self._fis_cache.items():
                # cache_key 格式: "resource_type:service_name"
                parts = cache_key.split(":", 1)
                if len(parts) == 2:
                    res_type_prefix = parts[0]
                    svc_part = cache_key.split(":")[-1] if ":" in cache_key else ""
                    # 宽松匹配：服务名出现在 cache_key 中
                    if service.lower() in cache_key.lower():
                        arn = cached.get("arn", "")
                        if arn:
                            entry["aws_resources"][cache_key] = arn

            snapshot[service] = entry

        logger.info(f"基础设施快照: {len(snapshot)} 个服务")
        return snapshot
