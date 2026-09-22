# ⚠️ 本目录是构建产物，不要据此判断线上行为

`build.sh` 把 `rca/` 复制到这里并装依赖，CDK 再从这里 `fromAsset` 打包
（`infra/lib/alert-buffer-stack.ts:166`）。也就是说：

- **权威源是 `rca/`**，不是这里
- 这里的 `.py` 是某次 build 之后的快照，`rca/` 后续更新它**不会自动跟上**
- 部署时会重跑 `build.sh`，所以**线上跑的是当时的 `rca/`**，不是 git 里的这份

## 这件事已经误导过两轮审查

2026-09-21 的审查对着这份陈旧产物得出两条「已确证」的结论，2026-09-22 实测
全部推翻：

| 审查结论 | 实测 |
|---|---|
| 产物缺 `analyze_group` → 调用 AttributeError → **窗口聚合 RCA 从未生效** | ❌ 线上部署包有 `analyze_group`，`core/rca_engine.py` 1035 行与 `rca/` 逐字节一致。窗口聚合**是生效的** |
| 产物 `fault_classifier.py` 在 `combined_tier0 >= 2` 时**自动升 P0**，与权威源刻意不升相悖 | ❌ `combined_tier0` 在线上与权威源里**出现 0 次**，只在这份陈旧产物里有 6 次。线上不会自动升 P0 |

当时 11 个 `.py` 全部漂移，其中 `neptune_queries.py` 差 410 行、`handler.py`
是产物比权威源**多** 133 行（反向漂移）。而逐个下载线上部署包比对，
**11/11 全部 `线上 ≡ rca/`** —— 这份产物与线上毫无关系。

## 怎么核实线上真实代码

不要读这个目录。下载实际部署的包：

```bash
URL=$(aws lambda get-function --region ap-northeast-1 \
        --function-name gp-window-flush --query "Code.Location" --output text)
curl -s -o pkg.zip "$URL" && unzip -oq pkg.zip -d pkg
sha256sum pkg/core/rca_engine.py rca/core/rca_engine.py   # 应当一致
```

## 门禁

`tests/test_90_build_artifact_must_not_drift.py` 断言这里被 git 跟踪的 `.py`
与 `rca/` 逐字节一致。它变红意味着产物又漂移了 —— 修法是从 `rca/` 重新同步，
**不是**修改这里的副本。要改代码请改 `rca/`。

## 未解决的结构性问题

产物本不该入库。彻底的修法是把本目录从 git 移除并 `.gitignore`，只保留
`build.sh`；代价是 `cdk deploy` 前必须先跑 `build.sh`，否则 `fromAsset`
找不到目录而失败（这是好的失败：明确报错，而不是打包陈旧代码）。
该变更会改变部署前提，等拍板。

此外 `81d243bd2c585b0f4821__mypyc.cpython-312-aarch64-linux-gnu.so` 与
`bin/normalizer` 是 pip 装依赖带来的二进制产物，同样不该入库。
