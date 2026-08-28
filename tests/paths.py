"""tests/paths.py — 测试用路径推导的**单一来源**

覆盖测试清单：共享基础设施，不对应具体用例编号

## 为什么需要这个文件

2026-08-28 之前,`tests/` 下 13 个测试文件各自硬编码
`PROJECT_ROOT = '/home/ubuntu/tech/graph-dependency-platform'` ——
那是最初开发机的路径。后果是**整个测试套件在其他任何机器上都跑不起来**:
`conftest.py` 导入即 `FileNotFoundError`,pytest 连收集阶段都过不去。
所谓"277 个测试"在原开发机之外一个都没运行过。

修 `conftest.py` 只解决了收集阶段;各测试文件自己的常量仍指向不存在的路径。

## 为什么不是把推导逻辑复制到每个文件

那会把"一处硬编码"换成"13 处重复推导" —— 正是 T-021 刚从
`SERVICE_FUNCTION_MAP` / `SVC_TO_CW` 清掉的那类问题。改一次布局要改 13 处,
且必然漏掉一两处产生新的漂移。故抽成本模块,所有测试文件 `from paths import ...`。

## 覆盖方式

设 `GDP_PROJECT_ROOT` 环境变量可覆盖推导结果,便于非常规布局
(如 CI 里仓库被挂到别处、或只检出部分子树)下运行。
"""
import os
from pathlib import Path

# 本文件位于 <repo>/tests/paths.py → 上两级即仓库根
PROJECT_ROOT = os.environ.get(
    'GDP_PROJECT_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

# Path 形态（test_23 等用 / 运算符拼接）
PROJECT_ROOT_PATH = Path(PROJECT_ROOT)

# 常用子目录 —— 集中在此,避免各测试文件各拼一遍
PROFILES_DIR = os.path.join(PROJECT_ROOT, 'profiles')
SHARED_DIR = os.path.join(PROJECT_ROOT, 'shared')
RCA_DIR = os.path.join(PROJECT_ROOT, 'rca')
CHAOS_CODE_DIR = os.path.join(PROJECT_ROOT, 'chaos', 'code')
DR_PLAN_DIR = os.path.join(PROJECT_ROOT, 'dr-plan-generator')

_LAMBDA_DIR = os.path.join(PROJECT_ROOT, 'infra', 'lambda')
ETL_AWS_DIR = os.path.join(_LAMBDA_DIR, 'etl_aws')
ETL_DEEPFLOW_DIR = os.path.join(_LAMBDA_DIR, 'etl_deepflow')
ETL_CFN_DIR = os.path.join(_LAMBDA_DIR, 'etl_cfn')
ETL_TRIGGER_DIR = os.path.join(_LAMBDA_DIR, 'etl_trigger')
RCA_WINDOW_FLUSH_DIR = os.path.join(_LAMBDA_DIR, 'rca_window_flush')


def assert_layout() -> None:
    """校验推导出的根目录确实是本仓库 —— 推错了要立刻失败,而不是让后续
    测试报出一堆令人误解的 "文件不存在"。
    """
    missing = [
        d for d in (PROFILES_DIR, SHARED_DIR, RCA_DIR, _LAMBDA_DIR)
        if not os.path.isdir(d)
    ]
    if missing:
        raise RuntimeError(
            f"PROJECT_ROOT 推导结果不像本仓库根: {PROJECT_ROOT}\n"
            f"缺失目录: {missing}\n"
            f"若仓库布局非常规,请设置 GDP_PROJECT_ROOT 环境变量。"
        )
