# P2b · 让已有的 otel-collector sidecar 把 span 同时发给 DeepFlow

**状态：待人工放行**（需要改 `petadoptions` 命名空间里 3–4 个生产 Deployment，会触发 Pod 重建）
**日期**：2026-08-29

---

## 为什么不是 P2a（改 DeepFlow 配置提取 X-Amzn-Trace-Id）

P2a 的设想是：给 DeepFlow 的 `http_log_trace_id` 加上 `X-Amzn-Trace-Id`，
零应用改动就让 `l7_flow_log.trace_id` 从 0 变成非 0。

**这个设想被官方文档否证了。** DeepFlow 只对**三个** header 做格式感知解析
（[HTTP 协议文档](https://deepflow.io/docs/features/l7-protocols/http/)脚注 [1]）：

> TraceID only extracts **part of the value** from the following HTTP Headers,
> **other custom headers read the full value**:
> - The `trace-id` part in the `traceparent` header
> - The `trace ID` part in the `sw8`/`sw6` header
> - The `{trace-id}` part in the `uber-trace-id` header

`X-Amzn-Trace-Id` 不在其中 → **整串读取**。而它的值形如：

```
Root=1-5759e988-bd862e3fe1be46a994272793;Parent=53995c3f42cd8ad8;Sampled=1
```

`Root` 全链一致，但 **`Parent` 每跳都不同**。整串因此每跳不同，
`trace_id` 会变成非 0 但**同一条链的各跳不会归到一起** —— 拿到的是非 0 的垃圾，
比 0 更坏（0 至少诚实）。

这与本仓库反复出现的陷阱同型：**字段有值 ≠ 值有用**。
（前两次：`profile.in_process` 表存在但 0 行；`l7_flow_log` 的 span 字段齐全但全空。）

## 为什么也不是 P1（用 syscall_trace_id 缝合）

P1 的设想是用 DeepFlow 的 AutoTracing（`syscall_trace_id`，无需应用埋点）缝合请求链。
**实测否证**：

| 指标 | 值 |
|---|---|
| 全量 L7 里 `syscall_trace_id_request` 非 0 的比例 | 10.11% |
| **只看跨 Pod 业务流量**的比例 | **4.6%** |
| 跨 Pod 流量里不同的 `syscall_trace_id` | 15,799 |
| 其中**横跨多跳**的 | **57（0.36%）** |
| 单条链最大跳数 | **2** |

那 10.11% 是被两类非业务流量抬高的：`127.0.0.1 → 127.0.0.1` 上的
`/readyz` `/healthz`（kubelet 健康探针，覆盖率 58.35%）和
`169.254.170.23`（EKS Pod Identity Agent，34.21%）。

`syscall_trace_id` 是**进程内**的入向/出向关联，不是跨服务链路标识。
在 0.36% 覆盖率上建缝合没有意义。

DeepFlow 文档也印证了这一点：分布式追踪
「only supports traces initiated from data collected via eBPF or
**transmitted to DeepFlow through the OpenTelemetry protocol**」。
—— 即真正的跨服务链路要走 **OTLP 摄入**，这就是 P2b。

---

## P2b 方案

### 现状（实测）

4 个服务有 `aws-otel-collector:v0.47.0` sidecar：
`search-service`(Java) / `pay-for-adoption`(Go) / `list-adoptions`(Go) / `pethistory`(Python)。

配置**不走 ConfigMap**，而是通过 `AOT_CONFIG_CONTENT` 环境变量注入（实测取得原文）：

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
processors:
  batch/traces:
    timeout: 1s
    send_batch_size: 50
exporters:
  awsxray:
    region: ap-northeast-1
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch/traces]
      exporters: [awsxray]
```

而 DeepFlow agent **默认就在 38086 上接收 OpenTelemetry 数据**
（实测 example 配置：`external_agent_http_proxy_enabled: 1`、
`external_agent_http_proxy_port: 38086`，两项都是默认开启）。

### 改动：加一个 exporter，保留原有的

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
processors:
  batch/traces:
    timeout: 1s
    send_batch_size: 50
exporters:
  awsxray:
    region: ap-northeast-1
  otlphttp/deepflow:
    traces_endpoint: "http://${env:K8S_NODE_IP}:38086/api/v1/otel/trace"
    tls:
      insecure: true
    retry_on_failure:
      enabled: true
      max_elapsed_time: 30s
    sending_queue:
      enabled: true
      queue_size: 500
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch/traces]
      exporters: [awsxray, otlphttp/deepflow]
```

同时给 sidecar 容器补一个环境变量（DeepFlow agent 是 DaemonSet，
必须发给**本节点**那个实例，不能跨节点）：

```yaml
- name: K8S_NODE_IP
  valueFrom:
    fieldRef:
      fieldPath: status.hostIP
```

### 一条关键的安全性质

**两个 exporter 相互独立。** OTel Collector 的 exporter 各自失败、互不阻塞：
即使 DeepFlow 的 38086 完全不可达，`awsxray` 仍然照常导出。
`retry_on_failure` + `sending_queue` 会让失败的批次重试后丢弃，不会反压。

**所以现有的 X-Ray 链路不承担风险。**

### 真实风险：配置解析失败导致 sidecar 起不来

残余风险只有一个，但它是实的：`AOT_CONFIG_CONTENT` 若有语法错误，
collector 容器启动失败 → Pod 无法 Ready → **该服务不可用**。

因此必须：

1. **先离线校验配置语法**（下面给了命令）
2. **canary 先行**：只改 `pethistory`（4 个里业务权重最低，且它在 X-Ray
   服务图里本来就不出现，影响面最小），确认 Pod Ready 且 X-Ray 仍有数据后
   再推其余 3 个
3. 每次只改一个 Deployment，观察 `kubectl rollout status` 再继续

### 离线校验

```bash
# 用与生产同版本的镜像校验配置语法（不连任何后端）
docker run --rm -e AOT_CONFIG_CONTENT="$(cat p2b-collector-config.yaml)" \
  public.ecr.aws/aws-observability/aws-otel-collector:v0.47.0 \
  --config=env:AOT_CONFIG_CONTENT --dry-run 2>&1 | head -20
```

若本机无 docker，退而用 `python3 -c "import yaml,sys; yaml.safe_load(open('p2b-collector-config.yaml'))"`
至少保证 YAML 合法（但这**不能**校验 collector 的 schema，仍需 canary）。

### 应用（canary 先行）

```bash
export PATH="$HOME/bin:$PATH"
CFG=$(cat infra/k8s/p2b-collector-config.yaml)

# ① canary：只改 pethistory
kubectl -n petadoptions set env deploy/pethistory-deployment \
  -c aws-otel-collector "AOT_CONFIG_CONTENT=$CFG"
kubectl -n petadoptions patch deploy pethistory-deployment --type=json -p='[{
  "op":"add",
  "path":"/spec/template/spec/containers/1/env/-",
  "value":{"name":"K8S_NODE_IP","valueFrom":{"fieldRef":{"fieldPath":"status.hostIP"}}}
}]'
kubectl -n petadoptions rollout status deploy/pethistory-deployment --timeout=180s

# ② 验证（见下），通过后再推 search-service / pay-for-adoption / list-adoptions
```

**注意**：`/spec/template/spec/containers/1/` 里的下标 `1` 需要先确认
otel 容器的实际位置，不要照抄：

```bash
kubectl -n petadoptions get deploy pethistory-deployment \
  -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"\n"}{end}' | cat -n
```

### 验证（三条，缺一不可）

```bash
# 1. Pod 必须 Ready，且 collector 容器没有 CrashLoop
kubectl -n petadoptions get pods -l app=pethistory
kubectl -n petadoptions logs deploy/pethistory-deployment -c aws-otel-collector --tail=30

# 2. X-Ray 必须仍有数据（证明没打断原有链路）
END=$(date -u +%s); START=$((END-600))
aws xray get-trace-summaries --region ap-northeast-1 \
  --start-time $START --end-time $END --query 'length(TraceSummaries)'

# 3. DeepFlow 侧必须开始出现 trace_id（这是 P2b 的目的）
curl -s http://11.0.2.30:8123 --data-binary "
SELECT count() AS total, countIf(trace_id != '') AS with_trace,
       round(100.0*countIf(trace_id != '')/count(),3) AS pct
FROM flow_log.l7_flow_log WHERE time > now() - 600 FORMAT Vertical"
```

第 3 条的基线是 **0 / 789,055**（近 1h 实测）。canary 后应看到非 0。

### 回滚

```bash
# 恢复原配置（原文已存档在本文件上方，也可从其它未改的 Deployment 取）
kubectl -n petadoptions set env deploy/pethistory-deployment \
  -c aws-otel-collector "AOT_CONFIG_CONTENT=$(cat infra/k8s/p2b-collector-config-original.yaml)"
kubectl -n petadoptions rollout undo deploy/pethistory-deployment   # 或直接回滚上一版本
```

`rollout undo` 是更干脆的回滚，因为环境变量改动本身就产生了新的 ReplicaSet。

---

## 为什么这一步需要人工放行

- 会改 `petadoptions` 命名空间里的**生产 Deployment**，触发 Pod 重建
- 失败模式是实的：配置语法错误 → sidecar 起不来 → **服务不可用**
- 不在本轮已批准的计划范围内（原计划 Stage 6 明确写了
  「若必须改 EKS 工作负载则先报备再动」）

仓库侧的产物（配置文件、校验、应用、验证、回滚步骤）已就绪，
执行只需一条 canary 命令。
