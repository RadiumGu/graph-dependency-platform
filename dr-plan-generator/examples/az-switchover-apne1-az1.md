# DR Switchover Plan — AZ Level

> Generated: 2026-03-28T13:22:04.313305+00:00
> Failure scope: apne1-az1 → DR target: apne1-az2,apne1-az4
> Estimated RTO: 13 minutes
> Estimated RPO: 15 minutes
> Graph snapshot: 2026-03-28T13:22:04.313305+00:00
> Plan ID: `dr-az-apne1az1-example`

## Impact Summary

**Risk level**: 🔴 HIGH  
**Scope**: AZ — `apne1-az1` → `apne1-az2,apne1-az4`  
**Estimated RTO**: 13 min | **RPO**: 15 min

| Dimension | Value |
|-----------|-------|
| Affected services | 7 |
| Affected resources | 14 |
| Tier0 (critical) | 6 |
| Tier1 (important) | 3 |
| Tier2 (standard) | 3 |
| Switchover steps | 6 |
| Rollback steps | 2 |

### Tier0 Critical Services

`petsearch`, `petsearch-db`, `petsearch-svc`, `petsite`, `petsite-db`, `petsite-svc`

### Tier1 Important Services

`payforadoption`, `pethistory`, `pethistory-queue`

### Affected Resource Types

| Type | Count | Fault Domain |
|------|-------|-------------|
| Microservice | 5 | 🌐 regional |
| K8sService | 2 | 🌐 regional |
| LambdaFunction | 2 | 🌐 regional |
| RDSCluster | 1 | ⚡ zonal |
| DynamoDBTable | 1 | 🌐 regional |
| SQSQueue | 1 | 🌐 regional |
| LoadBalancer | 1 | 🌐 regional |
| TargetGroup | 1 | 🌐 regional |

> ℹ️ **7 regional/global resource type(s)** in the affected subgraph are unaffected by AZ failure and have no switchover steps: `DynamoDBTable`, `K8sService`, `LambdaFunction`, `LoadBalancer`, `Microservice`, `SQSQueue`, `TargetGroup`

### Single Point of Failure Risks

- **petsite-db** (RDSCluster) — Only in `apne1-az1`, affects 3 service(s)

## phase-0: Pre-flight Check

**Estimated duration**: 1 min
**Gate condition**: All preflight checks passed, replication lag within threshold

### Step phase-0.1: `check_target_connectivity` — apne1-az2,apne1-az4

**Resource type**: AWS
**Estimated time**: 10s

**Command**:
```bash
aws sts get-caller-identity --region apne1-az2,apne1-az4
```

**Validation**:
```bash
echo $?
```
Expected result: `0`

**Rollback**:
```bash
# No rollback needed for connectivity check
```

### Step phase-0.2: `check_replication_lag` — petsite-db [Tier0]

**Resource type**: RDSCluster
**Estimated time**: 15s

**Command**:
```bash
aws rds describe-db-clusters --db-cluster-identifier petsite-db --region apne1-az1 --query 'DBClusters[0].ReplicationSourceIdentifier'
```

**Validation**:
```bash
# ReplicaLag should be < 1000ms
```
Expected result: `ReplicaLag < 1000ms`

**Rollback**:
```bash
# No rollback needed for lag check
```

### Step phase-0.3: `lower_dns_ttl` — dns-ttl

**Resource type**: Route53
**Estimated time**: 30s

**Command**:
```bash
aws route53 change-resource-record-sets --hosted-zone-id $ZONE_ID --change-batch '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"TTL":60}}]}'
```

**Validation**:
```bash
aws route53 list-resource-record-sets --hosted-zone-id $ZONE_ID --query 'ResourceRecordSets[?Name==`petsite.example.com.`].TTL'
```
Expected result: `60`

**Rollback**:
```bash
aws route53 change-resource-record-sets --hosted-zone-id $ZONE_ID --change-batch '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"TTL":300}}]}'
```

## phase-1: Data Layer Switchover

**Estimated duration**: 5 min
**Gate condition**: All data stores reachable and writable in target region

### Step phase-1.1: `promote_read_replica` — petsite-db (requires approval) [Tier0]

**Resource type**: RDSCluster
**Estimated time**: 300s

**Command**:
```bash
aws rds failover-db-cluster --db-cluster-identifier petsite-db --region apne1-az2,apne1-az4
```

**Validation**:
```bash
aws rds describe-db-clusters --db-cluster-identifier petsite-db --region apne1-az2,apne1-az4 --query 'DBClusters[0].Status' --output text
```
Expected result: `available`

**Rollback**:
```bash
aws rds failover-db-cluster --db-cluster-identifier petsite-db --region apne1-az1
```

## phase-2: Compute Layer Activation

**Estimated duration**: 1 min
**Gate condition**: All Tier0 services healthy in target

## phase-3: Network / Traffic Layer Cutover

**Estimated duration**: 1 min
**Gate condition**: End-user traffic routed to target; DNS propagated

## phase-4: Post-switchover Validation

**Estimated duration**: 3 min
**Gate condition**: All smoke tests pass and no critical alarms firing

### Step phase-4.1: `run_end_to_end_smoke_test` — e2e-smoke-test

**Resource type**: Synthetic
**Estimated time**: 120s

**Command**:
```bash
# Run end-to-end smoke test against target endpoint
curl -sf https://petsite.example.com/health | jq '.status'
```

**Validation**:
```bash
curl -sf https://petsite.example.com/health | jq '.status'
```
Expected result: `ok`

**Rollback**:
```bash
# Initiate rollback plan if validation fails
```

### Step phase-4.2: `verify_no_critical_alarms` — alarms-check

**Resource type**: CloudWatch
**Estimated time**: 60s
**Depends on**: validation-e2e

**Command**:
```bash
aws cloudwatch describe-alarms --state-value ALARM --alarm-name-prefix petsite --output table
```

**Validation**:
```bash
aws cloudwatch describe-alarms --state-value ALARM --alarm-name-prefix petsite --query 'length(MetricAlarms)' --output text
```
Expected result: `0`

**Rollback**:
```bash
# Investigate alarms before proceeding
```

---

# Rollback Plan

## rollback-phase-3: Rollback: Network / Traffic Layer Cutover

**Estimated duration**: 1 min
**Gate condition**: All Network / Traffic Layer Cutover rollback steps verified

## rollback-phase-2: Rollback: Compute Layer Activation

**Estimated duration**: 1 min
**Gate condition**: All Compute Layer Activation rollback steps verified

## rollback-phase-1: Rollback: Data Layer Switchover

**Estimated duration**: 5 min
**Gate condition**: All Data Layer Switchover rollback steps verified

### Step rollback-phase-1.1: `rollback_promote_read_replica` — petsite-db (requires approval) [Tier0]

**Resource type**: RDSCluster
**Estimated time**: 300s

**Command**:
```bash
aws rds failover-db-cluster --db-cluster-identifier petsite-db --region apne1-az1
```

**Validation**:
```bash
aws rds describe-db-clusters --db-cluster-identifier petsite-db --region apne1-az2,apne1-az4 --query 'DBClusters[0].Status' --output text
```
Expected result: `Original state of petsite-db`

**Rollback**:
```bash
# Manual intervention required
```

## rollback-phase-validation: Rollback Validation

**Estimated duration**: 2 min
**Gate condition**: Source region restored; all services healthy

### Step rollback-phase-validation.1: `verify_rollback_complete` — rollback-smoke-test (requires approval)

**Resource type**: Synthetic
**Estimated time**: 120s

**Command**:
```bash
# Verify traffic is back on original source
curl -sf https://petsite.example.com/health | jq '.status'
# Verify source region is serving requests
aws sts get-caller-identity --region apne1-az1
```

**Validation**:
```bash
curl -sf https://petsite.example.com/health | jq '.status'
```
Expected result: `ok`

**Rollback**:
```bash
# No further rollback available — escalate to incident commander
```
