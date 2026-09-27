# 443 监听器规则清单 —— Cognito 收编前的只读盘点

**日期**：2026-09-27 ｜ 方式：`aws elbv2 describe-rules`，**未做任何改动**

这份清单是「把手工 Cognito 规则收进 CDK」那件事的前置输入。刻意先只读产出它，
因为真正有风险的一步（`cdk import`）必须照着一份逐字准确的现状来写，
而不是凭记忆或凭 CDK 里现有的声明。

> ⚠️ 本文档**不含** `X-Demo-Bypass` 的令牌值。那是一个静态共享密钥，
> 作用等同于 bearer token（见下面的发现 ①），写进仓库等于再复制一份。
> 需要它时从 `aws elbv2 describe-rules` 现取。

---

## 发现 ① ⚠️ `X-Demo-Bypass` 不是「演示便利」，是整个 ALB 的认证旁路

`Servic-PetSi-by0kpyBtxswj` 的 443 监听器上，三条规则带这个头，
而**其中一条没有任何路径条件**：

```
prio 1    path /streamlit,/streamlit/*  +  header X-Demo-Bypass=<48 位静态值>  → forward
prio 2    header X-Demo-Bypass          +  path /graph,/graph/*                → forward
prio 3    header X-Demo-Bypass                                                 → forward → PetSite 目标组
          ↑ 只有这个头，没有路径条件

prio 10   path /graph,/graph/*   → cognito + forward
prio 20   path /streamlit,…      → cognito + forward
prio 4    host temporal.rainmeadows.com → cognito + forward
default                          → cognito + forward
```

**ALB 按优先级数字从小到大匹配**，所以 `prio 3` 排在 10 / 20 / default **之前**。
含义是：

> 任何人只要知道那个 48 位十六进制字符串，就能**不经认证访问这个 ALB 后面的全部内容** ——
> 包括 `/graph`（Neptune 图数据库 UI）与 `/streamlit`。

prio 1 与 prio 2 在 prio 3 存在的前提下是**冗余**的：没有路径条件的那条已经覆盖一切。

这与「任何公网入口都要有认证」这条既定规则直接冲突。它不是我建的，
所以**处置需要它的主人拍板**，但盘点必须把性质写清楚：
这不是一条便利规则，是一把万能钥匙，而且钥匙是静态的、不会过期、无法按人吊销。

三个可选处置（按代价从低到高）：

1. **给 prio 3 补上路径条件**，把旁路范围缩回它实际需要的那一个路径。
   改动最小，立刻把「整站可绕」降为「一个路径可绕」。
2. **换成按来源收窄**：旁路规则加 `source-ip` 条件，限定到已知的演示出口。
3. **删掉旁路，改用 Cognito 的机器身份**（client credentials）。最干净，但要改调用方。

---

## 发现 ② API 返回的 Actions **不按 Order 排序** —— 会让正确的规则看起来是坏的

`prio 20` 的 Actions 在 API 返回里是 `forward` 在前、`authenticate-cognito` 在后。
按列表顺序读会得出「认证在转发之后，等于没认证」这个**错误结论**。

实际的 `Order` 字段是：

```
prio 20:   Order=2 forward     Order=1 authenticate-cognito     ← 认证确实在前
prio 10:   Order=1 authenticate-cognito   Order=2 forward
```

**判据是 `Order` 字段，不是数组下标。** 我一开始按下标读，差点报一个不存在的安全问题，
核实了 `Order` 才没有误报。任何自动化盘点脚本都必须先按 `Order` 排序。

---

## 现状 vs CDK 的逐条比对

443 监听器本身**在 CDK 里**（`PetAdoptions/cdk/pet_stack/lib/services-eks.ts`
的 `alb.addListener('HttpsListenerV2', { port: 443, certificates: [...] })`）。

而**规则**的情况：

| 线上规则 | CDK 里有吗 | 差异 |
|---|---|---|
| default → cognito + forward | 监听器有，**cognito 没有** | 认证完全不在 IaC 里 |
| prio 1 `/streamlit*` + bypass → forward | **没有** | 旁路 |
| prio 2 `/graph*` + bypass → forward | **没有** | 旁路 |
| prio 3 bypass（无路径）→ forward | **没有** | 旁路，见发现 ① |
| prio 4 host `temporal.rainmeadows.com` → cognito + forward | **没有** | |
| prio 10 `/graph*` → cognito + forward | CDK 声明了 neptune-ui 目标组的规则，但**路径是 `/neptune-ui`、`/neptune-api`**，且无 cognito | **路径与线上不一致** |
| prio 20 `/streamlit*` → cognito + forward | CDK 声明了 streamlit 规则，**无 cognito** | |

**整个 CDK 里 `cognito` 只出现 1 次，且是注释。** 也就是说这个栈的认证
100% 活在 CloudFormation 之外。

⚠️ 另一个此前没人提的landmine：**CDK 声明的 neptune 路径与线上不同**
（`/neptune-ui` vs 线上 `/graph`）。所以即使不动认证，一次 `cdk deploy` 也会
按 CDK 里那份路径去建规则 —— 线上访问 `/graph` 的入口不会因此消失，
但会多出一组指向同一目标组的 `/neptune-ui` 规则，且优先级由 CDK 决定。
**收编之前必须先把这个不一致解决掉，否则导入后的 `cdk diff` 永远不会是空的。**

---

## openclaw-alb-v2（agent / Temporal 侧，我 2026-08-29 之后陆续建的）

```
default   → cognito(client=6epgihsh…, sessionTimeout=604800) + forward → openclaw-tg-v2
prio 3    path /openclaw,/openclaw/*                    → cognito + forward → openclaw-tg-v2
prio 4    host code.rainmeadows.com + path /openclaw    → redirect 301 → /openclaw/
prio 5    host code.rainmeadows.com + path /openclaw*   → cognito + forward → openclaw-tg-v2
prio 6    path /kronos,/kronos/*   → cognito(client=1c365c7l…, sessionTimeout=604800) + forward → kronos-webui-tg
prio 10   host code.rainmeadows.com → cognito + forward → code-server-tg
```

**这个 ALB 整体不在任何 IaC 里** —— 连 ALB、监听器、目标组都不是。
它与 PetSite 那个栈是两件独立的事，不要混在一次收编里做。

注意它用了**三个不同的 Cognito client**（`6epgihsh…` / `1c365c7l…`，
以及 PetSite ALB 上的 `3g9nsp90…`），同一个 user pool `ap-northeast-1_uEVoS9ocy`。
`sessionTimeout` 也不统一（86400 与 604800 混用）。收编时要按现状逐条保留，
**不要"顺手统一"** —— 会话时长是安全参数，统一它是一个需要单独决定的改动。

---

## 收编的可行路径（本文档不执行）

CFN **不能接管已存在的资源**，所以「在 CDK 里写上规则然后 deploy」
会在创建 `AWS::ElasticLoadBalancingV2::ListenerRule` 时失败 ——
这与 2026-09-26 在 API Gateway 上踩到的是同一个坑（见 4.40 ②）。

可行路径是 `cdk import`：

1. 先解决 neptune 路径不一致（上表最后一行）
2. 在 CDK 里写出与线上**逐字一致**的规则，含 Cognito 的全部参数
   （user pool ARN、client、domain、scope、onUnauthenticated、sessionTimeout）
3. `cdk import` 把现有规则收进栈（不删除、不新建，因此**没有认证短暂关闭的窗口**）
4. **要求 `cdk diff` 为空**才算成功，再谈 `cdk deploy`

⚠️ 即使导入成功，`cdk deploy ServicesEks2` 仍会重建全部 6 个服务的 asset 镜像，
并把 2026-09-26 那三次 `set image` 覆盖回 asset。那**不是退步**
（源码已含全部修复），但它是一次大操作，不该顺手做。

**优先级是语义的一部分**：写错优先级 = 认证被旁路绕过，而表现是「一切正常」。
2026-09-26 已经踩过一次优先级抢占（PR #35）。
