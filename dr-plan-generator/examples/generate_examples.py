#!/usr/bin/env python3
"""
Generate example DR plans using mock data (no Neptune connection needed).
Produces both Markdown and JSON examples for AZ-level and Region-level switchovers.
"""
import sys, os, json, time, dataclasses
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from models import DRPlan
from graph.graph_analyzer import GraphAnalyzer
from planner.plan_generator import PlanGenerator
from planner.step_builder import StepBuilder
from planner.rollback_generator import RollbackGenerator
from assessment.impact_analyzer import ImpactAnalyzer
from assessment.rto_estimator import RTOEstimator
from validation.plan_validator import PlanValidator
from output.markdown_renderer import MarkdownRenderer
from output.json_renderer import JSONRenderer

OUT_DIR = os.path.dirname(__file__)

# ── AZ1 故障数据 (PetSite on ap-northeast-1, AZ1 down) ──
AZ1_NODES = [
    {"name": "petsite-db", "type": "RDSCluster", "tier": "Tier0", "az": "apne1-az1", "state": "available"},
    {"name": "petsearch-db", "type": "DynamoDBTable", "tier": "Tier0", "az": "apne1-az1", "state": "ACTIVE", "global_table": True},
    {"name": "pethistory-queue", "type": "SQSQueue", "tier": "Tier1", "az": "apne1-az1", "state": "active"},
    {"name": "petsite", "type": "Microservice", "tier": "Tier0", "az": "apne1-az1", "state": "running"},
    {"name": "petsearch", "type": "Microservice", "tier": "Tier0", "az": "apne1-az1", "state": "running"},
    {"name": "payforadoption", "type": "Microservice", "tier": "Tier1", "az": "apne1-az1", "state": "running"},
    {"name": "pethistory", "type": "Microservice", "tier": "Tier1", "az": "apne1-az1", "state": "running"},
    {"name": "petfood", "type": "Microservice", "tier": "Tier2", "az": "apne1-az1", "state": "running"},
    {"name": "petadoption-lambda", "type": "LambdaFunction", "tier": "Tier2", "az": "apne1-az1", "state": "Active"},
    {"name": "petstatusupdater", "type": "LambdaFunction", "tier": "Tier2", "az": "apne1-az1", "state": "Active"},
    {"name": "petsite-alb", "type": "LoadBalancer", "tier": None, "az": "apne1-az1", "state": "active"},
    {"name": "petsite-tg", "type": "TargetGroup", "tier": None, "az": "apne1-az1", "state": "active"},
    {"name": "petsite-svc", "type": "K8sService", "tier": "Tier0", "az": "apne1-az1", "state": "running"},
    {"name": "petsearch-svc", "type": "K8sService", "tier": "Tier0", "az": "apne1-az1", "state": "running"},
]
AZ1_EDGES = [
    {"from": "petsite", "to": "petsite-db", "type": "AccessesData"},
    {"from": "petsite", "to": "petsearch", "type": "Calls"},
    {"from": "petsite", "to": "payforadoption", "type": "Calls"},
    {"from": "petsearch", "to": "petsearch-db", "type": "AccessesData"},
    {"from": "payforadoption", "to": "petsite-db", "type": "AccessesData"},
    {"from": "pethistory", "to": "pethistory-queue", "type": "AccessesData"},
    {"from": "pethistory", "to": "petsite-db", "type": "AccessesData"},
    {"from": "petadoption-lambda", "to": "petsearch-db", "type": "AccessesData"},
    {"from": "petstatusupdater", "to": "pethistory-queue", "type": "AccessesData"},
    {"from": "petsite-alb", "to": "petsite-tg", "type": "DependsOn"},
    {"from": "petsite-tg", "to": "petsite", "type": "DependsOn"},
    {"from": "petsite-svc", "to": "petsite", "type": "DependsOn"},
    {"from": "petsearch-svc", "to": "petsearch", "type": "DependsOn"},
]

# ── Region 级故障数据 (ap-northeast-1 完全不可用，切换到 us-west-2) ──
REGION_NODES = AZ1_NODES + [
    {"name": "petsite-db-replica", "type": "RDSCluster", "tier": "Tier0", "az": "apne1-az2", "state": "available"},
    {"name": "petsite-cdn", "type": "LoadBalancer", "tier": None, "az": "apne1-az2", "state": "active"},
    {"name": "petsite-ec2-1", "type": "EC2Instance", "tier": None, "az": "apne1-az1", "state": "running"},
    {"name": "petsite-ec2-2", "type": "EC2Instance", "tier": None, "az": "apne1-az2", "state": "running"},
    {"name": "petsite-ec2-3", "type": "EC2Instance", "tier": None, "az": "apne1-az4", "state": "running"},
    {"name": "petsite-incidents", "type": "S3Bucket", "tier": "Tier2", "az": "apne1-az1", "state": "active"},
    {"name": "petsite-neptune", "type": "NeptuneCluster", "tier": "Tier1", "az": "apne1-az1", "state": "available"},
    {"name": "pet-stepfn-adoption", "type": "StepFunction", "tier": "Tier1", "az": "apne1-az1", "state": "ACTIVE"},
]
REGION_EDGES = AZ1_EDGES + [
    {"from": "petsite-db-replica", "to": "petsite-db", "type": "DependsOn"},
    {"from": "petsite-cdn", "to": "petsite-alb", "type": "DependsOn"},
    {"from": "petsite", "to": "petsite-ec2-1", "type": "RunsOn"},
    {"from": "petsearch", "to": "petsite-ec2-2", "type": "RunsOn"},
    {"from": "pethistory", "to": "petsite-ec2-3", "type": "RunsOn"},
    {"from": "petsite", "to": "petsite-incidents", "type": "WritesTo"},
    {"from": "petsite", "to": "petsite-neptune", "type": "AccessesData"},
    {"from": "pet-stepfn-adoption", "to": "petadoption-lambda", "type": "DependsOn"},
]


def generate_plan_from_data(nodes, edges, scope, source, target, exclude=None, plan_id_override=None):
    """Generate a DR plan from mock data without Neptune."""
    analyzer = GraphAnalyzer()
    builder = StepBuilder()
    generator = PlanGenerator(analyzer, builder)

    # Build subgraph from mock data
    subgraph = {"nodes": list(nodes), "edges": list(edges)}

    # Filter excluded
    if exclude:
        excluded_set = set(exclude)
        subgraph['nodes'] = [n for n in subgraph['nodes'] if n['name'] not in excluded_set]
        subgraph['edges'] = [e for e in subgraph['edges']
                             if e['from'] not in excluded_set and e['to'] not in excluded_set]

    # Classify and sort
    layers = analyzer.classify_by_layer(subgraph)
    sorted_layers = {}
    for layer_name, layer_nodes in layers.items():
        sorted_layers[layer_name] = analyzer.topological_sort_within_layer(
            layer_nodes, subgraph['edges']
        )

    # Build phases
    phases = generator._build_phases(sorted_layers, layers, subgraph, source, target, None)

    # Impact + RTO/RPO
    impact = ImpactAnalyzer().assess_impact(subgraph, scope, source)
    rto = RTOEstimator().estimate(phases)
    rpo = generator._estimate_rpo(layers.get('L0', []))

    now = datetime.now(timezone.utc).isoformat()
    plan = DRPlan(
        plan_id=plan_id_override or f"dr-{scope}-{int(time.time())}",
        created_at=now,
        scope=scope,
        source=source,
        target=target,
        affected_services=[n['name'] for n in subgraph['nodes']
                          if n.get('type') in ('Microservice', 'K8sService')],
        affected_resources=[n['name'] for n in subgraph['nodes']],
        phases=phases,
        rollback_phases=[],
        impact_assessment=impact,
        estimated_rto=rto,
        estimated_rpo=rpo,
        validation_status='pending',
        graph_snapshot_time=now,
    )

    # Rollback + validation
    plan.rollback_phases = RollbackGenerator().generate_rollback(plan)
    report = PlanValidator().validate(plan)
    plan.validation_status = 'valid' if report.valid else 'issues_found'

    return plan, report


def write_outputs(plan, report, name):
    """Write markdown and JSON outputs."""
    md_path = os.path.join(OUT_DIR, f"{name}.md")
    json_path = os.path.join(OUT_DIR, f"{name}.json")

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(MarkdownRenderer().render(plan))
    with open(json_path, 'w', encoding='utf-8') as f:
        f.write(JSONRenderer().render(plan))

    total_steps = sum(len(p.steps) for p in plan.phases)
    rollback_steps = sum(len(p.steps) for p in plan.rollback_phases)
    valid = "✅ VALID" if report.valid else f"⚠️ {len(report.issues)} issues"
    print(f"  {name}.md + .json")
    print(f"     Affected: {len(plan.affected_services)} services, {len(plan.affected_resources)} resources")
    print(f"     Steps: {total_steps} switchover + {rollback_steps} rollback")
    print(f"     RTO: ~{plan.estimated_rto}min | RPO: ~{plan.estimated_rpo}min | Validation: {valid}")


if __name__ == '__main__':
    print("=" * 60)
    print("  DR Plan Generator — Example Plans")
    print("=" * 60)

    # ── Example 1: AZ 级切换 ──
    print("\n📋 Example 1: AZ-level switchover")
    print("   Scenario: apne1-az1 failure → failover to apne1-az2,apne1-az4")
    plan, report = generate_plan_from_data(
        AZ1_NODES, AZ1_EDGES,
        scope='az', source='apne1-az1', target='apne1-az2,apne1-az4',
        plan_id_override='dr-az-apne1az1-example',
    )
    write_outputs(plan, report, 'az-switchover-apne1-az1')

    # ── Example 2: AZ 级切换（排除 petfood）──
    print("\n📋 Example 2: AZ-level switchover with exclusion")
    print("   Scenario: same as above but excluding petfood service")
    plan, report = generate_plan_from_data(
        AZ1_NODES, AZ1_EDGES,
        scope='az', source='apne1-az1', target='apne1-az2,apne1-az4',
        exclude=['petfood'],
        plan_id_override='dr-az-apne1az1-no-petfood',
    )
    write_outputs(plan, report, 'az-switchover-exclude-petfood')

    # ── Example 3: Region 级切换 ──
    print("\n📋 Example 3: Region-level switchover")
    print("   Scenario: ap-northeast-1 total outage → failover to us-west-2")
    plan, report = generate_plan_from_data(
        REGION_NODES, REGION_EDGES,
        scope='region', source='ap-northeast-1', target='us-west-2',
        plan_id_override='dr-region-apne1-to-usw2',
    )
    write_outputs(plan, report, 'region-switchover-apne1-to-usw2')

    print(f"\n{'=' * 60}")
    print(f"  All examples generated in: {OUT_DIR}/")
    print(f"{'=' * 60}")
