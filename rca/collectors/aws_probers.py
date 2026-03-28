"""
aws_probers.py - Plugin-based AWS Service Prober (Layer 2)

Architecture:
  Each AWS service is a self-contained Probe class.
  Probes register themselves via @register_probe decorator.
  ProbeRegistry runs all relevant probes in parallel and returns
  a unified list of ProbeResult for the scoring pipeline.

Adding a new probe:
  1. Create a class inheriting BaseProbe
  2. Implement is_relevant() and probe()
  3. Decorate with @register_probe
  Done. No changes to rca_engine.py needed.
"""

import os
import logging
import boto3
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)
REGION = os.environ.get('REGION', 'ap-northeast-1')

# ─────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────

@dataclass
class ProbeResult:
    """Standard output format for every probe."""
    service_name: str           # AWS service name, e.g. "SQS", "RDS"
    healthy: bool               # False = anomaly detected
    score_delta: int            # Points to add to RCA confidence score (0~40)
    summary: str                # One-line human-readable finding
    details: dict = field(default_factory=dict)   # Raw data for prompt
    evidence: list  = field(default_factory=list) # Bullet points for Slack/report

    def to_prompt_block(self) -> str:
        lines = [f"[{self.service_name} Probe]", f"Status: {'OK' if self.healthy else '⚠️ ANOMALY'}",
                 f"Summary: {self.summary}"]
        for e in self.evidence:
            lines.append(f"  - {e}")
        return "\n".join(lines)


# ─────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────

_PROBE_REGISTRY: list = []

def register_probe(cls):
    """Decorator: auto-register a Probe class."""
    _PROBE_REGISTRY.append(cls())
    return cls


class BaseProbe:
    """Base class for all AWS service probes."""

    def is_relevant(self, signal: dict, affected_service: str) -> bool:
        """Return True if this probe should run for the given alarm/service."""
        return True

    def probe(self, signal: dict, affected_service: str) -> Optional[ProbeResult]:
        """Execute probe. Return ProbeResult or None if nothing found."""
        raise NotImplementedError


def run_all_probes(signal: dict, affected_service: str, timeout_sec: int = 10) -> list[ProbeResult]:
    """
    Run all relevant probes in parallel.
    Returns list of ProbeResult (anomalies only by default).
    """
    relevant = [p for p in _PROBE_REGISTRY if p.is_relevant(signal, affected_service)]
    logger.info(f"Layer2 probers: running {len(relevant)}/{len(_PROBE_REGISTRY)} probes "
                f"for service={affected_service}")

    results = []
    with ThreadPoolExecutor(max_workers=min(len(relevant), 6)) as executor:
        futures = {executor.submit(p.probe, signal, affected_service): p for p in relevant}
        for future in as_completed(futures, timeout=timeout_sec):
            probe = futures[future]
            try:
                result = future.result()
                if result is not None:
                    results.append(result)
                    status = "ANOMALY" if not result.healthy else "OK"
                    logger.info(f"Probe {result.service_name}: {status} score_delta={result.score_delta}")
            except Exception as e:
                logger.warning(f"Probe {type(probe).__name__} failed: {e}")

    return results


def format_probe_results(results: list[ProbeResult]) -> str:
    """Format all ProbeResults into a prompt block."""
    if not results:
        return "[Layer2 AWS Probers]\nNo anomalies detected across monitored AWS services."
    return "\n\n".join(r.to_prompt_block() for r in results)


def total_score_delta(results: list[ProbeResult]) -> int:
    """Sum score contributions from all anomalous probes (cap at 40)."""
    return min(sum(r.score_delta for r in results if not r.healthy), 40)


# ─────────────────────────────────────────────
# Probe implementations
# ─────────────────────────────────────────────

@register_probe
class SQSProbe(BaseProbe):
    """Detect SQS queue backlog and DLQ accumulation."""

    # Queue name patterns associated with each service
    SERVICE_QUEUE_MAP = {
        'petsite':        ['sqspetadoption', 'petadoption'],
        'petadoption':    ['sqspetadoption', 'petadoption'],
        'payforadoption': ['sqspetadoption'],
    }

    def is_relevant(self, signal, affected_service):
        return True  # SQS backlog can affect any service

    def probe(self, signal, affected_service) -> Optional[ProbeResult]:
        try:
            sqs = boto3.client('sqs', region_name=REGION)
            cw  = boto3.client('cloudwatch', region_name=REGION)

            # List all queues matching service patterns
            patterns = self.SERVICE_QUEUE_MAP.get(affected_service, [affected_service])
            queue_urls = []
            for pattern in patterns:
                resp = sqs.list_queues(QueueNamePrefix='')
                all_urls = resp.get('QueueUrls', [])
                queue_urls += [u for u in all_urls if pattern.lower() in u.lower()]
            queue_urls = list(set(queue_urls))

            if not queue_urls:
                return None  # No queues found, not relevant

            anomalies = []
            for url in queue_urls:
                qname = url.split('/')[-1]
                attrs = sqs.get_queue_attributes(
                    QueueUrl=url,
                    AttributeNames=['ApproximateNumberOfMessages',
                                    'ApproximateNumberOfMessagesNotVisible',
                                    'ApproximateNumberOfMessagesDelayed']
                )['Attributes']
                visible  = int(attrs.get('ApproximateNumberOfMessages', 0))
                inflight = int(attrs.get('ApproximateNumberOfMessagesNotVisible', 0))
                delayed  = int(attrs.get('ApproximateNumberOfMessagesDelayed', 0))

                is_dlq = 'dlq' in qname.lower() or 'dead' in qname.lower()
                if (is_dlq and visible > 0) or (not is_dlq and visible > 1000):
                    anomalies.append({
                        'queue': qname, 'visible': visible,
                        'inflight': inflight, 'delayed': delayed, 'is_dlq': is_dlq
                    })

            if not anomalies:
                return ProbeResult('SQS', True, 0, 'All queues healthy', {})

            evidence = []
            for a in anomalies:
                tag = '🔴 DLQ has messages' if a['is_dlq'] else '⚠️ Large backlog'
                evidence.append(f"{a['queue']}: visible={a['visible']} inflight={a['inflight']} ({tag})")

            return ProbeResult(
                service_name='SQS',
                healthy=False,
                score_delta=20,
                summary=f"{len(anomalies)} queue(s) with anomalies",
                details={'anomalies': anomalies},
                evidence=evidence,
            )
        except Exception as e:
            logger.warning(f"SQSProbe error: {e}")
            return None


@register_probe
class DynamoDBProbe(BaseProbe):
    """Detect DynamoDB throttling and system errors."""

    # DynamoDB table name patterns per service
    SERVICE_TABLE_MAP = {
        'petsite':     ['petadoption', 'ddbpetadoption'],
        'petadoption': ['petadoption', 'ddbpetadoption'],
    }

    def is_relevant(self, signal, affected_service):
        return True

    def probe(self, signal, affected_service) -> Optional[ProbeResult]:
        try:
            cw = boto3.client('cloudwatch', region_name=REGION)
            dynamodb = boto3.client('dynamodb', region_name=REGION)

            patterns = self.SERVICE_TABLE_MAP.get(affected_service, [affected_service])
            tables = []
            paginator = dynamodb.get_paginator('list_tables')
            for page in paginator.paginate():
                for t in page['TableNames']:
                    if any(p.lower() in t.lower() for p in patterns):
                        tables.append(t)

            if not tables:
                return None

            end = datetime.now(timezone.utc)
            start = end - timedelta(minutes=10)
            anomalies = []

            for table in tables:
                dims = [{'Name': 'TableName', 'Value': table}]
                for metric in ['ReadThrottleEvents', 'WriteThrottleEvents', 'SystemErrors']:
                    resp = cw.get_metric_statistics(
                        Namespace='AWS/DynamoDB', MetricName=metric,
                        Dimensions=dims, StartTime=start, EndTime=end,
                        Period=300, Statistics=['Sum']
                    )
                    pts = resp.get('Datapoints', [])
                    total = sum(p['Sum'] for p in pts)
                    if total > 0:
                        anomalies.append({'table': table, 'metric': metric, 'count': int(total)})

            if not anomalies:
                return ProbeResult('DynamoDB', True, 0, 'No throttling detected', {})

            evidence = [f"{a['table']}: {a['metric']}={a['count']}" for a in anomalies]
            is_throttle = any('Throttle' in a['metric'] for a in anomalies)

            return ProbeResult(
                service_name='DynamoDB',
                healthy=False,
                score_delta=25 if is_throttle else 15,
                summary=f"DynamoDB anomalies on {len(set(a['table'] for a in anomalies))} table(s)",
                details={'anomalies': anomalies},
                evidence=evidence,
            )
        except Exception as e:
            logger.warning(f"DynamoDBProbe error: {e}")
            return None


@register_probe
class LambdaProbe(BaseProbe):
    """Detect Lambda function errors, throttles, and timeout spikes."""

    SERVICE_FUNCTION_MAP = {
        'petsite':          ['petsite', 'statusupdater', 'StepFn'],
        'petadoption':      ['statusupdater', 'StepFn', 'stepread', 'stepprice'],
        'payforadoption':   ['StepFn', 'stepprice'],
    }

    def is_relevant(self, signal, affected_service):
        return True

    def probe(self, signal, affected_service) -> Optional[ProbeResult]:
        try:
            lam = boto3.client('lambda', region_name=REGION)
            cw  = boto3.client('cloudwatch', region_name=REGION)

            patterns = self.SERVICE_FUNCTION_MAP.get(affected_service, [affected_service])
            functions = []
            paginator = lam.get_paginator('list_functions')
            for page in paginator.paginate():
                for fn in page['Functions']:
                    name = fn['FunctionName']
                    if any(p.lower() in name.lower() for p in patterns):
                        functions.append(name)

            if not functions:
                return None

            end = datetime.now(timezone.utc)
            start = end - timedelta(minutes=10)
            anomalies = []

            for fn_name in functions:
                dims = [{'Name': 'FunctionName', 'Value': fn_name}]
                for metric, threshold in [('Errors', 1), ('Throttles', 1), ('Duration', None)]:
                    resp = cw.get_metric_statistics(
                        Namespace='AWS/Lambda', MetricName=metric,
                        Dimensions=dims, StartTime=start, EndTime=end,
                        Period=300, Statistics=['Sum' if metric != 'Duration' else 'Maximum']
                    )
                    pts = resp.get('Datapoints', [])
                    if not pts:
                        continue

                    if metric == 'Duration':
                        # Get function timeout config
                        try:
                            fn_config = lam.get_function_configuration(FunctionName=fn_name)
                            timeout_ms = fn_config.get('Timeout', 30) * 1000
                            max_duration = max(p['Maximum'] for p in pts)
                            if max_duration > timeout_ms * 0.9:  # > 90% of timeout
                                anomalies.append({
                                    'function': fn_name, 'metric': 'Duration',
                                    'value': f'{max_duration:.0f}ms (timeout={timeout_ms}ms)'
                                })
                        except Exception:
                            pass
                    else:
                        total = sum(p['Sum'] for p in pts)
                        if total >= threshold:
                            anomalies.append({
                                'function': fn_name, 'metric': metric, 'value': int(total)
                            })

            if not anomalies:
                return ProbeResult('Lambda', True, 0, 'All functions healthy', {})

            evidence = [f"{a['function']}: {a['metric']}={a['value']}" for a in anomalies]
            has_errors = any(a['metric'] == 'Errors' for a in anomalies)

            return ProbeResult(
                service_name='Lambda',
                healthy=False,
                score_delta=25 if has_errors else 10,
                summary=f"Lambda anomalies on {len(set(a['function'] for a in anomalies))} function(s)",
                details={'anomalies': anomalies},
                evidence=evidence,
            )
        except Exception as e:
            logger.warning(f"LambdaProbe error: {e}")
            return None


@register_probe
class ALBProbe(BaseProbe):
    """Detect ALB-side errors (ELB 5XX), unhealthy target counts, and high latency."""

    ALB_NAME_PATTERN = 'Servic-PetSi'  # Matches PetSite ALB

    def is_relevant(self, signal, affected_service):
        return True

    def probe(self, signal, affected_service) -> Optional[ProbeResult]:
        try:
            elb = boto3.client('elbv2', region_name=REGION)
            cw  = boto3.client('cloudwatch', region_name=REGION)

            # Find the ALB
            lbs = elb.describe_load_balancers()['LoadBalancers']
            albs = [lb for lb in lbs if self.ALB_NAME_PATTERN in lb['LoadBalancerName']]
            if not albs:
                return None

            lb = albs[0]
            lb_dim_value = '/'.join(lb['LoadBalancerArn'].split('/')[-3:])
            dims = [{'Name': 'LoadBalancer', 'Value': lb_dim_value}]

            end = datetime.now(timezone.utc)
            start = end - timedelta(minutes=10)
            anomalies = []

            metrics = {
                'HTTPCode_ELB_5XX_Count': ('Sum', 5),      # ALB-side errors (not target)
                'HTTPCode_ELB_4XX_Count': ('Sum', 50),
                'TargetResponseTime':     ('Average', 3.0), # seconds
                'RejectedConnectionCount': ('Sum', 1),      # connection pool exhausted
            }

            for metric, (stat, threshold) in metrics.items():
                resp = cw.get_metric_statistics(
                    Namespace='AWS/ApplicationELB', MetricName=metric,
                    Dimensions=dims, StartTime=start, EndTime=end,
                    Period=300, Statistics=[stat]
                )
                pts = resp.get('Datapoints', [])
                if not pts:
                    continue
                value = pts[-1].get(stat, 0)
                if value > threshold:
                    anomalies.append({'metric': metric, 'value': round(value, 3)})

            # Check unhealthy target count
            tgs = elb.describe_target_groups(LoadBalancerArn=lb['LoadBalancerArn'])['TargetGroups']
            for tg in tgs:
                health = elb.describe_target_health(TargetGroupArn=tg['TargetGroupArn'])
                unhealthy = [t for t in health['TargetHealthDescriptions']
                             if t['TargetHealth']['State'] != 'healthy']
                if unhealthy:
                    anomalies.append({
                        'metric': 'UnhealthyTargets',
                        'value': f"{len(unhealthy)} unhealthy in {tg['TargetGroupName']}"
                    })

            if not anomalies:
                return ProbeResult('ALB', True, 0, 'ALB healthy', {})

            evidence = [f"{a['metric']}={a['value']}" for a in anomalies]
            has_5xx = any('5XX' in a['metric'] or 'Unhealthy' in a['metric'] for a in anomalies)

            return ProbeResult(
                service_name='ALB',
                healthy=False,
                score_delta=30 if has_5xx else 10,
                summary=f"ALB anomalies: {', '.join(a['metric'] for a in anomalies)}",
                details={'alb_name': lb['LoadBalancerName'], 'anomalies': anomalies},
                evidence=evidence,
            )
        except Exception as e:
            logger.warning(f"ALBProbe error: {e}")
            return None


@register_probe
class EC2ASGProbe(BaseProbe):
    """
    Detect non-running EKS nodes.
    Replaces the inline EC2 API fallback in rca_engine.py.
    Activates only when Neptune graph traversal found nothing (passed via signal).
    """

    def is_relevant(self, signal, affected_service):
        # Only run when Neptune infra layer found no fault
        # Caller sets signal['neptune_infra_fault'] = False when graph is empty
        return not signal.get('neptune_infra_fault', True)

    def probe(self, signal, affected_service) -> Optional[ProbeResult]:
        try:
            ec2 = boto3.client('ec2', region_name=REGION)
            cluster = os.environ.get('EKS_CLUSTER_NAME', 'PetSite')
            resp = ec2.describe_instances(Filters=[
                {'Name': 'tag:eks:cluster-name', 'Values': [cluster]},
                {'Name': 'instance-state-name',
                 'Values': ['stopped', 'stopping', 'shutting-down', 'terminated']},
            ])

            cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
            import re
            unhealthy = []
            for res in resp.get('Reservations', []):
                for inst in res.get('Instances', []):
                    reason = inst.get('StateTransitionReason', '')
                    ts_match = re.search(r'\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', reason)
                    if ts_match:
                        try:
                            t = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                            if t < cutoff:
                                continue
                        except Exception:
                            pass
                    tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
                    unhealthy.append({
                        'id': inst['InstanceId'],
                        'name': tags.get('Name', inst['InstanceId']),
                        'state': inst['State']['Name'],
                        'az': inst.get('Placement', {}).get('AvailabilityZone', ''),
                    })

            if not unhealthy:
                return ProbeResult('EC2/ASG', True, 0, 'All EKS nodes running', {})

            evidence = [f"{n['name']} ({n['id']}): {n['state']} in {n['az']}" for n in unhealthy]
            az_set = set(n['az'] for n in unhealthy)

            return ProbeResult(
                service_name='EC2/ASG',
                healthy=False,
                score_delta=40,
                summary=f"{len(unhealthy)} EKS node(s) non-running, AZs: {az_set}",
                details={'unhealthy_nodes': unhealthy},
                evidence=evidence,
            )
        except Exception as e:
            logger.warning(f"EC2ASGProbe error: {e}")
            return None


@register_probe
class StepFunctionsProbe(BaseProbe):
    """Detect Step Functions execution failures and timeouts."""

    SF_NAME_PATTERN = 'StepFnStateMachine'

    def is_relevant(self, signal, affected_service):
        return affected_service in ('petsite', 'petadoption', 'payforadoption')

    def probe(self, signal, affected_service) -> Optional[ProbeResult]:
        try:
            sf = boto3.client('stepfunctions', region_name=REGION)
            cw = boto3.client('cloudwatch', region_name=REGION)

            # Find state machines
            machines = sf.list_state_machines()['stateMachines']
            targets = [m for m in machines if self.SF_NAME_PATTERN in m['name']]
            if not targets:
                return None

            end = datetime.now(timezone.utc)
            start = end - timedelta(minutes=15)
            anomalies = []

            for sm in targets:
                arn = sm['stateMachineArn']
                dims = [{'Name': 'StateMachineArn', 'Value': arn}]
                for metric, threshold in [('ExecutionsFailed', 1), ('ExecutionsTimedOut', 1),
                                          ('ExecutionsAborted', 1), ('ExecutionThrottled', 1)]:
                    resp = cw.get_metric_statistics(
                        Namespace='AWS/States', MetricName=metric,
                        Dimensions=dims, StartTime=start, EndTime=end,
                        Period=300, Statistics=['Sum']
                    )
                    pts = resp.get('Datapoints', [])
                    total = sum(p['Sum'] for p in pts)
                    if total >= threshold:
                        anomalies.append({
                            'state_machine': sm['name'],
                            'metric': metric, 'count': int(total)
                        })

            if not anomalies:
                return ProbeResult('StepFunctions', True, 0, 'State machines healthy', {})

            evidence = [f"{a['state_machine']}: {a['metric']}={a['count']}" for a in anomalies]
            return ProbeResult(
                service_name='StepFunctions',
                healthy=False,
                score_delta=20,
                summary=f"Step Functions anomalies: {len(anomalies)} metric(s) breached",
                details={'anomalies': anomalies},
                evidence=evidence,
            )
        except Exception as e:
            logger.warning(f"StepFunctionsProbe error: {e}")
            return None
