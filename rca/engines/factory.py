"""engines/factory.py — NLQuery 引擎工厂，按环境变量 NLQUERY_ENGINE 选实现。

规则：
  - 默认 strands（north_star §1.5 硬约束：LLM/agent 一律走 Strands）
  - NLQUERY_ENGINE=strands 且依赖已装 → strands
  - NLQUERY_ENGINE=strands 但 strands 未装 → 日志 warning + 回退 direct（不崩）
"""
from __future__ import annotations

import logging
import os
from typing import Any

from engines.base import NLQueryBase

logger = logging.getLogger(__name__)


def make_nlquery_engine(profile: Any = None) -> NLQueryBase:
    """构造 NLQuery 引擎。

    Args:
        profile: EnvironmentProfile；为 None 时由具体 engine 在 __init__ 内加载默认 profile。

    Returns:
        NLQueryBase 具体实现。
    """
    # ── 2026-09-20：去掉 direct 回退，只保留 strands ──────────────────────
    #
    # 这是四个模块里最后一个收尾的。卡了五个月的理由是「p99 是 direct 的
    # 5.58 倍」，而 2026-09-20 同日重测双引擎发现那批 04-18 数据早已过期
    # （实测 2.17x，已在 ≤2.5x 门槛内），随后把 ReAct 从 3 轮压到 2 轮，
    # 降到 **1.57x**、token 2.28x，准确率仍 20/20。
    #
    # 真正的阻塞其实不是性能，是两类代码问题，都已修：
    #   · 判据 `'error' in result` —— strands 的 _pack 总带 error key（值 None），
    #     direct 只在出错时放，于是每次成功都被判失败（test_e2e02 的「通过率
    #     不足」就是这么来的，不是引擎能力问题）
    #   · 七个测试文件绑定 direct 的实现方式（mock invoke_model、patch
    #     nl_query_direct.nc.results、调 _generate_cypher 私有方法），
    #     契约已由 tests/test_81_nlquery_contract_strands.py 用 strands 重写
    #
    # ⚠️ 代价：strands 不可用时直接抛异常、不再降级。
    from neptune.nl_query_strands import StrandsNLQueryEngine  # type: ignore
    return StrandsNLQueryEngine(profile=profile)


def make_hypothesis_engine(profile: Any = None) -> "NLQueryBase":  # type: ignore[name-defined]
    """构造 HypothesisAgent 引擎。

    ⚠️ 2026-09-21：env `HYPOTHESIS_ENGINE` 已无作用 —— direct 实现已删除、
    回退分支已去掉，只有 strands 一种。设 `=direct` 不报错但也不生效。

    默认 strands；strands 不可用 → warning + 回退 direct（回退是应急，不是常态）。
    """
    # ── 2026-09-20：去掉 direct 回退，只保留 strands ──────────────────────
    #
    # 收尾前 `prioritize_with_meta` 其实是个假实现：strands 版整个委托给
    # DirectBedrockHypothesis，再把结果打上 engine="strands" 标签 ——
    # 于是打分从来没被 Strands 化，而按 engine 标签做的统计都以为它是。
    # 现在 strands 版有了真实现（无 tools 的打分 Agent，实测 1692ms / 3087 tokens），
    # prompt 与加权规则搬到 agents/hypothesis_common.py 两个引擎共用。
    #
    # ⚠️ 代价：strands 不可用时直接抛异常、不再降级。
    from chaos.code.agents.hypothesis_strands import StrandsHypothesisAgent  # type: ignore
    return StrandsHypothesisAgent(profile=profile)  # type: ignore[return-value]


def make_learning_engine(profile: Any = None) -> "LearningBase":  # type: ignore[name-defined]
    """构造 LearningAgent 引擎。

    ⚠️ 2026-09-21：env `LEARNING_ENGINE` 已无作用 —— direct 实现已删除、
    回退分支已去掉，只有 strands 一种。设 `=direct` 不报错但也不生效。

    默认 strands；strands 不可用 → warning + 回退 direct（回退是应急，不是常态）。
    """
    # ── 2026-09-20：去掉 direct 回退，只保留 strands ──────────────────────
    #
    # 收尾前 learning 的迁移只做了 `generate_recommendations` 一个方法，
    # 另外四个（analyze / iterate_hypotheses / update_graph / generate_report，
    # 共 304 行、零 LLM 调用）在 strands 版里是**纯委托**给
    # DirectBedrockLearning，再盖上 engine="strands" 标签。
    # 现在那四个搬到 `agents/learning_common.LearningCommonMixin`，
    # 由 strands 版继承提供 —— mixin 用 self.ENGINE_NAME，标签自然正确。
    #
    # ⚠️ 代价：strands 不可用时直接抛异常、不再降级。
    # 路径写法与上面 hypothesis 保持一致（chaos.code.agents.*）——
    # 原来这里写的是短路径 `from agents.learning_strands import`，
    # 依赖 sys.path 里恰好有 chaos/code。两处不一致纯属历史遗留。
    #
    # ⚠️ 注意：`agents/` **不在 rca 的部署包清单里**
    # （build.sh 只复制 core/neptune/actions/collectors/data/search/engines），
    # 所以这个函数在 Lambda 里调用会 ImportError。当前无碍 ——
    # make_learning_engine 只被 chaos/code/main.py（CLI）与测试调用，
    # 两个 Lambda 都不走 learning。若将来 Lambda 需要它，必须先把
    # agents/ 加进打包清单，否则就是又一个「本地跑得通、线上必挂」。
    from chaos.code.agents.learning_strands import StrandsLearningAgent  # type: ignore
    return StrandsLearningAgent(profile=profile)  # type: ignore[return-value]

def make_layer2_engine(profile: Any = None) -> "Layer2ProberBase":  # type: ignore[name-defined]
    """构造 Layer2 Prober 引擎。**只有 strands 一种实现**（2026-09-20 起）。

    ## 为什么去掉了 direct 回退

    原先是 `LAYER2_ENGINE=direct|strands` 双轨 + strands 不可用时
    warning 回退 direct。那个回退在生产上**一直是实际路径** ——
    线上 Lambda 包里根本没装 strands（实测 strands 条目数 0），
    于是 LLM 路径全在静默跑 direct，而 golden 基线测的是本地装了
    strands 的环境，两者从未对齐过整整五个月。

    2026-09-20 给 `rca/deploy.sh` 与
    `infra/lambda/rca_window_flush/build.sh` 补上 strands 并部署，
    实测确认线上已真正走 strands："回退 direct" 日志 0 条、
    日志里有 `Creating Strands MetricsClient`、
    Step3d 产出 6 个 prober 结果、RCA complete in 48.8s。

    `collectors/layer2_direct.py` 的 delete_date 是 2026-08-19，
    已过期一个月，CI 的 check-deadlines 每次 push 都报红。

    ## 去掉回退的代价，必须知道

    strands 不可用时现在会**直接抛异常**，不再降级。这是迁移的目标
    （回退掩盖了"生产从未跑过 strands"这个事实五个月），
    但它要求 strands 依赖在**所有**部署环境里装齐。
    新增部署目标时先确认打包脚本装了 strands，否则 Layer 2 直接挂。
    """
    from engines.base import Layer2ProberBase  # 延迟导入避免循环
    from collectors.layer2_strands import StrandsLayer2Prober  # type: ignore
    return StrandsLayer2Prober(profile=profile)  # type: ignore[return-value]

