"""
Chaos Engineering — 混沌实验历史、韧性评估 & 新实验执行

从 Neptune 读取 ChaosExperiment 节点，展示服务韧性状况；
支持从 UI 直接发起（或 dry-run）Chaos Mesh / FIS 实验。
"""
import os
import sys
import subprocess
import threading
import time
from pathlib import Path

import streamlit as st

# ── 路径设置 ──────────────────────────────────────────────────────────────────
_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_DEMO_DIR, "..", ".."))
_RCA_ROOT = os.path.join(_PROJECT_ROOT, "rca")

for _p in [_PROJECT_ROOT, _RCA_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault(
    "NEPTUNE_ENDPOINT",
    "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com",
)
os.environ.setdefault("REGION", "ap-northeast-1")

st.set_page_config(page_title="混沌工程", page_icon="💥", layout="wide")

KNOWN_SERVICES = [
    "petsite",
    "petsearch",
    "payforadoption",
    "petlistadoptions",
    "petadoptionshistory",
    "petfood",
    "trafficgenerator",
]

RESULT_ICONS = {"passed": "✅", "failed": "❌", "running": "⏳", "aborted": "⛔"}
FAULT_ICONS = {
    "cpu": "🔥",
    "memory": "💾",
    "network": "🌐",
    "latency": "⏱️",
    "disk": "💿",
    "pod": "📦",
    "az": "🌩️",
    "region": "🌍",
}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_all_experiments() -> list:
    """查询所有 ChaosExperiment 节点。"""
    try:
        from neptune import neptune_client as nc

        cypher = """
        MATCH (svc:Microservice)-[:TestedBy]->(exp:ChaosExperiment)
        RETURN svc.name AS service, exp.experiment_id AS id,
               exp.fault_type AS fault_type, exp.result AS result,
               exp.recovery_time_sec AS recovery_time,
               exp.degradation_rate AS degradation,
               exp.timestamp AS timestamp
        ORDER BY exp.timestamp DESC
        LIMIT 200
        """
        return nc.results(cypher)
    except Exception as exc:
        return []


@st.cache_data(ttl=60, show_spinner=False)
def fetch_untested_services() -> list:
    """查找没有混沌实验的服务。"""
    try:
        from neptune import neptune_client as nc

        cypher = """
        MATCH (s:Microservice)
        WHERE NOT (s)-[:TestedBy]->(:ChaosExperiment)
        RETURN s.name AS service, s.recovery_priority AS priority
        ORDER BY CASE s.recovery_priority WHEN 'Tier0' THEN 0
                                          WHEN 'Tier1' THEN 1 ELSE 2 END
        """
        return nc.results(cypher)
    except Exception as exc:
        return []


@st.cache_data(ttl=60, show_spinner=False)
def fetch_service_experiments(service_name: str) -> list:
    """Q18：特定服务的混沌实验历史。"""
    try:
        from neptune import neptune_queries as nq
        return nq.q18_chaos_history(service_name, limit=20)
    except Exception as exc:
        return []


# ── UI ────────────────────────────────────────────────────────────────────────
st.title("💥 混沌工程")
st.markdown(
    "浏览混沌实验历史，评估服务韧性覆盖率。"
    "实验数据通过 AWS FIS 执行后由 `neptune_sync.py` 写入 Neptune 图谱。"
)

# ── 实验目录 ──────────────────────────────────────────────────────────────────
CHAOS_CODE_DIR = Path("/home/ubuntu/tech/chaos/code")

EXPERIMENT_CATALOG = [
    # --- FIS 实验 ---
    # EKS 节点
    {"file": "experiments/fis/eks-node/fis-eks-terminate-node-1a.yaml",
     "backend": "fis", "name": "fis-eks-terminate-node-1a",
     "desc": "终止 AZ 1a 一个 EKS 工作节点，验证 Pod 重调度与 HA"},
    {"file": "experiments/fis/eks-node/fis-eks-terminate-node-1c.yaml",
     "backend": "fis", "name": "fis-eks-terminate-node-1c",
     "desc": "终止 AZ 1c 一个 EKS 工作节点，验证跨 AZ 高可用"},
    # EC2
    {"file": "experiments/fis/ec2/fis-ec2-stop-node-1a.yaml",
     "backend": "fis", "name": "fis-ec2-stop-node-1a",
     "desc": "停止 AZ 1a EKS 工作节点 EC2，验证 Pod 重调度"},
    {"file": "experiments/fis/ec2/fis-ec2-stop-node-1c.yaml",
     "backend": "fis", "name": "fis-ec2-stop-node-1c",
     "desc": "停止 AZ 1c EKS 工作节点 EC2，验证跨 AZ 韧性"},
    {"file": "experiments/fis/ec2/fis-ec2-reboot-node-1a.yaml",
     "backend": "fis", "name": "fis-ec2-reboot-node-1a",
     "desc": "重启 AZ 1a EKS 节点 EC2，验证节点恢复和 Pod 重启"},
    {"file": "experiments/fis/ec2/fis-ec2-reboot-node-1c.yaml",
     "backend": "fis", "name": "fis-ec2-reboot-node-1c",
     "desc": "重启 AZ 1c EKS 节点 EC2，验证 1c 侧恢复能力"},
    # EKS Pod
    {"file": "experiments/fis/eks-pod/fis-eks-pod-delete-petsite.yaml",
     "backend": "fis", "name": "fis-eks-pod-delete-petsite",
     "desc": "FIS 原生 Pod Delete — petsite，对比 ChaosMesh 行为差异"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-delete-petsearch.yaml",
     "backend": "fis", "name": "fis-eks-pod-delete-petsearch",
     "desc": "FIS 原生 Pod Delete — petsearch，验证搜索服务 HA"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-delete-payforadoption.yaml",
     "backend": "fis", "name": "fis-eks-pod-delete-payforadoption",
     "desc": "FIS 原生 Pod Delete — payforadoption，验证支付链路 HA"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-delete-pethistory.yaml",
     "backend": "fis", "name": "fis-eks-pod-delete-pethistory",
     "desc": "FIS 原生 Pod Delete — pethistory，验证历史服务 HA"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-cpu-stress-petsite.yaml",
     "backend": "fis", "name": "fis-eks-pod-cpu-stress-petsite",
     "desc": "petsite Pod CPU 压力，验证限流与延迟影响"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-cpu-stress-petsearch.yaml",
     "backend": "fis", "name": "fis-eks-pod-cpu-stress-petsearch",
     "desc": "petsearch Pod CPU 压力，验证搜索性能降级处理"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-cpu-stress-payforadoption.yaml",
     "backend": "fis", "name": "fis-eks-pod-cpu-stress-payforadoption",
     "desc": "payforadoption Pod CPU 压力，验证支付超时处理"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-cpu-stress-pethistory.yaml",
     "backend": "fis", "name": "fis-eks-pod-cpu-stress-pethistory",
     "desc": "pethistory Pod CPU 压力，验证 Tier1 降级处理"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-memory-stress-petsearch.yaml",
     "backend": "fis", "name": "fis-eks-pod-memory-stress-petsearch",
     "desc": "petsearch Pod 内存压力，验证 OOM 韧性"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-memory-stress-petsite.yaml",
     "backend": "fis", "name": "fis-eks-pod-memory-stress-petsite",
     "desc": "petsite Pod 内存压力，验证主站 OOM 韧性"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-memory-stress-payforadoption.yaml",
     "backend": "fis", "name": "fis-eks-pod-memory-stress-payforadoption",
     "desc": "payforadoption Pod 内存压力，验证支付服务 OOM 处理"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-memory-stress-pethistory.yaml",
     "backend": "fis", "name": "fis-eks-pod-memory-stress-pethistory",
     "desc": "pethistory Pod 内存压力，验证 Tier1 OOM 韧性"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-latency-petsearch.yaml",
     "backend": "fis", "name": "fis-eks-pod-latency-petsearch",
     "desc": "petsearch Pod 网络延迟注入，验证搜索超时处理"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-latency-payforadoption.yaml",
     "backend": "fis", "name": "fis-eks-pod-latency-payforadoption",
     "desc": "payforadoption Pod 网络延迟 500ms，验证支付超时韧性"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-latency-pethistory.yaml",
     "backend": "fis", "name": "fis-eks-pod-latency-pethistory",
     "desc": "pethistory Pod 网络延迟，验证 Tier1 延迟传播"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-loss-payforadoption.yaml",
     "backend": "fis", "name": "fis-eks-pod-loss-payforadoption",
     "desc": "payforadoption Pod 丢包 30%，验证支付重试机制"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-loss-petsite.yaml",
     "backend": "fis", "name": "fis-eks-pod-loss-petsite",
     "desc": "petsite Pod 丢包 30%，验证主站网络韧性"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-loss-petsearch.yaml",
     "backend": "fis", "name": "fis-eks-pod-loss-petsearch",
     "desc": "petsearch Pod 丢包 30%，验证搜索网络韧性"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-loss-pethistory.yaml",
     "backend": "fis", "name": "fis-eks-pod-loss-pethistory",
     "desc": "pethistory Pod 丢包 30%，验证 Tier1 丢包韧性"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-io-stress-payforadoption.yaml",
     "backend": "fis", "name": "fis-eks-pod-io-stress-payforadoption",
     "desc": "payforadoption Pod IO 压力，验证存储层韧性"},
    {"file": "experiments/fis/eks-pod/fis-eks-pod-blackhole-petsite.yaml",
     "backend": "fis", "name": "fis-eks-pod-blackhole-petsite",
     "desc": "petsite Pod 端口黑洞，验证下游依赖熔断"},
    # Lambda
    {"file": "experiments/fis/lambda/fis-lambda-delay-petstatusupdater.yaml",
     "backend": "fis", "name": "fis-lambda-delay-petstatusupdater",
     "desc": "给 petstatusupdater Lambda 注入 3s 延迟，验证 SQS 韧性"},
    {"file": "experiments/fis/lambda/fis-lambda-delay-petadoptionshistory.yaml",
     "backend": "fis", "name": "fis-lambda-delay-petadoptionshistory",
     "desc": "给 petadoptionshistory Lambda 注入 2s 延迟，验证历史查询韧性"},
    {"file": "experiments/fis/lambda/fis-lambda-error-petadoptionshistory.yaml",
     "backend": "fis", "name": "fis-lambda-error-petadoptionshistory",
     "desc": "给 petadoptionshistory Lambda 注入错误，验证错误处理"},
    {"file": "experiments/fis/lambda/fis-lambda-http-response-petstatusupdater.yaml",
     "backend": "fis", "name": "fis-lambda-http-response-petstatusupdater",
     "desc": "给 petstatusupdater Lambda 注入 HTTP 响应错误"},
    # RDS/Aurora
    {"file": "experiments/fis/rds/fis-aurora-failover.yaml",
     "backend": "fis", "name": "fis-aurora-failover",
     "desc": "触发 Aurora 主从切换，验证 petlistadoptions/payforadoption 重连 SLO"},
    {"file": "experiments/fis/rds/fis-aurora-reboot.yaml",
     "backend": "fis", "name": "fis-aurora-reboot",
     "desc": "重启 Aurora 实例（~60s 中断），验证 payforadoption 重试与连接池恢复"},
    {"file": "experiments/fis/rds/fis-aurora-reboot-petlistadoptions.yaml",
     "backend": "fis", "name": "fis-aurora-reboot-petlistadoptions",
     "desc": "重启 Aurora 实例，验证 petlistadoptions 连接池恢复"},
    # 网络/存储
    {"file": "experiments/fis/network-infra/fis-ebs-io-latency.yaml",
     "backend": "fis", "name": "fis-ebs-io-latency",
     "desc": "给 EKS 节点 EBS 卷注入高 IO 延迟，验证存储超时处理"},
    {"file": "experiments/fis/network-infra/fis-ebs-pause-io-1a.yaml",
     "backend": "fis", "name": "fis-ebs-pause-io-1a",
     "desc": "暂停 AZ 1a EBS IO，验证存储不可用时的应用韧性"},
    {"file": "experiments/fis/network-infra/fis-network-disrupt-az-1a.yaml",
     "backend": "fis", "name": "fis-network-disrupt-az-1a",
     "desc": "中断 AZ 1a 子网连通性（Network ACL），验证 AZ 故障切换"},
    {"file": "experiments/fis/network-infra/fis-network-disrupt-az-1c.yaml",
     "backend": "fis", "name": "fis-network-disrupt-az-1c",
     "desc": "中断 AZ 1c 子网连通性，验证 1c 侧 AZ 故障切换"},
    {"file": "experiments/fis/network-infra/fis-vpc-endpoint-disrupt.yaml",
     "backend": "fis", "name": "fis-vpc-endpoint-disrupt",
     "desc": "干扰 VPC Endpoint 连接，验证 AWS 服务依赖韧性"},
    # AZ 场景
    {"file": "experiments/fis/az-scenarios/fis-az-app-slowdown-1a.yaml",
     "backend": "fis", "name": "fis-az-app-slowdown-1a",
     "desc": "AZ 1a 应用层延迟场景，验证跨 AZ 流量切换"},
    {"file": "experiments/fis/az-scenarios/fis-az-app-slowdown-1c.yaml",
     "backend": "fis", "name": "fis-az-app-slowdown-1c",
     "desc": "AZ 1c 应用层延迟场景，验证跨 AZ 流量切换"},
    {"file": "experiments/fis/az-scenarios/fis-scenario-az-power-interruption-1a.yaml",
     "backend": "fis", "name": "fis-scenario-az-power-interruption-1a",
     "desc": "AZ 1a 断电场景：EC2 停止 + EBS 暂停 + RDS Failover，验证多 AZ HA"},
    {"file": "experiments/fis/az-scenarios/fis-scenario-az-power-interruption-1c.yaml",
     "backend": "fis", "name": "fis-scenario-az-power-interruption-1c",
     "desc": "AZ 1c 断电场景：EC2 停止 + EBS 暂停 + RDS Failover，验证多 AZ HA"},
    {"file": "experiments/fis/az-scenarios/fis-cross-az-traffic-slowdown-1a.yaml",
     "backend": "fis", "name": "fis-cross-az-traffic-slowdown-1a",
     "desc": "跨 AZ 流量丢包（1a 侧），验证流量均衡韧性"},
    {"file": "experiments/fis/az-scenarios/fis-cross-az-traffic-slowdown-1c.yaml",
     "backend": "fis", "name": "fis-cross-az-traffic-slowdown-1c",
     "desc": "跨 AZ 流量丢包（1c 侧），验证流量均衡韧性"},
    # API 注入
    {"file": "experiments/fis/api-injection/fis-api-internal-error-eks.yaml",
     "backend": "fis", "name": "fis-api-internal-error-eks",
     "desc": "注入 EKS API 内部错误，验证控制面错误处理"},
    {"file": "experiments/fis/api-injection/fis-api-throttle-eks.yaml",
     "backend": "fis", "name": "fis-api-throttle-eks",
     "desc": "注入 EKS API 限流错误，验证退避重试"},
    {"file": "experiments/fis/api-injection/fis-api-unavailable-rds.yaml",
     "backend": "fis", "name": "fis-api-unavailable-rds",
     "desc": "注入 RDS API 不可用错误，验证数据面降级策略"},
    # --- Chaos Mesh 实验 ---
    {"file": "experiments/tier0/petsite-pod-kill.yaml",
     "backend": "chaosmesh", "name": "petsite-pod-kill-tier0",
     "desc": "杀死 petsite 50% Pod（Tier0），验证韧性与可观测性"},
    {"file": "experiments/tier0/payforadoption-pod-kill.yaml",
     "backend": "chaosmesh", "name": "payforadoption-pod-kill-tier0",
     "desc": "杀死 payforadoption Pod，验证支付链路韧性和 RCA 精准度"},
    {"file": "experiments/tier0/payforadoption-http-chaos.yaml",
     "backend": "chaosmesh", "name": "payforadoption-http-chaos-tier0",
     "desc": "对 payforadoption 注入 HTTP 混沌（Tier0）"},
    {"file": "experiments/tier0/petsearch-network-delay.yaml",
     "backend": "chaosmesh", "name": "petsearch-network-delay-tier0",
     "desc": "给 petsearch 注入网络延迟（Tier0）"},
    {"file": "experiments/tier1/pethistory-network-delay.yaml",
     "backend": "chaosmesh", "name": "pethistory-network-delay-tier1",
     "desc": "给 pethistory 注入网络延迟（Tier1）"},
    {"file": "experiments/tier1/petlistadoptions-network-loss.yaml",
     "backend": "chaosmesh", "name": "petlistadoptions-network-loss-tier1",
     "desc": "对 petlistadoptions 注入网络丢包（Tier1）"},
    {"file": "experiments/network/petsearch-network-delay.yaml",
     "backend": "chaosmesh", "name": "petsearch-network-delay-network",
     "desc": "给 petsearch 加 200ms 延迟，验证延迟传播和 RCA 准确性"},
    # --- 补充 Tier0 实验 ---
    {"file": "experiments/tier0/petsite-network-delay-tier0.yaml",
     "backend": "chaosmesh", "name": "petsite-network-delay-tier0",
     "desc": "给 petsite 注入 200ms 网络延迟，验证延迟传播"},
    {"file": "experiments/tier0/petsite-pod-memory-stress-tier0.yaml",
     "backend": "chaosmesh", "name": "petsite-pod-memory-stress-tier0",
     "desc": "给 petsite Pod 注入 256MB 内存压力，验证 OOM 韧性"},
    {"file": "experiments/tier0/petsite-network-loss-tier0.yaml",
     "backend": "chaosmesh", "name": "petsite-network-loss-tier0",
     "desc": "给 petsite 注入 30% 网络丢包，验证重试机制"},
    {"file": "experiments/tier0/petsite-io-chaos-tier0.yaml",
     "backend": "chaosmesh", "name": "petsite-io-chaos-tier0",
     "desc": "给 petsite 注入 IO 延迟，验证磁盘 IO 韧性"},
    {"file": "experiments/tier0/petsearch-pod-kill-tier0.yaml",
     "backend": "chaosmesh", "name": "petsearch-pod-kill-tier0",
     "desc": "杀死 petsearch 50% Pod，验证搜索服务 HA"},
    {"file": "experiments/tier0/petsearch-pod-cpu-stress-tier0.yaml",
     "backend": "chaosmesh", "name": "petsearch-pod-cpu-stress-tier0",
     "desc": "给 petsearch 注入 CPU 压力（2 worker × 80%）"},
    {"file": "experiments/tier0/petsearch-network-loss-tier0.yaml",
     "backend": "chaosmesh", "name": "petsearch-network-loss-tier0",
     "desc": "给 petsearch 注入 30% 网络丢包"},
    {"file": "experiments/tier0/petsearch-io-chaos-tier0.yaml",
     "backend": "chaosmesh", "name": "petsearch-io-chaos-tier0",
     "desc": "给 petsearch 注入 IO 延迟，验证搜索存储层韧性"},
    {"file": "experiments/tier0/payforadoption-network-delay-tier0.yaml",
     "backend": "chaosmesh", "name": "payforadoption-network-delay-tier0",
     "desc": "给 payforadoption 注入 500ms 延迟，验证支付超时处理"},
    {"file": "experiments/tier0/payforadoption-network-loss-tier0.yaml",
     "backend": "chaosmesh", "name": "payforadoption-network-loss-tier0",
     "desc": "给 payforadoption 注入 30% 丢包，验证支付链路韧性"},
    {"file": "experiments/tier0/payforadoption-time-chaos-tier0.yaml",
     "backend": "chaosmesh", "name": "payforadoption-time-chaos-tier0",
     "desc": "给 payforadoption 注入时钟偏移 -5m，验证时序敏感逻辑"},
    # --- 补充 Tier1 实验 ---
    {"file": "experiments/tier1/pethistory-pod-kill-tier1.yaml",
     "backend": "chaosmesh", "name": "pethistory-pod-kill-tier1",
     "desc": "杀死 pethistory 50% Pod，验证历史记录服务 HA"},
    {"file": "experiments/tier1/pethistory-pod-cpu-stress-tier1.yaml",
     "backend": "chaosmesh", "name": "pethistory-pod-cpu-stress-tier1",
     "desc": "给 pethistory 注入 CPU 压力，验证查询性能降级处理"},
    {"file": "experiments/tier1/pethistory-network-loss-tier1.yaml",
     "backend": "chaosmesh", "name": "pethistory-network-loss-tier1",
     "desc": "给 pethistory 注入 30% 丢包，验证 Tier1 网络韧性"},
    {"file": "experiments/tier1/petlistadoptions-network-delay-tier1.yaml",
     "backend": "chaosmesh", "name": "petlistadoptions-network-delay-tier1",
     "desc": "给 petlistadoptions 注入 200ms 延迟，验证列表查询超时"},
    {"file": "experiments/tier1/petlistadoptions-pod-kill-tier1.yaml",
     "backend": "chaosmesh", "name": "petlistadoptions-pod-kill-tier1",
     "desc": "杀死 petlistadoptions 50% Pod，验证领养列表服务 HA"},
    {"file": "experiments/tier1/petstatusupdater-pod-memory-stress-tier1.yaml",
     "backend": "chaosmesh", "name": "petstatusupdater-pod-memory-stress-tier1",
     "desc": "给 petstatusupdater 注入内存压力，验证 SQS 消费者稳定性"},
    {"file": "experiments/tier1/petstatusupdater-network-delay-tier1.yaml",
     "backend": "chaosmesh", "name": "petstatusupdater-network-delay-tier1",
     "desc": "给 petstatusupdater 注入网络延迟，验证异步处理韧性"},
]

BACKEND_ICONS = {"fis": "☁️ FIS", "chaosmesh": "🕸️ Chaos Mesh"}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("控制")
    if st.button("🔄 刷新数据", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("**故障类型图例**")
    for ft, icon in FAULT_ICONS.items():
        st.markdown(f"{icon} `{ft}`")

    st.markdown("---")
    st.markdown("**实验结果**")
    for r, icon in RESULT_ICONS.items():
        st.markdown(f"{icon} `{r}`")

# ── 拉取数据 ──────────────────────────────────────────────────────────────────
with st.spinner("从 Neptune 加载混沌实验数据…"):
    experiments = fetch_all_experiments()
    untested = fetch_untested_services()

# ── 总览指标 ──────────────────────────────────────────────────────────────────
total = len(experiments)
passed = sum(1 for e in experiments if e.get("result") == "passed")
failed = sum(1 for e in experiments if e.get("result") == "failed")
services_tested = len({e.get("service") for e in experiments if e.get("service")})

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("实验总数", total)
c2.metric("通过", passed, delta=f"{passed/total*100:.0f}%" if total else "0%")
c3.metric("失败", failed, delta=f"-{failed/total*100:.0f}%" if total else "0%", delta_color="inverse")
c4.metric("已测试服务", services_tested)
c5.metric("未覆盖服务", len(untested), delta_color="inverse" if untested else "normal")

# ── 标签页 ────────────────────────────────────────────────────────────────────
tab_launch, tab_all, tab_service, tab_coverage, tab_insights = st.tabs(
    ["🚀 发起实验", "全部实验", "服务维度", "覆盖率分析", "韧性洞察"]
)

with tab_launch:
    st.subheader("🚀 发起新混沌实验")

    # ── 后端选择 ──────────────────────────────────────────────────────────────
    backend_filter = st.radio(
        "后端类型",
        ["全部", "☁️ FIS", "🕸️ Chaos Mesh"],
        horizontal=True,
        key="launch_backend_filter",
    )

    # 过滤
    if backend_filter == "☁️ FIS":
        visible = [e for e in EXPERIMENT_CATALOG if e["backend"] == "fis"]
    elif backend_filter == "🕸️ Chaos Mesh":
        visible = [e for e in EXPERIMENT_CATALOG if e["backend"] == "chaosmesh"]
    else:
        visible = EXPERIMENT_CATALOG

    # ── 分组展示（FIS 按类型分组，Chaos Mesh 按 Tier 分组）─────────────────
    GROUP_ORDER = {
        "fis": {
            "EKS 节点":   lambda e: "eks-node" in e["file"],
            "Lambda":      lambda e: "/lambda/" in e["file"],
            "RDS/Aurora":  lambda e: "/rds/" in e["file"],
            "网络/存储":   lambda e: "/network" in e["file"] or "/ebs" in e["file"],
        },
        "chaosmesh": {
            "Tier0 核心":  lambda e: "/tier0/" in e["file"],
            "Tier1 服务":  lambda e: "/tier1/" in e["file"],
            "网络实验":    lambda e: "/network/" in e["file"],
        },
        "all": {
            "☁️ FIS":         lambda e: e["backend"] == "fis",
            "🕸️ Chaos Mesh": lambda e: e["backend"] == "chaosmesh",
        },
    }
    backend_key = (
        "fis" if backend_filter == "☁️ FIS"
        else "chaosmesh" if backend_filter == "🕸️ Chaos Mesh"
        else "all"
    )
    groups = GROUP_ORDER[backend_key]

    # 按分组生成带 header 的选项列表
    grouped_options = []
    seen = set()
    for group_name, match_fn in groups.items():
        members = [e for e in visible if match_fn(e) and e["name"] not in seen]
        if members:
            grouped_options.append({"_header": group_name})
            for m in members:
                grouped_options.append(m)
                seen.add(m["name"])
    # 兜底：没被分到任何组的
    leftovers = [e for e in visible if e["name"] not in seen]
    if leftovers:
        grouped_options.append({"_header": "其他"})
        grouped_options.extend(leftovers)

    # 只把真正的实验（非 header）放进 selectbox
    real_options = [o for o in grouped_options if "_header" not in o]

    if not real_options:
        st.info("暂无可用实验。")
    else:
        def _format_exp(e):
            backend_tag = "☁️" if e["backend"] == "fis" else "🕸️"
            return f"{backend_tag}  {e['name']}  —  {e['desc']}"

        selected_exp = st.selectbox(
            f"选择实验（共 {len(real_options)} 个）",
            real_options,
            format_func=_format_exp,
            key="launch_exp_select",
        )

        st.markdown(f"""
**后端**：{BACKEND_ICONS[selected_exp['backend']]}  
**实验文件**：`{selected_exp['file']}`  
**描述**：{selected_exp['desc']}
""")

        # ── 运行选项 ──
        col_opt1, col_opt2 = st.columns(2)
        with col_opt1:
            dry_run = st.toggle("🧪 Dry-run 模式（不真实注入）", value=True, key="launch_dry_run")
        with col_opt2:
            if dry_run:
                st.info("ℹ️ Dry-run：只验证配置，不执行实际故障注入")
            else:
                st.warning("⚠️ 真实执行：将向生产环境注入故障！")

        # ── 实验运行状态管理 ──────────────────────────────────────────────
        # 使用全局 session_state 键保存运行中实验的状态，
        # 这样切换页面再切回来不会丢失。
        RUN_STATE_KEY = "chaos_running_experiment"

        # 初始化运行状态结构
        if RUN_STATE_KEY not in st.session_state:
            st.session_state[RUN_STATE_KEY] = {
                "active": False,       # 是否有实验在运行
                "experiment_name": "",  # 实验名
                "pid": None,           # 子进程 PID
                "log_file": "",        # 日志文件路径
                "status": "idle",      # idle / running / completed / failed / error
                "return_code": None,
                "start_time": None,
                "dry_run": False,
                "cmd": [],
            }

        run_state = st.session_state[RUN_STATE_KEY]

        def _check_process_alive(pid: int) -> bool:
            """检查进程是否还在运行"""
            if pid is None:
                return False
            try:
                os.kill(pid, 0)  # signal 0 = 检查存在性
                return True
            except (ProcessLookupError, PermissionError):
                return False

        def _read_log_tail(log_path: str, max_chars: int = 8000) -> str:
            """读取日志文件尾部"""
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                    return content[-max_chars:] if len(content) > max_chars else content
            except FileNotFoundError:
                return "(日志文件尚未生成)"
            except Exception as e:
                return f"(读取日志失败: {e})"

        # ── 如果有进行中的实验，检查并更新状态 ──
        if run_state["active"] and run_state["pid"] is not None:
            if not _check_process_alive(run_state["pid"]):
                # 进程已结束，读取退出码
                try:
                    _, exit_status = os.waitpid(run_state["pid"], os.WNOHANG)
                    rc = os.WEXITSTATUS(exit_status) if os.WIFEXITED(exit_status) else -1
                except ChildProcessError:
                    # 不是我们的子进程（可能被 init 收养），从日志推断
                    log_content = _read_log_tail(run_state["log_file"])
                    rc = 0 if "✅ 实验完成" in log_content or "PASSED" in log_content else 1

                run_state["return_code"] = rc
                run_state["status"] = "completed" if rc == 0 else "failed"
                run_state["active"] = False
                st.cache_data.clear()  # 刷新历史数据

        # ── 显示进行中或已完成的实验状态 ──
        if run_state["status"] in ("running", "completed", "failed", "error"):
            status_icon = {
                "running": "⏳", "completed": "✅", "failed": "❌", "error": "💥"
            }[run_state["status"]]
            status_label = {
                "running": "运行中", "completed": "已完成", "failed": "失败", "error": "异常"
            }[run_state["status"]]
            dry_label = " (dry-run)" if run_state["dry_run"] else ""

            st.markdown(f"""
### {status_icon} 实验{status_label}{dry_label}

**实验**: `{run_state['experiment_name']}`  
**PID**: `{run_state['pid']}`  
**启动时间**: {run_state['start_time']}
""")

            # 日志输出
            if run_state["log_file"]:
                log_content = _read_log_tail(run_state["log_file"])
                st.code(log_content, language="bash")

            col_refresh, col_clear = st.columns(2)
            with col_refresh:
                if run_state["status"] == "running":
                    if st.button("🔄 刷新日志", use_container_width=True, key="refresh_log"):
                        st.rerun()
            with col_clear:
                if run_state["status"] in ("completed", "failed", "error"):
                    if st.button("🗑️ 清除结果", use_container_width=True, key="clear_result"):
                        run_state["status"] = "idle"
                        run_state["active"] = False
                        run_state["pid"] = None
                        st.rerun()

            # 运行中时自动刷新（每 5 秒）
            if run_state["status"] == "running":
                time.sleep(5)
                st.rerun()

        # ── 启动按钮（仅在无运行中实验时显示） ──
        if not run_state["active"] and run_state["status"] in ("idle", "completed", "failed", "error"):
            btn_label = "🧪 运行 Dry-run" if dry_run else "💥 执行实验"
            btn_type = "secondary" if dry_run else "primary"

            if st.button(btn_label, type=btn_type, use_container_width=True, key="launch_btn"):
                import tempfile
                from datetime import datetime

                exp_file = str(CHAOS_CODE_DIR / selected_exp["file"])
                cmd = ["python3", "main.py", "run", "--file", exp_file]
                if dry_run:
                    cmd.append("--dry-run")

                # 创建日志文件
                log_dir = CHAOS_CODE_DIR / "logs"
                log_dir.mkdir(exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                log_file = str(log_dir / f"experiment_{timestamp}.log")

                # 后台启动子进程，stdout/stderr 重定向到日志文件
                with open(log_file, "w") as lf:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(CHAOS_CODE_DIR),
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        text=True,
                        start_new_session=True,  # 脱离父进程组，不随 Streamlit rerun 被清理
                    )

                # 更新状态
                run_state["active"] = True
                run_state["experiment_name"] = selected_exp["name"]
                run_state["pid"] = proc.pid
                run_state["log_file"] = log_file
                run_state["status"] = "running"
                run_state["return_code"] = None
                run_state["start_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                run_state["dry_run"] = dry_run
                run_state["cmd"] = cmd

                st.rerun()

with tab_all:
    st.subheader("所有混沌实验")
    if not experiments:
        st.info(
            "Neptune 中暂无 ChaosExperiment 节点。\n\n"
            "实验完成后，`chaos/code/neptune_sync.py` 的 `write_experiment()` "
            "会将结果写入图谱。"
        )
    else:
        import pandas as pd

        df = pd.DataFrame(experiments)

        # 美化展示
        result_col = "result" if "result" in df.columns else None
        if result_col:
            df["状态"] = df[result_col].apply(
                lambda r: f"{RESULT_ICONS.get(r, '?')} {r}"
            )

        fault_col = "fault_type" if "fault_type" in df.columns else None
        if fault_col:
            df["故障类型"] = df[fault_col].apply(
                lambda f: f"{FAULT_ICONS.get((f or '').split('_')[0].lower(), '🔧')} {f or ''}"
            )

        # 筛选
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            filter_result = st.multiselect(
                "筛选结果", ["passed", "failed", "running", "aborted"],
                default=[]
            )
        with col_f2:
            filter_service = st.multiselect(
                "筛选服务",
                sorted({e.get("service", "") for e in experiments if e.get("service")}),
                default=[]
            )

        filtered = df
        if filter_result:
            filtered = filtered[filtered["result"].isin(filter_result)]
        if filter_service:
            filtered = filtered[filtered["service"].isin(filter_service)]

        display_cols = ["service", "id", "故障类型", "状态", "recovery_time", "degradation", "timestamp"]
        existing_cols = [c for c in display_cols if c in filtered.columns]
        st.dataframe(
            filtered[existing_cols].rename(columns={
                "service": "服务",
                "id": "实验ID",
                "recovery_time": "恢复时长(s)",
                "degradation": "降级率",
                "timestamp": "时间",
            }),
            use_container_width=True,
            hide_index=True,
        )

with tab_service:
    st.subheader("按服务查看实验历史")
    selected_svc = st.selectbox("选择服务", KNOWN_SERVICES, key="chaos_svc")
    with st.spinner(f"加载 {selected_svc} 的实验历史…"):
        svc_exps = fetch_service_experiments(selected_svc)

    if not svc_exps:
        st.info(f"{selected_svc} 暂无混沌实验记录。")
    else:
        import pandas as pd

        # 汇总指标
        df_svc = pd.DataFrame(svc_exps)
        pass_cnt = sum(1 for e in svc_exps if e.get("result") == "passed")
        avg_recovery = sum(
            float(e.get("recovery_time") or 0) for e in svc_exps
        ) / len(svc_exps)
        avg_degradation = sum(
            float(e.get("degradation") or 0) for e in svc_exps
        ) / len(svc_exps)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("实验次数", len(svc_exps))
        m2.metric("通过率", f"{pass_cnt/len(svc_exps)*100:.0f}%")
        m3.metric("平均恢复时长", f"{avg_recovery:.0f}s")
        m4.metric("平均降级率", f"{avg_degradation*100:.1f}%")

        st.markdown("---")
        for exp in svc_exps:
            result = exp.get("result", "")
            icon = RESULT_ICONS.get(result, "?")
            fault = exp.get("fault_type", "未知")
            fault_icon = FAULT_ICONS.get((fault or "").split("_")[0].lower(), "🔧")
            with st.expander(
                f"{icon} {fault_icon} {fault} — {exp.get('timestamp', '')}",
                expanded=False,
            ):
                c_a, c_b, c_c = st.columns(3)
                c_a.metric("结果", result)
                c_b.metric("恢复时长", f"{exp.get('recovery_time', 0)}s")
                c_c.metric(
                    "降级率",
                    f"{float(exp.get('degradation') or 0)*100:.1f}%"
                )
                if exp.get("id"):
                    st.caption(f"实验 ID: {exp['id']}")

with tab_coverage:
    st.subheader("服务韧性覆盖率")

    all_services = set(KNOWN_SERVICES)
    tested_services = {e.get("service") for e in experiments if e.get("service")}
    untested_set = all_services - tested_services

    # 饼图（用文本模拟）
    coverage_pct = len(tested_services) / len(all_services) * 100 if all_services else 0
    st.progress(int(coverage_pct) / 100, text=f"混沌测试覆盖率: {coverage_pct:.0f}%")

    col_tested, col_untested = st.columns(2)
    with col_tested:
        st.markdown("**已覆盖服务** ✅")
        for svc in sorted(tested_services):
            svc_exps_count = sum(1 for e in experiments if e.get("service") == svc)
            svc_pass_rate = sum(
                1 for e in experiments if e.get("service") == svc and e.get("result") == "passed"
            ) / svc_exps_count if svc_exps_count else 0
            st.markdown(
                f"- **{svc}** — {svc_exps_count} 次实验, 通过率 {svc_pass_rate*100:.0f}%"
            )

    with col_untested:
        st.markdown("**未覆盖服务** ⚠️")
        if untested:
            for row in untested:
                priority = row.get("priority", "")
                priority_badge = (
                    "🔴 Tier0" if priority == "Tier0" else
                    "🟠 Tier1" if priority == "Tier1" else
                    "🟡 Tier2"
                )
                st.markdown(f"- **{row.get('service', '?')}** {priority_badge}")
        elif untested_set:
            for svc in sorted(untested_set):
                st.markdown(f"- **{svc}**")
        else:
            st.success("所有已知服务均已覆盖！")

with tab_insights:
    st.subheader("韧性洞察")

    if not experiments:
        st.info("暂无实验数据，无法生成洞察。")
    else:
        import pandas as pd

        df_all = pd.DataFrame(experiments)

        # 故障类型分布
        if "fault_type" in df_all.columns:
            st.markdown("**故障类型分布**")
            fault_counts = df_all["fault_type"].value_counts()
            st.bar_chart(fault_counts)

        # 各服务通过率
        if "service" in df_all.columns and "result" in df_all.columns:
            st.markdown("**各服务通过率**")
            svc_stats = (
                df_all.groupby("service")["result"]
                .apply(lambda x: (x == "passed").sum() / len(x) * 100)
                .round(1)
                .reset_index()
                .rename(columns={"result": "通过率 (%)"})
            )
            st.bar_chart(svc_stats.set_index("service"))

        # 恢复时长分布
        if "recovery_time" in df_all.columns:
            st.markdown("**恢复时长分布 (秒)**")
            recovery_df = df_all[["service", "recovery_time"]].dropna()
            if not recovery_df.empty:
                recovery_df["recovery_time"] = pd.to_numeric(
                    recovery_df["recovery_time"], errors="coerce"
                ).fillna(0)
                avg_by_svc = (
                    recovery_df.groupby("service")["recovery_time"]
                    .mean()
                    .round(1)
                    .reset_index()
                    .rename(columns={"recovery_time": "平均恢复(s)"})
                )
                st.bar_chart(avg_by_svc.set_index("service"))
