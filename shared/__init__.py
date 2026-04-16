"""shared — 跨模块共享工具。"""

import os

_DEFAULT_REGION = "ap-northeast-1"


def get_region() -> str:
    """统一获取 AWS Region。

    优先级：环境变量 REGION > AWS_DEFAULT_REGION > 默认 ap-northeast-1
    """
    return os.environ.get("REGION",
           os.environ.get("AWS_DEFAULT_REGION", _DEFAULT_REGION))
