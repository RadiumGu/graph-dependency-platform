# Demo 脚本：图谱作为多观测源的对账中心

**时长** 12–15 分钟 · **实测时间** 2026-08-29 08:05 UTC · **账号** 926093770964 / ap-northeast-1

本文里每一个「预期输出」都是**当时实跑出来的真实结果**，不是示意。
数字会随流量漂移（调用次数、响应时间每轮都在变），**结构和量级不会**。
讲的时候按结构讲，别念死数字。

---

## 0. 开场前的准备（讲之前先跑一次，30 秒）

```bash
cd /home/ec2-user/works/graph-dependency-platform
export NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com
export NEPTUNE_HOST=$NEPTUNE_ENDPOINT NEPTUNE_PORT=8182
export REGION=ap-northeast-1 AWS_DEFAULT_REGION=ap-northeast-1
```

准备一个跑查询的小包装（后面每步都用它）：

```bash
cat > /tmp/q.py <<'PY'
import sys, os, json
root = '/home/ec2-user/works/graph-dependency-platform'
for p in (root, root+'/rca', root+'/rca/neptune'):
    sys.path.insert(0, p)
import warnings; warnings.filterwarnings('ignore')
from neptune.query_catalog import run_query
name = sys.argv[1]
kw = dict(a.split('=', 1) for a in sys.argv[2:])
for k, v in list(kw.items()):
    if v.isdigit(): kw[k] = int(v)
print(json.dumps(run_query(name, **kw), ensure_ascii=False, indent=2))
PY
```

确认调度在跑：

```bash
aws events describe-rule --region ap-northeast-1 --name neptune-etl-xray-hourly \
  --query '{Schedule:ScheduleExpression,State:State}' --output json
```

```json
{ "Schedule": "rate(1 hour)", "State": "ENABLED" }
```

---

## 1. 铺垫：为什么不能把遥测数据塞进图数据库（2 分钟）

**先讲这个,否则后面所有设计选择听起来都像过度设计。**

```bash
curl -s http://11.0.2.30:8123 --data-binary "
SELECT count() AS rows_per_hour FROM flow_log.l7_flow_log WHERE time > now() - 3600
FORMAT TSVWithNames"
```

预期量级：

```
rows_per_hour
789055
```

对比图谱规模：

```bash
python3.11 -c "
import sys; sys.path.insert(0,'/home/ec2-user/works/graph-dependency-platform')
sys.path.insert(0,'/home/ec2-user/works/graph-dependency-platform/rca')
import warnings; warnings.filterwarnings('ignore')
from neptune import neptune_client as nc
print('节点', nc.results('MATCH (n) RETURN count(n) AS n',{})[0]['n'])
print('边  ', nc.results('MATCH ()-[r]->() RETURN count(r) AS n',{})[0]['n'])
"
```

```
节点 891
边   1476
```

> **要讲的一句话**：单张 L7 表**一小时**的行数，是整个图谱的 **900 多倍**。
> 所以我们的目标不是「把四个支柱的数据都存进图谱」——那会把 Neptune 变成一个
> 很差的时序库。目标是「**四个支柱的数据都能从图谱一跳可达**」：
> 图谱存指针、聚合结论、派生拓扑；原始遥测留在各自的原生存储里。

---

## 2. 核心：同一套依赖，两个互不重叠的观测源（4 分钟）

这是整个 demo 的主戏。

```bash
python3 /tmp/q.py q21_observation_source_coverage limit=100 \
  | python3 -c "
import json,sys
from collections import Counter
rows=json.load(sys.stdin)
c=Counter(r['coverage'] for r in rows)
print(f'共 {len(rows)} 条依赖边')
for k in ('triple_corroborated','double_corroborated','xray_only',
          'deepflow_only','nfm_only','unobservable_by_design',
          'observable_but_unobserved'):
    print(f'  {k:16} {c[k]}')
"
```

**预期输出**：

```
共 94 条依赖边
  triple_corroborated          2
  double_corroborated          4
  xray_only                   20
  deepflow_only               51
  nfm_only                     1
  unobservable_by_design       6
  observable_but_unobserved   10
```

> **要讲的**：这七类不是「数据质量好坏」，而是**三个观测源各自的能力边界**。
> 逐类看下去，每一类都对应一个真实的技术原因。
>
> **注意分类名变过一次**：原来只有 X-Ray / DeepFlow 两源时最高一档叫 `both`。
> 引入 NFM 后该命名不再成立，改为按观测源数量分级
> （`triple_corroborated` / `double_corroborated`，另有 `observer_count` 字段）。
> 这次改名不是美化 —— 它修掉了一次**真实误报**：NFM 独家观测到的
> `petsearch → dynamodb` 因为 coverage 判定不认识 NFM，被归入了「真盲区」。
> 一条正在被观测的边被报成没人看见。同一个错误随后又犯了一次（L4 通道）。
> 教训值得当场讲出来：**每加一个写入通道，必须同步改对账口径，
> 否则新源写进去的数据在报告里等于不存在。**

### 2.1 `triple_corroborated` —— 三种完全不同的机制互相印证

```bash
python3 /tmp/q.py q21_observation_source_coverage coverage=triple_corroborated limit=20
```

**预期输出**（截取关键字段）：

```json
[{
  "src": "petsite",  "edge_type": "Calls",  "dst": "petsearch",
  "discovered_by": "deepflow-etl",
  "xray_calls": 381,        "xray_rt_seconds": 88.764,
  "deepflow_calls": 96,
  "nfm_bytes": 26868143.0,  "nfm_cross_az": true,
  "coverage": "triple_corroborated",
  "seen_by": ["xray", "deepflow", "nfm"],  "observer_count": 3
}]
```

> **要讲的**：
> - 这条边是 **DeepFlow 先发现的**（`discovered_by=deepflow-etl`），
>   X-Ray 后来独立印证。**`discovered_by` 没有被覆盖** —— 这是有意的设计：
>   原 `source` 记录「谁首先发现了这条依赖」，第二个源来了只补自己的度量。
> - 两个数字不一样（381 vs 96）**是正常的,不是 bug**：X-Ray 数的是应用埋点看到的
>   span，DeepFlow 数的是网络上的 L7 请求，统计窗口和口径都不同。
>   **两个源都指向"这条依赖存在且活跃"** —— 这才是要看的结论。
> - 一条边被两种完全不同的机制（应用埋点 / 内核 eBPF）同时看到，
>   是这条依赖可信度最高的证据。

### 2.2 `xray_only` —— DeepFlow 结构性看不见的部分

```bash
python3 /tmp/q.py q21_observation_source_coverage coverage=xray_only limit=20 \
  | python3 -c "
import json,sys
for r in json.load(sys.stdin):
    g=r.get('dst_granularity') or 'resource'
    print(f\"{r['src']:11} -> {r['dst'][:46]:46} [{r['dst_type']}/{g}]\")
    print(f\"   calls={r['xray_calls']:6}  rt={r['xray_rt_seconds']:9}s  discovered_by={r['discovered_by']}\")
"
```

**预期输出**：

```
petsearch   -> ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBS... [DynamoDBTable/resource]
   calls= 11507  rt=   70.309s  discovered_by=aws-etl
petsearch   -> s3                                             [AWSServiceEndpoint/service]
   calls=  2924  rt= 1339.728s  discovered_by=xray
petsite     -> ssm                                            [AWSServiceEndpoint/service]
   calls=   381  rt=    8.941s  discovered_by=xray
petsearch   -> sts                                            [AWSServiceEndpoint/service]
   calls=    85  rt=    9.454s  discovered_by=xray
petsearch   -> ssm                                            [AWSServiceEndpoint/service]
   calls=    12  rt=    4.421s  discovered_by=xray
petsite     -> sts                                            [AWSServiceEndpoint/service]
   calls=     1  rt=    0.062s  discovered_by=xray
```

> **旁注（有人问起再讲）**：X-Ray 会把同一条依赖用两种身份报两次 ——
> 一次按插桩服务名（`PetSearch`），一次按主机名
> （`search-service.petadoptions.svc.cluster.local`，`Type=remote`）。
> 图谱里那个服务叫 `petsearch`，K8s 部署叫 `search-service`，
> 所以 ETL 会剥掉集群 FQDN 后缀再过一遍 `service_mappings.json` 的 `k8s_alias`，
> 两种身份归一成**同一条边**。不做这一步的话，第二条边永远写不进图谱。

> **要讲的（这是全场最有说服力的一条）**：
> 第一行 `petsearch → DynamoDB 表`，**11,507 次调用**，而
> **DeepFlow 完全看不见它**。原因不是 DeepFlow 有 bug：
> AWS SDK 在启动时解析一次域名，之后**复用连接**；走 VPC 端点更是
> 根本不产生公网 DNS 查询。所以一个每天被调用一万多次的依赖，
> 在 DNS 观测窗口里可以彻底隐形。
>
> 这不是假设 —— 引入 X-Ray 之前，26 条带漂移判定的边里有 **22 条（85%）**
> 被判成「声明了但没观测到」，其中就包括这一条。那是**假阴性**，不是真漂移。
> 一个只有单一观测源的图谱，会自信地告诉你一条每天上万次的依赖已经死了。

### 2.3 `deepflow_only` —— X-Ray 结构性看不见的部分

```bash
python3 /tmp/q.py q21_observation_source_coverage coverage=deepflow_only limit=8 \
  | python3 -c "
import json,sys
for r in json.load(sys.stdin):
    print(f\"{r['src']:16} -[{r['edge_type']:6}]-> {r['dst'][:38]}\")
"
```

**预期输出**（前 8 条，共 52 条）：

```
gateway-service  -[Calls ]-> product-service
order-service    -[Calls ]-> petsite
artillery        -[Calls ]-> gateway-service
gateway-service  -[Calls ]-> order-service
gateway-service  -[Calls ]-> petsite
artillery-write  -[Calls ]-> gateway-service
order-service    -[Calls ]-> points-service
trafficgenerator -[Calls ]-> petsite
```

> **要讲的**：这 52 条边里的服务 —— `gateway-service`、`order-service`、
> `product-service`、`points-service` —— **在 X-Ray 里一个都不存在**。
> 因为 X-Ray 的前提是应用必须埋点，而这些服务没有。
> DeepFlow 走 eBPF，**零埋点**，所以它们逃不掉。
>
> 换个角度说：如果只用 X-Ray，这张图会少掉 **51/94 ≈ 54%** 的依赖关系，
> 而且你不会收到任何错误提示 —— 它们只是**安静地不存在**。
>
> （这个比例从 66% 降到 54%，是因为给 10 个 Lambda 开了 X-Ray 追踪、
>   而且压测流量恢复后 X-Ray 服务图从 4 个服务涨到 57 个。
>   **观测覆盖面依赖真实流量** —— 没有流量的链路，任何埋点都是哑的。）

### 2.4 一句话收束这一节

> X-Ray 知道「谁调了**哪个具体资源**、花了多久」；
> DeepFlow 知道「网络上**真实发生了什么**，包括没埋点的东西」；
> NFM 知道「这条流**走了哪条 ENI 路径、跨不跨 AZ**」。
> 三者的盲区**互不重叠**，所以图谱把三边都写下来、都保留，
> 自己做对账中心 —— 而不是选一个当"真相"。

---

## 2.5 全场最有说服力的一段：「盲区」这个判定本身要被质疑（4 分钟）

这一段建议**留足时间**，它是整个 demo 里唯一能证明「对账中心」不是包装词的部分。

图谱长期把这 3 条报成 `observable_but_unobserved`（真盲区）：

```
petlistadoptions -[AccessesData]-> serviceseks2-databaseb269d8bb-...  [RDSCluster]
pethistory       -[AccessesData]-> serviceseks2-databaseb269d8bb-...  [RDSCluster]
payforadoption   -[AccessesData]-> serviceseks2-databaseb269d8bb-...  [RDSCluster]
```

按字面解读，结论会是「需要再加一个观测源」。**实测证明这个结论是错的**：

```sql
SELECT toString(ip4_0) AS client, server_port, pod_group_id_0, count() AS n,
       sum(byte_tx+byte_rx) AS bytes
FROM flow_log.l4_flow_log
WHERE time > now() - INTERVAL 30 MINUTE AND toString(ip4_1)='11.0.2.135'
GROUP BY client, server_port, pod_group_id_0
```

```
11.0.2.105  5432  51  59  783490      11.0.3.236  5432  51  58  694834
11.0.2.222  5432  48  58  890040      11.0.3.50   5432  48  36  699672
11.0.2.45   5432  49  58 1620988      11.0.3.177  5432  49  24  678430
```

`11.0.2.135` 是 Aurora writer 的**私有 IP**。查 DeepFlow 自己的资源表：

```sql
SELECT id, name FROM flow_tag.pod_group_map WHERE id IN (48,49,51)
→ 48 = pay-for-adoption
  49 = list-adoptions
  51 = pethistory-deployment
```

**正好就是那 3 条「盲区」边的源。** 30 分钟 293 条流、5.4 MB。

> **要讲的**（这是全场的转折点）：
> eBPF **一直看得见**。问题在于原有的 ETL 只在「两端都能从 Pod IP 表查到」时才建边
> —— 而 Aurora 的 IP 不是 Pod IP，整批流在 `continue` 处被丢掉。
>
> 所以「盲区」这三个字下面藏着**三种性质完全不同**的东西，
> 混在一起会把人引向完全错误的修复方向：
>
> | 真实性质 | 该做什么 | 本环境实例 |
> |---|---|---|
> | **真没人看见** | 加观测源 / 加埋点 | DynamoDB 表级依赖（网络侧物理不可能） |
> | **看见了没写进去** | 修 ETL，**不需要任何新观测源** | 微服务 → Aurora 这 3 条 |
> | **声明了但从未实现** | 修声明或删依赖，**观测永远不会有** | `petstatusupdater → SQS`（见下） |
>
> 第二类最危险：它长得和第一类**一模一样**，会让人去买/部署一个根本不缺的观测源。

### 修法：按 endpoint 解析出的 IP 反查，不靠猜

图谱里 `RDSCluster` / `RDSInstance` / `NeptuneCluster` 节点都带 `endpoint` 属性。
ETL Lambda 在 VPC 内，`getaddrinfo(endpoint)` 拿到的就是**私有 IP**。
DNS 是一次**事实查询**，而截断主机名去猜集群名是**推断** —— 这条纪律贯穿全仓库。

中间踩到一个值得讲的坑（**粒度错配的第 5 例**）：
Aurora 的 endpoint 解析出的是 **writer 实例**的 IP，第一版只建了指向 `RDSInstance`
的边，于是**盲区没被印证、旁边多了 3 条平行边**。
解法不是二选一 —— `RDSInstance -[BelongsTo]-> RDSCluster` 是**图谱里已有的事实**
（aws-etl 从 RDS API 写入），沿它把同一次观测同时记在集群上，
集群级边标注 `l4_via_instance`，**不假装直接观测到了集群**。

### 第三类的现场实例：声明了但从未实现

```
petstatusupdater -[DependsOn]-> ServicesEks2-sqspetadoption2E8B1217-...  [SQSQueue]
petstatusupdater -[DependsOn]-> ServicesEks2-sqspetadoptiondlqEEEFF2AC-... [SQSQueue]
```

实测（压测流量恢复后才看得出来）：

```
SQS  NumberOfMessagesSent      409        ← 有生产者（petsite，X-Ray 实测 ok=246）
     NumberOfMessagesReceived    0        ← 14 天逐日全为 0
aws lambda list-event-source-mappings ... → []        ← 全账号 41 个 Lambda，无一挂在此队列
CFN 模板里没有任何 AWS::Lambda::EventSourceMapping，也没有任何 sqs: 动作
```

> **要讲的**：**消费者从未部署。** 队列是架构占位 —— 创建出来、URL 发布到 SSM 供
> 生产者发现，但没有任何东西被接上来排空它。领养业务真实落库走的是
> petsite → `pay-for-adoption` 的 **HTTP 同步路径**，SQS 是一条**并行的孤儿路径**。
>
> 这条边永远不会被任何观测源印证，**因为运行时确实不存在**。
> 把它算进「盲区」等于要求观测系统去看一件没发生的事。
>
> 顺带一个运维要点：现有告警只盯 **DLQ**，主队列积压**无告警**，
> 所以这个积压会一路涨到 4 天保留期然后**静默过期**，没人会发现。

---

## 3. 一个诚实性设计：粒度差异不许被抹平（2 分钟）

回看 2.2 的输出里两种 `dst_type`：

| 目标 | 节点类型 | 粒度 |
|---|---|---|
| `ServicesEks2-ddbpetadoption7B7CFEC9-...` | `DynamoDBTable` | **resource** |
| `s3` / `ssm` / `sts` | `AWSServiceEndpoint` | **service** |

看 X-Ray 原始上报：

```bash
END=$(date -u +%s); START=$((END-21600))
aws xray get-service-graph --region ap-northeast-1 --start-time $START --end-time $END \
  --query 'Services[?Type!=null && Type!=`client`].[Type,Name]' --output text
```

**预期输出**：

```
AWS::DynamoDB::Table    ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM
AWS::S3                 S3
AWS::SSM                SSM
AWS::STS                STS
AWS::SimpleSystemsManagement    SimpleSystemsManagement
```

再看图谱里有多少个 bucket：

```bash
python3.11 -c "
import sys; sys.path.insert(0,'/home/ec2-user/works/graph-dependency-platform')
sys.path.insert(0,'/home/ec2-user/works/graph-dependency-platform/rca')
import warnings; warnings.filterwarnings('ignore')
from neptune import neptune_client as nc
print('S3Bucket 节点数:', nc.results('MATCH (n:S3Bucket) RETURN count(n) AS n',{})[0]['n'])
"
```

```
S3Bucket 节点数: 33
```

> **要讲的**：DynamoDB 那条，X-Ray 给的是**完整表名**，逐字符命中图谱里
> 已有的节点 —— 所以它是**资源级精确边**。
>
> 但 S3 那条，X-Ray 报的是一个**字面叫 `S3` 的节点，没有 bucket 名**。
> 图谱里有 **33 个 bucket**。我们**可以**猜（"petsearch 在静态声明里只连了一个
> bucket，那就是它"）—— 但那是**推断，不是观测**。
> 用观测源的名义写推断结果，就是编造。
>
> 所以我们另立了一个节点类型 `AWSServiceEndpoint`，`granularity='service'`，
> 把「只知道调了 S3，不知道哪个 bucket」这件事**如实记下来**。
>
> **这个诚实反而变成了能力**：同一个依赖，`aws-etl` 给你资源级（哪个 bucket）、
> X-Ray 给你服务级（调了几次、多久）—— 两个粒度合起来才是完整答案。

### 顺带展示别名归一

```bash
python3.11 -c "
import sys; sys.path.insert(0,'/home/ec2-user/works/graph-dependency-platform')
sys.path.insert(0,'/home/ec2-user/works/graph-dependency-platform/rca')
import warnings; warnings.filterwarnings('ignore')
from neptune import neptune_client as nc
for r in nc.results('MATCH (n:AWSServiceEndpoint) RETURN n.name AS name, n.xray_type AS t, n.xray_aliases AS a ORDER BY name',{}):
    print(f\"{r['name']:16} type={r['t']:46} aliases={r['a']}\")
"
```

**预期输出**：

```
s3               type=AWS::S3                                   aliases=S3
secretsmanager   type=AWS::Unknown                              aliases=Secrets Manager
ssm              type=AWS::SSM; AWS::SimpleSystemsManagement    aliases=SSM; SimpleSystemsManagement
sts              type=AWS::STS                                  aliases=STS
```

> **要讲的**：`ssm` 那行 —— X-Ray 因 SDK 版本差异，把**同一个 AWS 服务**
> 报成 `SSM` 和 `SimpleSystemsManagement` 两个名字。我们归一到一个节点，
> 但**把两个原始名和两个 type 都留着**（存集合而不是取一个）。
> 取一个的话，留下哪个取决于遍历顺序 —— 那是不可复现的。

---

## 4. 只有 X-Ray 能回答的问题：慢在哪个下游（2 分钟）

```bash
python3 /tmp/q.py q21_observation_source_coverage service_name=petsearch limit=20 \
  | python3 -c "
import json,sys
rows=[r for r in json.load(sys.stdin) if r.get('xray_rt_seconds')]
tot=sum(r['xray_rt_seconds'] for r in rows)
print(f'petsearch 下游累计响应时间 {tot:.1f}s')
for r in sorted(rows,key=lambda x:-x['xray_rt_seconds']):
    pct=100*r['xray_rt_seconds']/tot
    print(f\"  {r['dst'][:40]:40} {r['xray_rt_seconds']:9.1f}s  {pct:5.1f}%  calls={r['xray_calls']}\")
"
```

**预期输出**：

```
petsearch 下游累计响应时间 1424.0s
  s3                                          1339.7s   94.1%  calls=2924
  ServicesEks2-ddbpetadoption7B7CFEC9-3B0...     70.3s    4.9%  calls=11507
  sts                                             9.5s    0.7%  calls=85
  ssm                                             4.4s    0.3%  calls=12
```

> **要讲的**：注意这个反差 —— DynamoDB 被调了 **11,507 次**只花 70 秒，
> S3 只被调了 **2,924 次**却花了 **1,340 秒（94%）**。
> 平均下来 S3 每次约 **458 毫秒**，DynamoDB 每次约 **6 毫秒**。
>
> 「petsearch 慢」这个问题的答案是「**慢在 S3**」，而且不是因为调得多，
> 是因为**每次都慢**。
>
> DeepFlow 能给你单条网络流的延迟，但它归不到「S3 这个逻辑资源」这个层次上 ——
> 它看到的是一堆到不同 IP 的 TCP 流。**这个归因只有 X-Ray 给得出来。**

---

## 5. 数据是活的：调度与幂等（2 分钟）

**连跑两次** —— 幂等要靠「第二次」证明，不能靠第一次。
X-Ray 的 24h 窗口里随时可能滚进一条新边，首轮出现 `created=1` 是**正常的**，
拿首轮当幂等证据现场会翻车。

```bash
for i in 1 2; do
  aws lambda invoke --region ap-northeast-1 --function-name neptune-etl-from-xray \
    --cli-binary-format raw-in-base64-out --payload '{}' /tmp/out$i.json \
    --query 'StatusCode' --output text >/dev/null
  python3 -c "
import json; b=json.load(open('/tmp/out$i.json'))['body']
print(f\"第 $i 次: seen={b['xray_edges_seen']} created={b['edges_created']} \"
      f\"corroborated={b['edges_corroborated']} skipped={b['edges_skipped_no_node']} \"
      f\"failed={b['edges_write_failed']}\")"
done
```

**预期输出**：

```
第 1 次: seen=7 created=0 corroborated=7 skipped=0 failed=0
第 2 次: seen=7 created=0 corroborated=7 skipped=0 failed=0
```

再确认图谱侧的数字对得上：

```bash
python3.11 -c "
import sys,warnings; warnings.filterwarnings('ignore')
sys.path.insert(0,'/home/ec2-user/works/graph-dependency-platform')
sys.path.insert(0,'/home/ec2-user/works/graph-dependency-platform/rca')
from neptune import neptune_client as nc
print('带 xray 度量的边:', nc.results(\"MATCH ()-[r]->() WHERE r.xray_last_seen IS NOT NULL RETURN count(r) AS n\",{})[0]['n'])
print('边总数:', nc.results('MATCH ()-[r]->() RETURN count(r) AS n',{})[0]['n'])
"
```

```
带 xray 度量的边: 7
边总数: 1476
```

> **要讲的**：三个数字必须互相对得上 —— `seen=7`、`corroborated=7`、
> 图谱里带 xray 度量的边 `=7`。**重复跑边总数不变**（1476 不动）。
>
> 这一点不是"顺便提一下"：多源写同一张图时，谁都不能因为多跑一轮就把图写脏。
> 而且 `created` 是**回读确认过才计数**的 —— 我们最初的实现是"调用没抛异常
> 就算创建成功"，结果目标节点不存在时 Gremlin 静默产出空集、不报错也不写边，
> 于是每轮都谎报一次成功。三个数字对不上就是这么发现的。
> 这一点在多源写同一张图时是硬要求：
> 两个 ETL 各跑各的调度，谁都不能因为多跑一轮就把图谱写脏。
>
> `window_hours: 24` 背后有个 API 硬约束：X-Ray 的 `GetServiceGraph`
> **单次窗口上限 6 小时**（超过直接报错），所以 24 小时视图是**分 4 段合并**的。
> 调度是每小时一轮 —— X-Ray 拓扑按部署节奏变化，不按秒变。

---

## 6. 收尾：这套东西解决了什么（1 分钟）

> 一句话：**图谱不再是某一个工具的导出结果，而是多个观测源的对账中心。**
>
> 三条具体收益：
> 1. **消除单源假阴性** —— 单靠 DNS 观测时，85% 的声明依赖被误判为「已消失」，
>    其中包括每天上万次调用的 DynamoDB。
> 2. **保留发现史** —— `discovered_by` 永不被后来的源覆盖，
>    所以你随时能回答「这条依赖最初是谁发现的、后来谁印证了」。
> 3. **粒度如实** —— 观测不到具体资源就明说观测不到，绝不用推断冒充观测。
>    结果是不同源的不同粒度可以叠加，而不是互相污染。

---

## 附：一页速查（打印出来放手边）

| 步骤 | 命令 | 一句话 |
|---|---|---|
| 1 | ClickHouse 行数 vs 图谱规模 | 789k/小时 是图谱的 900 倍 → 不能存遥测 |
| 2 | `q21` 无参数 | 三源=2 / 双源=4 / xray=20 / deepflow=51 / nfm=1 / 不可观测=6 / 真盲区=10 |
| 2.1 | `q21 coverage=triple_corroborated` | `discovered_by` 没被覆盖，三源印证 |
| 2.5 | ClickHouse 查 `ip4_1='11.0.2.135'` | 盲区其实采到了没写 —— 全场转折点 |
| 2.2 | `q21 coverage=xray_only` | DynamoDB 11,507 次，DNS 完全看不见 |
| 2.3 | `q21 coverage=deepflow_only` | 52 条来自未埋点服务，X-Ray 里不存在 |
| 3 | `get-service-graph` + S3Bucket 计数 | X-Ray 只说「S3」，图谱有 33 个 bucket → 不许猜 |
| 4 | `q21 service_name=petsearch` | S3 占 94% 响应时间，每次 458ms |
| 5 | `lambda invoke` 连跑两次 | created=0 / corroborated=7，且 seen=图谱边数=7 |

**如果有人问「为什么不用 X-Ray 的服务图直接看？」**
答：这个环境里 54% 的依赖来自没埋点的服务，X-Ray 里根本不存在。
而且 X-Ray 的覆盖面**依赖真实流量** —— 压测停掉的 95 天里它只看得到 4 个服务，
流量恢复后立刻涨到 57 个。埋点开了不等于看得见。

**如果有人问「为什么不干脆全用 DeepFlow？」**
答：它对 AWS 托管服务只到域名粒度，而连接复用 + VPC 端点让它连域名都拿不到 ——
每天 11,507 次的 DynamoDB 调用在它眼里是不存在的。

**如果有人问「两个源数字不一致怎么办？」**
答：不需要「办」。口径本来不同（应用 span vs 网络请求）。
图谱要回答的是「这条依赖存不存在、活不活跃」，两个源在这个问题上是一致的。
需要精确计数时，回到各自的原生存储去查 —— 图谱存的是指针和结论，不是遥测。
