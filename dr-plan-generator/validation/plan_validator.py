"""
validation/plan_validator.py — Static DR plan validation

Checks for dependency cycles, completeness, step ordering consistency,
rollback command presence, and graph data freshness.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from models import DRPlan, Issue, ValidationReport

logger = logging.getLogger(__name__)

# Warn if graph snapshot is older than this (seconds)
_FRESHNESS_THRESHOLD = 3600


class PlanValidator:
    """Perform static validation of a DRPlan.

    Checks:
    1. Dependency cycle detection.
    2. Completeness (all affected resources have steps).
    3. Ordering consistency (dependencies before dependents).
    4. Rollback command presence.
    5. Graph snapshot freshness.
    """

    def validate(self, plan: DRPlan) -> ValidationReport:
        """Validate a DRPlan and return a ValidationReport.

        Args:
            plan: The DRPlan to validate.

        Returns:
            ValidationReport with ``valid`` flag and list of Issues.
        """
        issues: List[Issue] = []

        # 1. Cycle detection
        cycles = self._check_cycles(plan)
        if cycles:
            issues.append(Issue("CRITICAL", f"Dependency cycles detected: {cycles}"))

        # 2. Completeness
        missing = self._check_completeness(plan)
        if missing:
            issues.append(Issue("WARNING", f"Resources not covered in plan: {missing}"))

        # 3. Ordering consistency
        ordering_violations = self._check_ordering(plan)
        if ordering_violations:
            issues.append(Issue("CRITICAL", f"Ordering violations: {ordering_violations}"))

        # 4. Rollback command completeness
        no_rollback = [
            s.step_id
            for p in plan.phases
            for s in p.steps
            if not s.rollback_command
        ]
        if no_rollback:
            issues.append(
                Issue(
                    "WARNING",
                    f"{len(no_rollback)} steps missing rollback commands: {no_rollback}",
                )
            )

        # 5. Graph freshness
        if plan.graph_snapshot_time:
            try:
                snapshot_dt = datetime.fromisoformat(
                    plan.graph_snapshot_time.replace("Z", "+00:00")
                )
                if snapshot_dt.tzinfo is None:
                    snapshot_dt = snapshot_dt.replace(tzinfo=timezone.utc)
                age_secs = (
                    datetime.now(timezone.utc) - snapshot_dt
                ).total_seconds()
                if age_secs > _FRESHNESS_THRESHOLD:
                    issues.append(
                        Issue(
                            "WARNING",
                            f"Graph snapshot is {age_secs // 60:.0f}min old; "
                            "consider re-running ETL before executing this plan",
                        )
                    )
            except ValueError as exc:
                logger.warning("Could not parse graph_snapshot_time: %s", exc)

        # 6. Validation command quality
        validation_issues = self._check_validation_quality(plan)
        issues.extend(validation_issues)

        return ValidationReport(
            valid=all(i.severity != "CRITICAL" for i in issues),
            issues=issues,
        )

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_validation_quality(self, plan: DRPlan) -> List[Issue]:
        """Check that every step has meaningful, executable commands.

        对 **三个字段** 都做同一套判据：``command`` / ``validation`` /
        ``rollback_command``。

        ## 为什么要扫三个字段（2026-09-22 修）

        此前判据只完整应用在 ``command`` 上：

            command           可执行内容 ✓  TODO ✓  未绑定变量 ✓
            validation        空 ✓  echo $? ✓  纯注释 ✓ —— 但 **不查** TODO
                              与未绑定变量
            rollback_command  **只在别处查是否存在，内容完全不查**

        后果是一类缺陷结构性地看不见：Lambda 回滚步骤引用未绑定的
        ``$EVENT_SOURCE_UUID``，它在 ``rollback_command`` 里，所以校验器
        从来没有机会报它。

        而回滚命令写错比正常命令写错更危险：它只在**出事之后**才被执行，
        那时没人有余裕调试一条展开成空串的参数。

        ## 判据按字段语义分档

        三个字段对「空」的容忍度不同，不能一刀切：

        - ``command`` 为空 → 步骤空转，ERROR
        - ``validation`` 为空 → 验证是假的，ERROR
        - ``rollback_command`` 为空 → 合法（不是每步都可回滚），由别处出
          WARNING；但**非空时**必须真的可执行

        ## 为什么「无可执行内容」是 ERROR，而不是只查 TODO 字样

        原来的坑长这样::

            # TODO: Manual switchover required for NeptuneCluster 'x'
            # Add the appropriate AWS CLI command here.

        注释是合法的空命令，执行会「成功」，于是**演练全绿而那一步什么都没做**。
        这比命令写错危险得多：写错会报错，什么都没做不会。

        判据刻意是「有没有可执行行」而不是「有没有 TODO 这个词」：
        一句解释性注释里提到 TODO 不构成问题，而一个措辞里不含 TODO 的
        纯注释步骤同样是空转。抓行为，不抓关键词。

        Args:
            plan: The DRPlan to check.

        Returns:
            List of Issues found.
        """
        issues: List[Issue] = []
        for phase in plan.phases:
            for step in phase.steps:
                step_label = f"{phase.phase_id}/{step.step_id}"

                # (字段名, 取值, 空值是否算错)
                fields = (
                    ("command", (step.command or "").strip(), True),
                    ("validation", (step.validation or "").strip(), True),
                    ("rollback_command",
                     (getattr(step, "rollback_command", None) or "").strip(), False),
                )

                for fname, value, empty_is_error in fields:
                    if not value:
                        if empty_is_error:
                            issues.append(Issue(
                                "ERROR",
                                f"Step {step_label} has empty {fname}",
                            ))
                        # rollback_command 为空是合法的 —— 别处已出 WARNING
                        continue

                    code = self._executable_lines(value)
                    if not code:
                        if empty_is_error:
                            # command / validation 为纯注释 = 步骤空转或验证是假的。
                            # 注释是合法的空命令，执行会「成功」，于是演练全绿而
                            # 那一步什么都没做 —— 比命令写错危险得多。
                            issues.append(Issue(
                                "ERROR",
                                f"Step {step_label} {fname} has no executable line — it "
                                "is only comments, which 'succeed' while doing nothing, "
                                "so a rehearsal would pass with this step silently "
                                "skipped",
                            ))
                        # rollback_command 为纯注释**不报**。
                        #
                        # 2026-09-22：第一版把它也判 ERROR，对真实计划一跑报出 41 条，
                        # 其中绝大多数是 preflight-connectivity / preflight-vcpu-quota
                        # 这类**只读检查**步骤 —— 它们没有修改任何状态，本就不需要
                        # 回滚，写一条「无需回滚」的注释是正确做法，不是缺陷。
                        #
                        # 判据的分界是「这个字段为空会不会导致演练假通过」：
                        #   command / validation 为空 → 会（步骤空转但报成功）
                        #   rollback 为空            → 不会（它只在回滚时才执行）
                        # 「有没有 rollback」由别处的第 4 项出 WARNING，够了。
                        continue

                    if any("TODO" in line for line in code):
                        issues.append(Issue(
                            "ERROR",
                            f"Step {step_label} {fname} has a TODO placeholder inside "
                            "an executable line",
                        ))

                    # 未绑定的 shell 变量：展开成空串后参数解析错位，报的错离根因很远。
                    # 原 DNS 步骤的 $ZONE_ID / $TG_ARN、以及 Lambda 回滚的
                    # $EVENT_SOURCE_UUID 都是这个形态。
                    for var in self._unbound_shell_vars(value):
                        issues.append(Issue(
                            "WARNING",
                            f"Step {step_label} {fname} references shell variable "
                            f"${var} that the step never assigns — it expands to an "
                            "empty string and misaligns the following arguments",
                        ))

                    # 未替换的 Python 格式占位符。命令是模板拼出来的，
                    # `{target}` 这类残留说明拼接时漏了参数 —— 它会被原样
                    # 送进 shell，而 kubectl/aws CLI 只会报一个与根因无关的错。
                    for ph in self._unresolved_placeholders(value):
                        issues.append(Issue(
                            "ERROR",
                            f"Step {step_label} {fname} contains an unresolved "
                            f"template placeholder {ph} — the command was built by "
                            "string formatting and this argument never got filled in",
                        ))

                # validation 专有：echo $? 在独立执行的步骤里没有意义
                v = (step.validation or "").strip()
                if v and (v == "echo $?" or v.startswith("echo $")):
                    issues.append(Issue(
                        "WARNING",
                        f"Step {step_label} uses 'echo $?' as validation — "
                        "not meaningful (steps execute independently)",
                    ))
        return issues

    @staticmethod
    def _unresolved_placeholders(command: str) -> List[str]:
        """找出命令里没被替换掉的 Python 格式占位符，如 ``{target}``。

        命令是模板拼出来的，残留的 ``{xxx}`` 说明拼接时漏了参数。它会被原样
        送进 shell，而 kubectl / aws CLI 只会报一个与根因无关的错 ——
        「集群 {target}-cluster 不存在」这种信息不会让人想到是格式化漏了。

        ## 为什么不能简单地匹配所有大括号

        shell 自己合法地使用大括号：``${VAR}``、``awk '{print $1}'``、
        brace expansion ``{a,b}``、jq 的 ``'{...}'``。所以判据收窄为
        **纯标识符**占位符（字母数字下划线，可带点号或数字索引），
        并排除前面紧跟 ``$`` 的形式。

        误判的代价比漏判高：这条是 ERROR，一次误报会让人把整个校验器关掉。

        Args:
            command: 步骤命令（可能多行）。

        Returns:
            未替换的占位符列表（含大括号，便于直接显示）。
        """
        import re

        if not command:
            return []
        code = "\n".join(
            ln for ln in command.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
        found = []
        for m in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}", code):
            # ${VAR} 是 shell 变量，不是格式占位符
            if m.start() > 0 and code[m.start() - 1] == "$":
                continue
            found.append(m.group(0))
        return sorted(set(found))

    @staticmethod
    def _executable_lines(command: str) -> List[str]:
        """返回命令里真正会执行的行（去掉空行与注释行）。

        Args:
            command: 步骤命令（可能多行）。

        Returns:
            可执行行列表。
        """
        if not command:
            return []
        return [
            ln for ln in command.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]

    @staticmethod
    def _unbound_shell_vars(command: str) -> List[str]:
        """找出命令里引用了但从未赋值的 shell 变量。

        只看同一步骤内。视为「已绑定」的形式：

        - ``VAR=...`` / ``VAR=$(...)`` 直接赋值
        - ``for VAR in ...`` 循环变量
        - ``while read VAR`` / ``read VAR`` 读入
        - ``export VAR=...``

        ## 为什么必须认全这些形式（2026-09-22 补 for / read）

        判据原先只认 ``VAR=``。于是把 Lambda 回滚改成遍历写法::

            for U in $(aws lambda list-event-source-mappings ... --output text); do
              aws lambda update-event-source-mapping --uuid "$U" --enabled
            done

        时，``$U`` 会被报成未绑定 —— 一个**误报**。而这条判据的消费方会因为
        误报而失去信任：误判的代价比漏判高，一次误报就足以让人把整个校验器关掉。

        Args:
            command: 步骤命令（可能多行）。

        Returns:
            未绑定的变量名列表。
        """
        import re

        if not command:
            return []
        # 注释行里的引用不算——那是说明文字。
        code = "\n".join(
            ln for ln in command.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
        assigned = set(re.findall(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=", code, re.M))
        # for VAR in ... / while read VAR / read -r VAR
        assigned |= set(re.findall(r"\bfor\s+([A-Z_][A-Z0-9_]*)\s+in\b", code))
        assigned |= set(re.findall(r"\bread\s+(?:-\w+\s+)*([A-Z_][A-Z0-9_]*)", code))
        referenced = set(re.findall(r'\$\{?([A-Z_][A-Z0-9_]*)\}?', code))
        return sorted(referenced - assigned)

    def _check_cycles(self, plan: DRPlan) -> List[List[str]]:
        """Detect dependency cycles across all plan steps.

        Builds a step-level dependency graph and runs DFS cycle detection.

        Args:
            plan: DRPlan to check.

        Returns:
            List of detected cycles (each cycle is a list of step IDs).
        """
        # Collect all steps
        all_steps = [s for p in plan.phases for s in p.steps]
        step_ids = {s.step_id for s in all_steps}

        adj: Dict[str, List[str]] = {s.step_id: [] for s in all_steps}
        for step in all_steps:
            for dep in step.dependencies:
                if dep in step_ids:
                    adj[dep].append(step.step_id)

        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {sid: WHITE for sid in step_ids}
        cycles: List[List[str]] = []

        def dfs(node: str, path: List[str]) -> None:
            color[node] = GRAY
            path.append(node)
            for neighbor in adj.get(node, []):
                if color[neighbor] == GRAY:
                    cycle_start = path.index(neighbor)
                    cycles.append(path[cycle_start:] + [neighbor])
                elif color[neighbor] == WHITE:
                    dfs(neighbor, path)
            path.pop()
            color[node] = BLACK

        for sid in list(step_ids):
            if color[sid] == WHITE:
                dfs(sid, [])

        return cycles

    def _check_completeness(self, plan: DRPlan) -> List[str]:
        """Check that all affected resources have at least one step.

        Args:
            plan: DRPlan to check.

        Returns:
            List of resource names not covered by any step.
        """
        covered = {s.resource_name for p in plan.phases for s in p.steps}
        # Exclude infra/synthetic steps that don't map to real resource names
        excluded_prefixes = ("preflight-", "validation-", "rollback-validation-")
        covered_resources = {
            n for n in covered
            if not any(n.startswith(pfx) for pfx in excluded_prefixes)
        }

        missing = [
            r for r in plan.affected_resources
            if r not in covered_resources
        ]
        return missing

    def _check_ordering(self, plan: DRPlan) -> List[str]:
        """Verify that no step is scheduled before its dependencies.

        Rule: if step A lists step ID B in its dependencies, B must
        appear before A in the flattened step execution order.

        Args:
            plan: DRPlan to check.

        Returns:
            List of violation description strings.
        """
        step_order: Dict[str, int] = {}
        global_order = 0
        for phase in plan.phases:
            for step in phase.steps:
                step_order[step.step_id] = global_order
                step_order[step.resource_name] = global_order
                global_order += 1

        violations: List[str] = []
        for phase in plan.phases:
            for step in phase.steps:
                for dep_id in step.dependencies:
                    dep_pos = step_order.get(dep_id)
                    step_pos = step_order.get(step.step_id)
                    if dep_pos is not None and step_pos is not None:
                        if dep_pos > step_pos:
                            violations.append(
                                f"{step.step_id} is scheduled before its "
                                f"dependency {dep_id}"
                            )
        return violations
