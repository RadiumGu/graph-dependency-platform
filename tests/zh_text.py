"""中文文本断言辅助 —— 让门禁比对**语义**而不是标点宽度。

## 为什么有这个文件

连续两轮（test_98、test_99）出现同一种假失败:文档里写的是半角逗号 `,`,
断言里手抄成了全角 `，`,于是**文档完全正确、门禁却挂掉**。

这是「判据没照抄实现」这个错法的第 9、10 次,而且两次在同一条轴上。
打补丁修字符串是第三次踩同一个坑,所以改成从根上消掉这一类:
**断言比对前把两侧的标点归一化**。

## 为什么这样做是对的，而不是在放松判据

门禁想守的是「这句话还在手册里吗」,**不是**「这句话用的是哪种逗号」。
后者从来不是被守护的性质 —— 把它写进判据是我的手抄失误,不是需求。

归一化只动标点,不动任何汉字、数字、英文标识符。所以
「7 个 Deployment」不会匹配「8 个 Deployment」,
`alb-ingress-controller` 也不会匹配别的角色名 —— 区分力一点没少。

## 不要扩大到大小写或空白

刻意**只**归一化成对的中英标点。大小写在这里是有意义的
（`AmazonEKSViewPolicy` 与 `amazoneksviewpolicy` 不是一回事）,
空白也是（Markdown 的缩进有语义）。
"""
from __future__ import annotations

# 成对的中/英标点。左边是全角，右边是对应半角。
# 只列真正会被混写的那些 —— 不做全角字母数字之类的转换。
_PUNCT_MAP = {
    "，": ",",
    "。": ".",
    "：": ":",
    "；": ";",
    "！": "!",
    "？": "?",
    "（": "(",
    "）": ")",
    "、": ",",
}

_TABLE = str.maketrans(_PUNCT_MAP)


def normalize_punct(s: str) -> str:
    """把全角标点折成半角，便于按语义比对。

    刻意不动大小写、空白、引号和破折号:
    - 引号有 `「」` / `""` / `''` 多种风格，折叠会造成误匹配
    - 破折号 `—` 与连字符 `-` 在标识符里有意义（`pay-for-adoption`）
    """
    return s.translate(_TABLE)


def contains(haystack: str, needle: str) -> bool:
    """`needle in haystack`，但对标点宽度不敏感。"""
    return normalize_punct(needle) in normalize_punct(haystack)


def assert_contains(haystack: str, needle: str, why: str = "") -> None:
    """断言 `needle` 出现在 `haystack` 里（标点宽度不敏感）。

    失败信息里带上归一化后的针,这样一眼能看出是真缺了还是别的问题。
    """
    if contains(haystack, needle):
        return
    msg = f"找不到这句话:{needle!r}"
    if why:
        msg += f"\n  {why}"
    msg += f"\n  （已做标点归一化后仍找不到，所以不是全角/半角的问题）"
    raise AssertionError(msg)
