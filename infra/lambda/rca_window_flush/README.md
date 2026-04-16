# lambda/rca_window_flush

CDK 打包目录，用于部署 `gp-window-flush` Lambda。

## 说明

此目录在 CDK synth/deploy 时由 `build.sh` 从 `rca/` 源码打包填充。
CDK 使用 `Code.fromAsset('../lambda/rca_window_flush')` 直接上传此目录内容。

Lambda handler: `window_flush_handler.window_flush_handler`（对应 `rca/window_flush_handler.py`）

## 打包方式

```bash
cd infra/lambda/rca_window_flush
bash build.sh
```

## 包含内容

打包完成后，此目录应包含：

- `window_flush_handler.py` — Lambda 入口
- `core/` — RCA 核心模块
- `neptune/` — Neptune 客户端 + 查询
- `actions/` — 告警动作
- `collectors/` — 指标采集
- `config.py` — 配置
- `requests/` 等第三方依赖（pip 安装）

## 注意事项

- 此目录的内容**不应手动编辑**，由 `build.sh` 生成
- 源码改动请在 `rca/` 目录进行，然后重新运行 `build.sh`
- `.gitignore` 应忽略此目录下除 `README.md` 和 `build.sh` 之外的文件
