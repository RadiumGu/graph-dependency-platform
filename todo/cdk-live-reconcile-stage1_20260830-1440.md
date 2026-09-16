# CDK 与线上追平：Stage 1 勘察 —— cdk diff 跑不了，而且原因是源码缺陷

**日期**：2026-08-30 14:16–14:40 UTC
**对象**：`/home/ec2-user/works/one-observability-demo`（从 i-022fb7c32b71c72d9 经 m2m 桶搬运）
**结论**：**`cdk diff` 无法执行**，两个独立阻塞点；而且即使跑通，按现状部署会**回退**我昨天做的修复。

---

## 一、工具链：一半就绪

| 项 | 状态 |
|---|---|
| `node` / `npm` | v22.12.0 / 10.9.0 ✅ |
| `node_modules` | `npm install` 7 秒完成，465M ✅（搬运时排除，已恢复） |
| `npx cdk` | **2.1106.0** ✅（devDependencies 里的 aws-cdk ^2.204） |
| **Docker** | ❌ **未安装**，且免密 sudo 不可用 |

`cdk.json` 的 app 入口是 `npx ts-node app/pet_stack.ts`。
这个 stack 有 **6 个 `DockerImageAsset`**（petsite / petsearch-java / payforadoption-go /
petlistadoptions-go / petadoptionshistory-py / trafficgenerator），
**synth 阶段必须有 Docker 才能算出 asset hash** —— 没有替代路径
（`finch` / `podman` / `nerdctl` 均未安装，`CDK_DOCKER` 也无处可指）。

**源机 i-022fb7c32b71c72d9 有 Docker 29.7.2、daemon 正常，且有现成的 `cdk.out`。**
所以部署主机应当是源机，不是本机 —— 这改变了后续阶段的形态：
本机改代码 → 送回源机 → 在源机部署。

## 二、阻塞点二：CDK 从 2026-04-17 起就 synth 不了

```
npx cdk list
→ ENOENT: open './resources/microservices/petadoptionshistory-py/deployment.yaml'
```

`resources/microservices/` 下全是**符号链接**（`petadoptionshistory-py -> ../../../../petadoptionshistory-py/`），
链接本身完好。问题在提交 `0037ed3b`（2026-04-17）：

```
remove: delete duplicate pethistory deployment manifest
Single source of truth is k8s-manifests/04-pethistory.yaml.
This standalone copy caused a 13-day CrashLoopBackOff due to
unprocessed {{}} template vars. Removing to prevent future drift.
 .../petadoptionshistory-py/deployment.yaml | 188 ---------
```

**文件删了，但 `lib/applications.ts:109` 还在引用它**：

```ts
kubernetesManifestPath: "./resources/microservices/petadoptionshistory-py/deployment.yaml",
otelConfigMapPath:      "./resources/microservices/petadoptionshistory-py/otel-collector-config.yaml",
```

时间线对得上：`ServicesEks2` 最后更新 **2026-04-02**、`Applications` **2026-04-03**，
而这批提交全在 **04-17**。也就是说这份 CDK **4.5 个月没有被成功部署过**。

> 这个缺陷是「修一个重复源反而制造了一个死引用」—— 与本项目反复出现的
> 「同一事实有两个来源」是同一类问题，只是这次的后果是**整个 IaC 通道失效**。

## 三、逐提交核实部署状态（不靠时间戳推断）

04-01 之后共 6 个提交，全部日期 2026-04-17。逐项对线上核实：

| 提交 | 内容 | 线上核实 | 判定 |
|---|---|---|---|
| `3cf487de` | `.gitignore` | 无部署效果 | — |
| `4f31fbec` | ① 按 AZ 拆分节点组 | 线上有 `workers1a60` + `workers1cFE` | **已生效** |
| | ② reduce pod CPU → 128m | 线上 search-service `256m`（我昨天从 512m 改的） | **未部署** |
| | ③ UI ALB rules | 线上 listener 规则 10=`/graph*`、20=`/streamlit*` | **已生效** |
| `5c731c61` | ACM 证书换成 `ce750241` | 线上 :443 listener 证书正是 `ce750241` | **已生效** |
| `4f68ab37` | neptune-etl ClusterRole | 线上有 `neptune-etl-reader`（2026-04-16 创建，135 天） | **已生效**（走 kubectl 非 CDK） |
| `0037ed3b` | 删 pethistory manifest | —— | **就是它打破了 synth** |
| `2da1ea01` | 修该 manifest 的模板变量 | 被下一个提交整文件删掉 | 已作废 |

**`4f31fbec` 是部分生效的** —— 节点组拆分与 ALB 规则在线上，pod CPU 那部分不在。
说明代码曾在提交之前被部署过（这正是「不要用提交时间推断部署状态」的实例）。

## 四、三套真相源互相不一致

线上资源的归属用 CDK 的 `aws.cdk.eks/prune-*` label 判定：

| 对象 | CDK 管理 | kubectl 痕迹 | 权威源 |
|---|---|---|---|
| 6 个 Deployment | **是** | 有 | CDK，但被 kubectl 覆写过 |
| 6 个 Service | **是** | — | CDK |
| **5 个 HPA** | **否** | 有 | **`PetAdoptions/k8s-manifests/08-hpa.yaml`** |

### CPU 声明的三方对照

| 服务 | CDK (`services-eks.ts`) | `k8s-manifests/` | **线上** | 谁赢了 |
|---|---|---|---|---|
| list-adoptions | 128m/128m | 128m/**512m** | **128m/128m** | CDK |
| pay-for-adoption | 128m/128m | 128m/**512m** | **128m/128m** | CDK |
| search-service | 128m/128m | 128m/**512m** | **256m/512m** | 我昨天的运行时改动 |
| pethistory | （引用已删文件） | 50m/500m | **50m/500m** | k8s-manifests |
| **petsite** | **A 无 resources 段** | **50m/500m** | **（无 request）** | CDK 的 A |
| traffic-generator | 64m/64m | — | 64m/64m | 一致 |

两份 petsite manifest **不是同一个文件，而且差异正好就是缺失的 request**：

```
A = cdk/pet_stack/resources/k8s_petsite/deployment.yaml   79 行  ← applications.ts:84 读这份，无 resources
B = PetAdoptions/k8s-manifests/04-petsite.yaml           106 行  ← 已有 cpu 50m/500m + 一个 sidecar
```

**B 里已经写好了用户这次要的修复，但 B 从未被应用** —— 线上 petsite 无 request、
只有一个容器，与 A 完全一致。同理 `08-hpa.yaml` 五个 HPA 全声明 60%，
而线上 search-service 是 120%（我昨天改的），那个文件也落后于线上。

`k8s-manifests/` 里那 512m 的 limit 一个都没生效 —— 说明这套 YAML
只对 pethistory 与 RBAC 真正应用过，对另外三个微服务没有。

## 五、blast radius：按现状部署会**回退**修复

`cdk deploy` 目前**不可能执行**。即使修好 synth，按现状部署会做这些事：

| 影响 | 性质 |
|---|---|
| search-service `256m/512m` → **`128m/128m`** | ⚠️ **回退我昨天的右调，且 limit 从 512m 砍到 128m** |
| petsite 仍然没有 request | ⚠️ `petsite-hpa` 继续失效（`cpu: <unknown>/60%`，已 155 天） |
| pethistory 的 CDK 通道需先修死引用 | ⚠️ 修法决定 CDK 是否重新接管 pethistory —— 有重新引入 `0037ed3b` 修掉的 CrashLoopBackOff 的风险 |
| 两个节点组 `t4g.large` → `t4g.xlarge`（未提交改动） | ⚠️ **`instanceTypes` 不可原地修改**（两组都无 launch template），CloudFormation 会**替换整个节点组** |
| ACM 证书 / ALB 规则 / RBAC | 已在线上，no-op |

**search-service 的 limit 回退是实质风险**：昨天实测它在压测下瞬时打到 **520m**，
`128m` 的 limit 会把它硬节流。这一条单独就足以否决「先部署再说」。

> 这验证了方案 A 的必要性：**必须先把线上现实写进 CDK（并加 `cpuLimit` 参数让
> `128m/512m` 这种形态可表达），才能部署** —— 不能反过来。

## 六、Stage 1 的三个结论

1. **`cdk diff` 拿不到** —— 需要先修 `applications.ts` 的死引用（Stage 2 的前置），
   并在有 Docker 的主机上跑。所以「完整逐项差异」这一项本阶段以
   **人工逐项核实**（第三、四节）替代，结果更可靠：它对的是线上实际状态，
   而 `cdk diff` 对的是 CloudFormation 模板。
2. **部署主机应当是源机 i-022fb7c32b71c72d9**（有 Docker + cdk.out），
   本机只做代码修改。
3. **`eks-service.ts` 的 `request == limit` 必须先拆开**，否则线上
   `256m/512m` 在 CDK 里根本无法表达，追平在结构上做不到。

## 七、需要在 Stage 2 之前定的一件事

修 synth 死引用有三种改法，性质不同：

| 改法 | 后果 |
|---|---|
| **(a)** `git revert 0037ed3b` 恢复文件 | 重新引入 `0037ed3b` 明确要消灭的重复源，且那份文件曾导致 13 天 CrashLoopBackOff |
| **(b)** 把 `applications.ts` 指向 `../../k8s-manifests/04-pethistory.yaml` | 让 k8s-manifests 成为唯一源；需核对该文件形态是否满足 `PetAdoptionsHistory` 构造对占位符的要求 |
| **(c)** 从 CDK 里移除 pethistory 应用，交给 kubectl | 与线上现实一致（线上 pethistory 就是 k8s-manifests 的值），但 CDK 从此不再覆盖它 |

**倾向 (c)**：线上 pethistory 的 CPU 值（50m/500m）来自 k8s-manifests 而非 CDK，
说明实际管理权早已移交；把 CDK 里的死代码删掉是让声明追上现实，
而 (a) 是把现实拖回一个已知有害的旧状态，(b) 则要让 CDK 重新接管一个
它已经管不好的对象。

---

# 补记：为什么 arm64 环境里会冒出 amd64 需求（2026-08-30 15:10）

用户问了一个关键问题：「现在的环境都是 arm 的，为什么还需要 x86 的环境去构建？」
**答案是不需要，而且用 x86 构建会坏事。**

## 实测

| 对象 | 架构 |
|---|---|
| 4 个 EKS 节点 | 全部 `arch=arm64` |
| nodegroup AMI | `AL2023_ARM_64_STANDARD` |
| list-adoptions / pay-for-adoption / search-service / traffic-generator 的 `DockerImageAsset` | **显式 `platform: Platform.LINUX_ARM64`** |
| **petsite 的 `DockerImageAsset`**（`lib/applications.ts:79`） | **未指定 platform —— 跟随构建宿主** |

## amd64 的唯一来源

是第三方 construct `cdk-ecr-deployment` **自己的辅助 Lambda**：

```dockerfile
# node_modules/cdk-ecr-deployment/lambda-src/Dockerfile
ENV GOOS=linux GOARCH=amd64      # 硬编码
RUN make OUTPUT=/asset/bootstrap  # 默认目标 all: lambda test
```

这个 Lambda 的作用是在 ECR 仓库之间复制镜像，**它跑在 Lambda 上、架构由 construct 自己决定，
与被部署的集群架构无关**。它在 arm64 宿主上失败不是因为「需要 x86 宿主」，
而是因为交叉编译出的 **amd64 测试二进制无法在 arm64 上执行**（`exec format error`）——
一个上游缺陷：`make` 不指定目标就会连 `test` 一起跑。

解法是 `make lambda` 只构建不测试（交付物是 amd64 `bootstrap`，测试是开发期检查）。
**不是去找 x86 宿主。**

## 用 x86 构建会引入的两个问题

1. 四个钉了 `LINUX_ARM64` 的镜像要走 qemu 模拟构建 —— 慢，且需要目标机装 binfmt。
2. **`petsite` 没钉 platform，在 x86 上会产出 amd64 镜像 —— 放到 t4g 节点上起不来。**

## 由此发现的一个潜在缺陷（未修，理由如下）

`petsite` 缺 `platform: Platform.LINUX_ARM64`，与其他四个服务不一致。
现在能跑只是因为构建恰好发生在 arm64 宿主上；换机器构建就会静默产出跑不起来的镜像。

**本轮刻意不补**：加上这一行会改变 asset hash → 触发 petsite 镜像重新构建与推送，
而本次部署已经要替换两个节点组。入口服务的镜像重建不该和节点组替换挤在同一个窗口 ——
出问题时无法区分是哪一项造成的。

建议与 keep-alive 的修复（也需要重建镜像）合并成一个 PR 单独做。
