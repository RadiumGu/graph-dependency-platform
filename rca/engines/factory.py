"""engines/factory.py — NLQuery 引擎工厂，按环境变量 NLQUERY_ENGINE 选实现。

规则：
  - 默认 direct
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
    engine = (os.environ.get("NLQUERY_ENGINE") or "direct").lower()
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

    默认 direct；strands 不可用 → warning + 回退 direct。
    """
    from engines.base import HypothesisBase  # 延迟导入避免循环
    engine = (os.environ.get("HYPOTHESIS_ENGINE") or "direct").lower()
    if engine == "strands":
        try:
            from chaos.code.agents.hypothesis_strands import StrandsHypothesisAgent  # type: ignore
            return StrandsHypothesisAgent(profile=profile)  # type: ignore[return-value]
        except ImportError as e:
            logger.warning(
                "Strands HypothesisAgent 不可用 (%s)；回退 direct。", e,
            )
        except Exception as e:
            logger.warning("Strands HypothesisAgent 构造失败 (%r)；回退 direct。", e)

    try:
        from chaos.code.agents.hypothesis_direct import DirectBedrockHypothesis  # type: ignore
        return DirectBedrockHypothesis(profile=profile)  # type: ignore[return-value]
    except ImportError:
        # PR2 rename 前的过渡期：回退到现版 HypothesisAgent
        from chaos.code.agents.hypothesis_agent import HypothesisAgent  # type: ignore
        try:
            return HypothesisAgent(profile=profile)  # type: ignore[call-arg,return-value]
        except TypeError:
            return HypothesisAgent()  # type: ignore[return-value]


def make_learning_engine(profile: Any = None) -> "LearningBase":  # type: ignore[name-defined]
    """构造 LearningAgent 引擎，切换 env：LEARNING_ENGINE=direct|strands。

    默认 direct；strands 不可用 → warning + 回退 direct。
    """
    from engines.base import LearningBase  # 延迟导入避免循环
    engine = (os.environ.get("LEARNING_ENGINE") or "direct").lower()
    if engine == "strands":
        try:
            from agents.learning_strands import StrandsLearningAgent  # type: ignore
            return StrandsLearningAgent(profile=profile)  # type: ignore[return-value]
        except ImportError as e:
            logger.warning(
                "Strands LearningAgent 不可用 (%s)；回退 direct。", e,
            )
        except Exception as e:
            logger.warning("Strands LearningAgent 构造失败 (%r)；回退 direct。", e)

    try:
        from agents.learning_direct import DirectBedrockLearning  # type: ignore
        return DirectBedrockLearning(profile=profile)  # type: ignore[return-value]
    except ImportError:
        from agents.learning_agent import LearningAgent  # type: ignore
        try:
            return LearningAgent(profile=profile)  # type: ignore[call-arg,return-value]
        except TypeError:
            return LearningAgent()  # type: ignore[return-value]


def make_layer2_engine(profile: Any = None) -> "Layer2ProberBase":  # type: ignore[name-defined]
    """\u6784\u9020 Layer2 Prober \u5f15\u64ce\uff0c\u5207\u6362 env\uff1aLAYER2_ENGINE=direct|strands\u3002

    \u9ed8\u8ba4 direct\uff1bstrands \u4e0d\u53ef\u7528 \u2192 warning + \u56de\u9000 direct\u3002
    """
    from engines.base import Layer2ProberBase  # \u5ef6\u8fdf\u5bfc\u5165\u907f\u514d\u5faa\u73af
    engine = (os.environ.get("LAYER2_ENGINE") or "direct").lower()
    if engine == "strands":
        try:
            from collectors.layer2_strands import StrandsLayer2Prober  # type: ignore
            return StrandsLayer2Prober(profile=profile)  # type: ignore[return-value]
        except ImportError as e:
            logger.warning(
                "Strands Layer2Prober \u4e0d\u53ef\u7528 (%s)\uff1b\u56de\u9000 direct\u3002", e,
            )
        except Exception as e:
            logger.warning("Strands Layer2Prober \u6784\u9020\u5931\u8d25 (%r)\uff1b\u56de\u9000 direct\u3002", e)

    from collectors.layer2_direct import DirectLayer2Prober  # type: ignore
    return DirectLayer2Prober(profile=profile)  # type: ignore[return-value]