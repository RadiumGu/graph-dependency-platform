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
    engine = (os.environ.get("NLQUERY_ENGINE") or "strands").lower()
    if engine == "strands":
        try:
            from neptune.nl_query_strands import StrandsNLQueryEngine  # type: ignore
            return StrandsNLQueryEngine(profile=profile)
        except ImportError as e:
            logger.warning(
                "Strands engine 不可用 (%s)；回退 direct。"
                "主环境安装：/usr/bin/pip3 install 'strands-agents>=1.36' 'strands-agents-tools>=0.5'",
                e,
            )
        except Exception as e:  # 构造期失败也回退，避免线上崩
            logger.warning("Strands engine 构造失败 (%r)；回退 direct。", e)

    try:
        from neptune.nl_query_direct import DirectBedrockNLQuery  # type: ignore
        return DirectBedrockNLQuery(profile=profile)
    except ImportError:
        # PR2 rename 前的过渡期：回退到现版 NLQueryEngine
        from neptune.nl_query import NLQueryEngine  # type: ignore
        try:
            return NLQueryEngine(profile=profile)
        except TypeError:
            return NLQueryEngine()


def make_hypothesis_engine(profile: Any = None) -> "NLQueryBase":  # type: ignore[name-defined]
    """构造 HypothesisAgent 引擎，切换 env：HYPOTHESIS_ENGINE=direct|strands。

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
    """构造 LearningAgent 引擎，切换 env：LEARNING_ENGINE=direct|strands。

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
    from agents.learning_strands import StrandsLearningAgent  # type: ignore
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

