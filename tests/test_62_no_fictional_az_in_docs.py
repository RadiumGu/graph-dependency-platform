"""test_62_no_fictional_az_in_docs.py — 文档不得教人用不存在的 AZ 名。

## 这条门禁挡的是什么

`dr-plan-generator` 的文档里到处是 `apne1-az1` / `apne1-az2` / `apne1-az4`。
真实图谱里的 AZ 是 `ap-northeast-1a` / `ap-northeast-1c` / `ap-northeast-1d`，
**而且没有任何别名翻译层**。

后果不是报错。README 与 SKILL.md 里那些是**可复制粘贴的命令行**：

    python3 main.py plan --scope az --source apne1-az1 --target apne1-az2

照着跑会得到一份「受影响服务 0」的空计划 —— 有计划 ID、有 RTO、有阶段，
唯独没有内容。一个读 `AGENT.md` 的 agent 会照着传这个参数，
然后拿着空计划继续推理。

这也解释了为什么 AZ scope 的一个基础缺陷能活到 2026-09-07：
**它从来只在合成 fixture 上验证过。**

## 为什么 examples/ 与 tests/ 不在管辖范围内

`examples/*.json|md` 是**历史产物**（由 `generate_examples.py` 从一份硬编码的
合成节点表生成）。把里面的 AZ 名搜索替换掉，会造出「声称来自
`ap-northeast-1a`、实际描述另一套拓扑」的假产物 —— 比留着假名更糟。
正确做法是**标明它们是合成的**，README 里已经这么做了。

`tests/` 与 `tests/fixtures/` 是合成数据，本就该用不会与现实混淆的名字。
真实 AZ 名的覆盖由 `tests/test_60_dr_az_scope_finds_services.py` 负责 ——
它直接打活图谱。
"""
import re
from pathlib import Path

from paths import PROJECT_ROOT

_DR = Path(PROJECT_ROOT) / 'dr-plan-generator'

#: 会被人或 agent 当成「照这么写」的文件。
_INSTRUCTIONAL = [
    'README.md', 'README_CN.md', 'AGENT.md',
    'skills/dr-plan/SKILL.md', 'docs/prd.md', 'docs/tdd.md',
    'references/aws-fault-isolation-boundaries.md',
    'references/aws-fault-isolation-boundaries_CN.md',
]

_FICTIONAL = re.compile(r'apne1-az\d')

#: 危害所在的**语法位置**：可复制粘贴的命令行参数。
#:
#: 第一版判据是「文件里出现 apne1-azN 就算违规」，结果它匹配到了
#: 我自己写的那两行警告 —— 那两行的内容恰恰是「这些名字是假的」。
#: 本会话第九次「判据对准了字符串而不是语法位置」。
#:
#: 真正有害的是**照着能跑**的那种出现：`--source apne1-az1`。
#: 说明性文字里提到这个名字（用来警告它不存在）是必要的，不能禁。
_CMD_ARG = re.compile(r'--(?:source|target|failure|az)[= ]+([^\s`|]*apne1-az\d[^\s`|]*)')


def test_m01_命令行里不得出现虚构AZ名():
    """判据对准「照着能跑」的位置，不是「提到这个名字」。"""
    bad = {}
    for rel in _INSTRUCTIONAL:
        p = _DR / rel
        if not p.exists():
            continue
        hits = [f'{i}: {ln.strip()[:90]}'
                for i, ln in enumerate(p.read_text(encoding='utf-8').split('\n'), 1)
                if _CMD_ARG.search(ln)]
        if hits:
            bad[rel] = hits
    assert not bad, (
        '这些文档里有**可复制粘贴**的虚构 AZ 名：\n  '
        + '\n  '.join(f'{k} → {v}' for k, v in bad.items()) + '\n\n'
        '真实图谱里是 ap-northeast-1a / 1c / 1d，且**没有别名翻译层**。'
        '照着跑会得到一份「受影响服务 0」的空计划 —— 有计划 ID、有 RTO、'
        '有阶段，唯独没有内容；读 AGENT.md 的 agent 会照着传这个参数，'
        '然后拿空计划继续推理。')


def test_m01b_必须有文字说明这套假名的存在():
    """光把命令改对不够 —— examples/ 里还留着假名，得有地方解释为什么。"""
    ok = False
    for rel in ('README.md', 'README_CN.md'):
        p = _DR / rel
        if p.exists() and _FICTIONAL.search(p.read_text(encoding='utf-8')):
            ok = True
    assert ok, (
        'README 里完全没提 apne1-azN 这套假名。examples/ 目录里还留着它们'
        '（那是历史产物，改了会造假产物），所以必须有一处说明它们是什么、'
        '为什么留着 —— 否则下一个人看到会以为那是有效的 AZ 名。')


def test_m02_examples必须被标明是合成的():
    """不改它们的 AZ 名是对的，但必须说清它们不是真实图谱的结果。"""
    for rel in ('README.md', 'README_CN.md'):
        src = (_DR / rel).read_text(encoding='utf-8')
        assert re.search(r'synthetic|合成', src), (
            f'{rel} 没有标明 examples/ 是合成拓扑。'
            '原文写「基于 PetSite 拓扑的预生成示例」，会被读成来自真实图谱 —— '
            '而它们其实来自 generate_examples.py 里一份硬编码的节点表。')
        assert 'ap-northeast-1' in src, (
            f'{rel} 没有给出真实 AZ 名下的实测数字作对照。'
            '标明「这是合成的」还不够，得让读者知道真实情况是什么。')


def test_m03_生成器自己必须标明产出的是合成数据():
    p = _DR / 'examples' / 'generate_examples.py'
    src = p.read_text(encoding='utf-8')
    assert re.search(r'synthetic|合成', src[:2000]), (
        'generate_examples.py 开头没有标明它产出的是合成数据。'
        '它是那批假产物的源头 —— 下一个改它的人应该一眼看到这件事，'
        '而不是以为自己在从真实图谱生成示例。')
