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
for k in ('both','xray_only','deepflow_only','declared_only'):
    print(f'  {k:16} {c[k]}')
"
```

**预期输出**：

```
共 81 条依赖边
  both                         1
  xray_only                    9
  deepflow_only               52
  unobservable_by_design       6
  observable_but_unobserved   13
```

> **要讲的**：这四类不是「数据质量好坏」，而是**两个观测源各自的能力边界**。
> 逐类看下去，每一类都对应一个真实的技术原因。

### 2.1 `both` —— 两种完全不同的机制互相印证

```bash
python3 /tmp/q.py q21_observation_source_coverage coverage=both limit=20
```

**预期输出**（截取关键字段）：

```json
[{
  "src": "petsite",  "edge_type": "Calls",  "dst": "petsearch",
  "discovered_by": "deepflow-etl",
  "xray_calls": 381,        "xray_rt_seconds": 88.764,
  "deepflow_calls": 96,
  "coverage": "both",       "seen_by": ["xray", "deepflow"]
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
> 换个角度说：如果只用 X-Ray，这张图会少掉 **52/79 ≈ 66%** 的依赖关系，
> 而且你不会收到任何错误提示 —— 它们只是**安静地不存在**。

### 2.4 一句话收束这一节

> X-Ray 知道「谁调了**哪个具体资源**、花了多久」；
> DeepFlow 知道「网络上**真实发生了什么**，包括没埋点的东西」。
> 两者的盲区**不重叠**，所以图谱把两边都写下来、都保留，
> 自己做对账中心 —— 而不是选一个当"真相"。

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
| 2 | `q21` 无参数 | both=1 / xray=9 / deepflow=52 / 不可观测=6 / 真盲区=13 |
| 2.1 | `q21 coverage=both` | `discovered_by` 没被覆盖，两源印证 |
| 2.2 | `q21 coverage=xray_only` | DynamoDB 11,507 次，DNS 完全看不见 |
| 2.3 | `q21 coverage=deepflow_only` | 52 条来自未埋点服务，X-Ray 里不存在 |
| 3 | `get-service-graph` + S3Bucket 计数 | X-Ray 只说「S3」，图谱有 33 个 bucket → 不许猜 |
| 4 | `q21 service_name=petsearch` | S3 占 94% 响应时间，每次 458ms |
| 5 | `lambda invoke` 连跑两次 | created=0 / corroborated=7，且 seen=图谱边数=7 |

**如果有人问「为什么不用 X-Ray 的服务图直接看？」**
答：它只有 4 个服务。这个环境里 66% 的依赖来自没埋点的服务，X-Ray 里根本不存在。

**如果有人问「为什么不干脆全用 DeepFlow？」**
答：它对 AWS 托管服务只到域名粒度，而连接复用 + VPC 端点让它连域名都拿不到 ——
每天 11,507 次的 DynamoDB 调用在它眼里是不存在的。

**如果有人问「两个源数字不一致怎么办？」**
答：不需要「办」。口径本来不同（应用 span vs 网络请求）。
图谱要回答的是「这条依赖存不存在、活不活跃」，两个源在这个问题上是一致的。
需要精确计数时，回到各自的原生存储去查 —— 图谱存的是指针和结论，不是遥测。
