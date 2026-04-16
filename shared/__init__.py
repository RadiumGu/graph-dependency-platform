"""shared — 跨模块共享工具。"""

import os

_DEFAULT_REGION = "ap-northeast-1"


def get_region() -> str:
    """统一获取 AWS Region。

    优先级：
    1. 环境变量 REGION
    2. profile.aws_resources.primary_region
    3. 环境变量 AWS_DEFAULT_REGION
    4. 默认 ap-northeast-1

    Profile 是 single source of truth，环境变量作为 override 机制。
    """
    # 1. 环境变量 REGION 最高优先级（显式 override）
    if "REGION" in os.environ:
        return os.environ["REGION"]

    # 2. 从 profile 读（single source of truth）
    try:
        from profiles.profile_loader import EnvironmentProfile
        p = EnvironmentProfile()
        region = p.get("aws_resources.primary_region")
        if region:
            return region
    except Exception:
        pass

    # 3/4. 兜底
    return os.environ.get("AWS_DEFAULT_REGION", _DEFAULT_REGION)
