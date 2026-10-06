# `Code.fromAsset` 打包的是目录当前内容 —— 依赖不在 git 里时等于部署了个空壳

**日期**：2026-10-04
**性质**：**我自己造成的生产变更缺陷**，在暴露前 50 分钟被查出并修复
**用途**：记录一个「所有判据都给绿灯」的缺陷形态，以及为它新建的检测维度

---

## 发生了什么

为部署 PR #47 的 active 过滤修复，我跑了 `cdk deploy AlertBufferStack`。
栈里的 `gp-window-flush` 用：

```ts
code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/rca_window_flush'))
```

CDK 打包那个目录的**当前内容**。而那个目录：

| | 数量 |
|---|---|
| 被 git 跟踪的文件 | **36** |
| 运行时实际需要的文件 | **约 2000**（`urllib3` / `strands` / `shared` / `profiles` …） |

差额由 `build.sh` 的 `pip install` 装进去，并被 `.gitignore` 排除。

我在一个**干净的工作树**上直接 deploy，于是：

```
部署前   14.6 MB / 2008 条目   含 urllib3 · shared · strands · .so
部署后   0.29 MB /   36 条目   只剩被 git 跟踪的业务文件
```

`window_flush_handler.py` 第一行就 `import urllib3`。这个包一旦被调用
就是 `ModuleNotFoundError`。

## 为什么它差点活下来：四个判据同时给了绿灯

这是本文真正要记的部分。

**① `cdk diff` 完全正常。** 它显示的是：

```
[~] AWS::Lambda::Function WindowFlushFunction
 └─ [~] Code
     └─ [~] .S3Key:  ← 变了
```

代码确实变了 —— 只是少了 14 MB。`cdk diff` 比较的是**模板**，
资产的内容差异在它眼里只是一个 hash 的变化，而 hash 本来就该变。

**② CloudWatch 没有任何信号。** 部署后的 50 分钟里这个函数零调用，
所以没有 Errors、没有 Duration 异常、没有告警。
盯「ETL 停摆」的 Invocations 告警至今不存在（已记在 §8.0 的缺口里），
而就算存在，它盯的也是「没有调用」，而不是「调用了会挂」。

**③ 这个仓库自己的漂移检测器（方向 B）给了满分。** 它逐字比对
`SOURCE_MAP` 里 gp-window-flush 的 5 个模块：

```
window_flush_handler.py      线上 ≡ 仓库  ✓
handler.py                   线上 ≡ 仓库  ✓
config.py                    线上 ≡ 仓库  ✓
core/rca_engine.py           线上 ≡ 仓库  ✓
neptune/neptune_queries.py   线上 ≡ 仓库  ✓   ← 我刚部署的修复，确实到了
```

**全部一致，而且这个结论是对的。** 业务代码逐字无误，少的是依赖。

方向 B 在结构上抓不到这个缺陷：**它比对模块的内容，而这里缺的是模块本身。**
一个缺 `urllib3` 必挂的函数，从内容比对拿到了满分。

**④ `build.sh` 自带的完整性校验没有执行。** 它有一段
「Verifying package completeness」，校验 6 项必需文件。但：

- 它校验**构建目录**，不校验线上包；
- 不跑 `build.sh` 直接 deploy —— 也就是我做的事 —— 这段校验根本不运行；
- 它的清单里**没有 `urllib3`**，恰好就是我缺的那个。
  它隐含假定「跑过 pip install 所以第三方依赖必然在」，
  而这个假定在「不跑 build.sh」的路径上不成立。

## 它是怎么被发现的

不是被任何自动判据发现的。是在一次**全量三方核对**（本地／线上／GitHub）
中，为了比较包结构而顺手打印了条目数：

```
petsite-rca-engine   13.3 MB   2356 条目
gp-window-flush       0.3 MB     36 条目   ← 与 50 分钟前实测的 14.6 MB 矛盾
```

**两个同族函数的量级差了 40 倍**，这才引起注意。
换句话说：它被发现靠的是我**刚好记得**部署前的数字。

## 修复

1. 跑 `build.sh` 装依赖（就地构建，46 MB）
2. 恢复它删掉的两个被跟踪文件 —— `build.sh` 的 `find -delete` 会删掉
   `81d243bd…__mypyc…so`，并改写 `bin/normalizer`。
   **这一点 build.sh 自己的注释早就预言过**（「2026-09-20 实测污染 80 项，
   还删掉了一个 .so」），我是照着那段注释去检查才发现的。
3. 重新 `cdk deploy` → 包回到 14.83 MB / 2008 条目
4. **实际调用验证**：`StatusCode 200`，`{"groups_processed": 0}` ——
   不是 import 失败
5. `git clean -fdx` 清掉构建残留

第 5 步也踩了一次：先用的是 `git clean -fd`，**它不删 `.gitignore`
排除的文件**，于是 381 个依赖文件留在目录里，而 `git status` 显示
「0 项改动」。后果是 `test_53` 的 `test_m06`（部署包里不得有从 handler
不可达的模块）开始失败，报 `certifi.core` / `charset_normalizer.api`
是死代码 —— 它扫的是**实际目录**，不是 git 索引。
CI 上同一个测试是 success，因为那边是干净 clone。

## 新建的检测维度：方向 C（包完整性）

`crons/lambda_drift.py` 加了 `PACKAGE_REQUIRED`，判据两条，都来自实测：

| 判据 | 抓什么 |
|---|---|
| 条目数下限 | 数量级丢失（36 vs 2008 这种） |
| 必需顶层项 | 指名道姓说缺哪个包，比「包变小了」可处置 |

```python
PACKAGE_REQUIRED = {
    "neptune-etl-from-cfn":        (120,  ("requests", "urllib3", "certifi", "yaml")),
    "neptune-etl-from-deepflow":   (100,  ("requests", "urllib3", "certifi")),
    "neptune-etl-from-aws":        (110,  ("requests", "urllib3", "certifi")),
    "neptune-etl-from-appsignals": (30,   ("yaml", "shared", "profiles")),
    "gp-window-flush":             (1500, ("urllib3", "shared", "strands", "profiles", "neptune")),
    "petsite-rca-engine":          (1500, ("urllib3", "shared", "strands", "profiles", "neptune")),
}
```

三个刻意的设计：

- **下限留 ~25% 余量**，不贴着实测值。依赖版本升级会让条目数小幅波动，
  而这个检测要抓的是数量级丢失。一个会周期性误报的门禁很快会被忽略 ——
  本仓 4.47 已经为此移除过队列积压告警。
- **没有依赖的函数（trigger / xray / agentcore 都是单文件）不进表**。
  给它们写下限等于守一个恒为真的判据，只会让人以为覆盖面比实际更广。
- **复用方向 B 已下载的包**，不额外拉一次。
  这也是 `PACKAGE_REQUIRED ⊆ SOURCE_MAP` 的原因。

门禁 `test_124` 新增 11 条，其中两条值得单独说：

- `test_integrity_result_reaches_the_report` 用 AST 钉住 `integrity`
  **必须由 `_package_integrity()` 赋值**。第一版只断言「它出现在 if 条件里」，
  反向验证时把赋值退化成 `integrity = None` 竟然照样通过 ——
  那正是本仓反复出现的「写了但没人读」的退化形态。
- `test_floors_leave_headroom` 钉住下限必须低于实测值、且不低于其一半。

反向验证：退化赋值 → 挂 2 条；下限贴实测值 → 挂 1 条；
从 SOURCE_MAP 删掉 petsite-rca-engine → 挂 3 条。

## 可复用的判据

**「部署成功」只说明 API 调用返回了 200，不说明部署的东西能跑。**
`cdk deploy` 报 `✨ Total time: 35.01s` 时，它上传的是一个必挂的包。

对任何「打包目录」式的部署（`Code.fromAsset`、`aws s3 sync`、
`docker build` 的 COPY），**构建产物与权威源的关系必须被显式校验一次**，
因为：

- 目录内容受上一次构建的残留影响（我这次是「没构建过」，
  反过来「构建过但没清理」会让 `test_m06` 误报死代码）；
- `git status` 干净**不代表**目录内容正确 —— `.gitignore` 排除的东西
  既不在索引里，也不在 `git clean -fd` 的清理范围里；
- 部署工具比较的是声明（模板 / hash），不是内容的语义完整性。

这与本仓 `deploy-worker.sh` 同日改掉的两个缺陷是**同一族**：
手工白名单漏一项、CDN 缓存取到旧版本 —— 三者的共同形态都是
**部署报成功，而线上拿到的不是你以为的东西**。

## 相关

- `docs/lessons/tech-debt-etl-lambdas-outside-cfn.md` —— 同族的 IaC 绕过
- `docs/runbooks/deployment-record.md` 4.47 —— 「永远红着的告警」的处置先例
- `infra/lambda/rca_window_flush/build.sh` —— 它的注释预言了 `.so` 被删

---

# 第二例：同一个坑在共享 Layer 上，而且更深

**日期**：2026-10-05
**发现路径**：收敛 `neptune-client-base` Layer 时顺手查「CDK 怎么定义它」

## 事实

`infra/lib/neptune-etl-stack.ts` 用 `Code.fromAsset('../lambda/shared')`
定义这个 Layer。`cdk synth` 实测它打出的 asset：

```
10 个文件，顶层只有 python/
python/ 下: __pycache__ · graph_cleanup.py · graph_confidence.py
            graph_contract.py · graph_contract_data.py · neptune_client_base.py
```

而线上 Layer 有 **90 个条目**，其中 77 个是依赖文件
（`certifi` / `charset_normalizer` / `idna` / `requests` / `urllib3`）。

`python/neptune_client_base.py` 的 import 行：

```python
import boto3, botocore, json, logging, os, requests, urllib3
```

`boto3` / `botocore` 由 Lambda 运行时提供，**`requests` 和 `urllib3` 不是**。

所以 `cdk deploy NeptuneEtlStack` 会把 Layer 替换成一个缺这两个包的版本，
**挂载它的 6 个 ETL 函数全部在 import 阶段挂掉**。

## 为什么它比第一例更深

| | gp-window-flush（10-04） | 共享 Layer（10-05） |
|---|---|---|
| 依赖怎么进去 | `build.sh` 的 pip install | **仓库里没有任何记录** |
| 爆炸半径 | 1 个函数 | **6 个函数同时** |
| 发现方式 | 记得部署前的包大小 | 查 CDK 定义时顺手 |

第一例至少有 `build.sh` 说明依赖怎么装。这一例**连构建脚本都没有** ——
线上那 77 个依赖文件是某次手工 `pip install` + 手工 `publish-layer-version`
的产物，而仓库里对此一字未记。

而且 CDK 的注释写着：

```
// 本 layer 是纯 Python（仅 python/neptune_client_base.py，无任何编译产物），
```

实盘是 5 个业务模块 + 77 个依赖文件。**注释与实盘脱节在这里不是风格问题** ——
下一个读它的人会据此判断「这个 Layer 很简单，deploy 一下没事」。

## 这也解释了 10-04 那条 cdk diff

部署 `etl_deepflow` 时 `cdk diff NeptuneEtlStack` 报过：

```
[~] AWS::Lambda::LayerVersion NeptuneClientBaseLayer   replace
 ├─ [+] CompatibleArchitectures (requires replacement)
 └─ [~] Content (requires replacement)
```

当时的判断是「Layer replacement 超出用户授权范围，改用 update-function-code」。
那个判断是对的，但**理由比当时想的严重** —— 它不只是「替换共享 Layer」，
而是会发布一个必挂的 Layer。`[~] Content` 这一行底下藏的是 90 → 5 条目。

## 处置

1. `infra/lambda/shared/requirements.txt` —— 依赖显式声明，**版本 pin**。
   本仓另两份用 `>=`，这里不行：Layer 由 6 个函数共享，`>=` 意味着每次构建
   装到不同版本 → 每次 `cdk deploy` 都 replace Layer → 6 个函数的引用反复变动，
   而那种变动在 `cdk diff` 里和一次真实更新长得一样。
2. `infra/lambda/shared/build.sh` —— 装依赖 + 清 `__pycache__`
   （CDK 的 `fromAsset` 不排除它，会一起打进 Layer）+ 完整性校验。
3. 修 CDK 注释，写清「部署前必须先跑 build.sh」及不跑的后果。
4. 门禁 `tests/test_128_cdk_layer_asset_must_carry_deps.py`：
   **凡被 CDK `Code.fromAsset` 打成 Layer 的目录，其模块的第三方 import
   必须在目录内可满足，或目录里有构建声明。** 判据是静态的，离线 CI 可跑。

验证：`build.sh` 产物与已验证的 Layer 21 比对 ——
**线上有而产物没有：无**（产物是超集，多的 34 个文件全是 `dist-info` 元数据）。

## 本轮自己踩的两个

**① 清理范围宽一格就误删。** 我在 build.sh 里写的清理命令是
`git clean -fdx infra/lambda/shared/`，然后它把我刚新建的 `build.sh` 和
`requirements.txt` 一起删了 —— 它们在首次提交前是未跟踪文件，而 `-x`
不区分 ignored 与单纯未跟踪。改成 `.../shared/python/`。
**这与本文要防的缺陷同源：清理范围也是个白名单。**

**② 判据自己静默失效。** 门禁第一版用一条正则跨整个构造块：

```
new\s+lambda\.LayerVersion\s*\([^)]*?code:\s*lambda\.Code\.fromAsset\(
```

给那个构造加上说明注释之后，注释里的中文括号和 `import requests, urllib3`
让 `[^)]*?` 跨不过去，于是判据**一个 Layer 都找不到**，静默变成「什么都没检查」。

抓到它的是 `test_cdk_defines_at_least_one_layer_from_asset` —— 那条测试
存在的全部理由就是这个。**一个找不到目标的门禁比没有门禁更坏**，
因为它让人以为这一类问题有人看着。改成按行扫，注释怎么写都不影响定位。

---

# 第三例：依赖装对了，但架构是构建机的

**日期**：2026-10-06
**发现路径**：给 `etl_appsignals` 补 build.sh（纳入 CFN 栈的前置）时，
比对产物与线上包，发现同名 `.so` 的架构标记不同

## 事实

```
线上包   yaml/_yaml.cpython-312-x86_64-linux-gnu.so
新产物   yaml/_yaml.cpython-312-aarch64-linux-gnu.so     ← 构建机是 ARM
```

顺着查共享 Layer，同一个问题更早就存在：

```
Layer 20 / 21   python/charset_normalizer/md.cpython-312-aarch64-linux-gnu.so
                python/charset_normalizer/md__mypyc.cpython-312-aarch64-linux-gnu.so

而挂这个 Layer 的 6 个 ETL 全部是 x86_64
```

各函数架构实测：

| 函数 | 架构 |
|---|---|
| 7 个 ETL（aws / cfn / deepflow / trigger / xray / appsignals / agentcore） | `x86_64` |
| `gp-window-flush` | `arm64` |
| 构建机 | `aarch64` |

## 为什么它一直没有信号

PyYAML 与 charset_normalizer 对架构不匹配的扩展都写了回退：

```python
try:
    from .cyaml import *          # 或 md.so
    __with_libyaml__ = True
except ImportError:
    __with_libyaml__ = False      # 纯 Python 实现
```

于是 **import 成功、功能正常、只是慢**。没有日志、没有指标、没有告警。
这是一次静默降级，不是故障 —— 要靠量性能才可能发现，而没人在量。

**严厉程度只取决于库有没有写回退。** `rca_window_flush/build.sh` 的注释
点明了这一点：`pydantic-core` 没有回退，所以那边会直接
`ImportError` 挂掉。同一个病，一边是性能损失，一边是冷启动失败。

## 这条才是真正的教训：正确做法早就存在

`infra/lambda/rca_window_flush/build.sh` **从一开始就做对了**：

```bash
python3.11 -m pip install requests pyyaml strands-agents \
    --platform "${LAMBDA_ARCH:-manylinux2014_aarch64}" \
    --python-version "${LAMBDA_PY:-3.12}" \
    --only-binary=:all: \
    -t "$DEST_DIR" -q
```

注释也写得比我后来写的更准：

> · 平台定向 aarch64 + py3.12：与 Lambda 运行时一致，
>   否则 pydantic-core / _yaml 这类二进制扩展在运行时 ImportError。

所以这不是「没人知道」的问题。Layer 的依赖是手工 `pip install` 装的，
手工操作的人没去看那个脚本；而 **2026-10-05 我补写 `shared/build.sh` 时
同样没去看，把同一个缺陷又写了一遍** —— 只写了 `--platform`，
还漏了 `--python-version`。

**一个正确做法只存在于一个文件里时，它等于不存在。**

这与本文前两例的「手工白名单」是同一类病的两面：前者是清单漏项，
后者是做法不传播。两者的修法也相同 —— 把它变成一道判据，而不是
一份需要有人记得的知识。

## 处置

- `infra/lambda/shared/build.sh` 与 `infra/lambda/etl_appsignals/build.sh`
  都固定 `--platform manylinux2014_x86_64` + `--python-version 3.12`
  + `--only-binary=:all:`（最后一项是必须的：没有它 pip 会在找不到目标
  wheel 时退回源码编译，编出构建机架构的产物，静默回到原问题）
- 修 CDK 里那段注释。它原来说这个 Layer「无编译产物，因此架构无关」——
  **那是 2026-10-05 我写错的**，实盘有 2 个架构特定 `.so`。
  双架构声明在功能上仍成立（靠回退），但理由不是「无编译产物」，
  而是「不匹配的那一侧会降级」，这两件事对下一个读者的意义完全不同。
- 门禁 `test_128` 增 4 条：本仓每个装依赖的构建脚本都必须固定
  `--platform` 与 `--python-version`、必须带 `--only-binary`、
  且必须写下固定的理由 —— 最后一条是因为删掉 `--platform` 之后
  一切照常工作，不写「为什么」的话下一个人会当它多余而删掉。

验证：两个脚本重跑后产物是 `cpython-312-x86_64`，
且 `etl_appsignals` 的产物与线上包「线上有而产物没有：无」。

## 遗留

线上 Layer 21 里那两个 `.so` **仍是 aarch64**。没有立刻重建 Layer，
因为这是性能降级而非故障，而重建要再动一次 6 个函数的 Layer 引用。
下一次 `cdk deploy NeptuneEtlStack` 会自然带上架构正确的版本。
