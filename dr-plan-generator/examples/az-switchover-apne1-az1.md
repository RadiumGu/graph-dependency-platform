# DR Switchover Plan — AZ Level

> Generated: 2026-03-28T11:53:54.550938+00:00
> Failure scope: apne1-az1 → DR target: apne1-az2,apne1-az4
> Estimated RTO: 34 minutes
> Estimated RPO: 15 minutes
> Graph snapshot: 2026-03-28T11:53:54.550938+00:00
> Plan ID: `dr-az-apne1az1-example`

## Impact Summary

| Dimension | Value |
|-----------|-------|
| Affected services | 7 |
| Affected resources | 14 |
| Tier0 services | 6 |
| Total phases | 5 |
| Total steps | 19 |

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

**Estimated duration**: 8 min
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

### Step phase-1.2: `switch_global_table_region` — petsearch-db (requires approval) [Tier0]

**Resource type**: DynamoDBTable
**Estimated time**: 60s

**Command**:
```bash
# DynamoDB Global Table: switch write endpoint to apne1-az2,apne1-az4
# Update application config (env var / Parameter Store)
aws ssm put-parameter --name '/petsite/dynamodb-region' --value 'apne1-az2,apne1-az4' --overwrite --region apne1-az2,apne1-az4
```

**Validation**:
```bash
aws dynamodb describe-table --table-name petsearch-db --region apne1-az2,apne1-az4 --query 'Table.TableStatus' --output text
```
Expected result: `ACTIVE`

**Rollback**:
```bash
aws ssm put-parameter --name '/petsite/dynamodb-region' --value 'apne1-az1' --overwrite --region apne1-az1
```

### Step phase-1.3: `manual_switchover` — pethistory-queue (requires approval) [Tier1]

**Resource type**: SQSQueue
**Estimated time**: 120s

**Command**:
```bash
# TODO: Manual switchover required for SQSQueue 'pethistory-queue'
# Source: apne1-az1 → Target: apne1-az2,apne1-az4
# Add the appropriate AWS CLI command here.
```

**Validation**:
```bash
# TODO: Add verification command for pethistory-queue
```
Expected result: `Resource healthy in target`

**Rollback**:
```bash
# TODO: Add rollback command for pethistory-queue
```

## phase-2: Compute Layer Activation

**Estimated duration**: 13 min
**Gate condition**: All Tier0 services healthy in target

### Step phase-2.1: `verify_k8s_service_endpoints` — petsearch-svc [Tier0]

**Resource type**: K8sService
**Estimated time**: 60s

**Command**:
```bash
kubectl get endpoints petsearch-svc --context apne1-az2,apne1-az4-cluster
kubectl describe service petsearch-svc --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get endpoints petsearch-svc --context apne1-az2,apne1-az4-cluster -o jsonpath='{.subsets[0].addresses[0].ip}'
```
Expected result: `<non-empty IP>`

**Rollback**:
```bash
kubectl delete endpoints petsearch-svc --context apne1-az2,apne1-az4-cluster
# Service endpoints will repopulate from source cluster
```

### Step phase-2.2: `verify_k8s_service_endpoints` — petsite-svc [Tier0]

**Resource type**: K8sService
**Estimated time**: 60s

**Command**:
```bash
kubectl get endpoints petsite-svc --context apne1-az2,apne1-az4-cluster
kubectl describe service petsite-svc --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get endpoints petsite-svc --context apne1-az2,apne1-az4-cluster -o jsonpath='{.subsets[0].addresses[0].ip}'
```
Expected result: `<non-empty IP>`

**Rollback**:
```bash
kubectl delete endpoints petsite-svc --context apne1-az2,apne1-az4-cluster
# Service endpoints will repopulate from source cluster
```

### Step phase-2.3: `scale_up_and_verify` — petsite (requires approval) [Tier0]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petsite --replicas=3 --context apne1-az2,apne1-az4-cluster
kubectl rollout status deployment/petsite --timeout=120s --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get deployment petsite --context apne1-az2,apne1-az4-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `3`

**Rollback**:
```bash
kubectl scale deployment petsite --replicas=0 --context apne1-az2,apne1-az4-cluster
```

### Step phase-2.4: `scale_up_and_verify` — petsearch (requires approval) [Tier0]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petsearch --replicas=3 --context apne1-az2,apne1-az4-cluster
kubectl rollout status deployment/petsearch --timeout=120s --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get deployment petsearch --context apne1-az2,apne1-az4-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `3`

**Rollback**:
```bash
kubectl scale deployment petsearch --replicas=0 --context apne1-az2,apne1-az4-cluster
```

### Step phase-2.5: `scale_up_and_verify` — pethistory [Tier1]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment pethistory --replicas=3 --context apne1-az2,apne1-az4-cluster
kubectl rollout status deployment/pethistory --timeout=120s --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get deployment pethistory --context apne1-az2,apne1-az4-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `3`

**Rollback**:
```bash
kubectl scale deployment pethistory --replicas=0 --context apne1-az2,apne1-az4-cluster
```

### Step phase-2.6: `scale_up_and_verify` — payforadoption [Tier1]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment payforadoption --replicas=3 --context apne1-az2,apne1-az4-cluster
kubectl rollout status deployment/payforadoption --timeout=120s --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get deployment payforadoption --context apne1-az2,apne1-az4-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `3`

**Rollback**:
```bash
kubectl scale deployment payforadoption --replicas=0 --context apne1-az2,apne1-az4-cluster
```

### Step phase-2.7: `scale_up_and_verify` — petfood [Tier2]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petfood --replicas=3 --context apne1-az2,apne1-az4-cluster
kubectl rollout status deployment/petfood --timeout=120s --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get deployment petfood --context apne1-az2,apne1-az4-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `3`

**Rollback**:
```bash
kubectl scale deployment petfood --replicas=0 --context apne1-az2,apne1-az4-cluster
```

### Step phase-2.8: `verify_lambda_function` — petstatusupdater [Tier2]

**Resource type**: LambdaFunction
**Estimated time**: 30s

**Command**:
```bash
aws lambda invoke --function-name petstatusupdater --region apne1-az2,apne1-az4 --payload '{"source": "dr-healthcheck"}' /tmp/petstatusupdater-response.json
cat /tmp/petstatusupdater-response.json
```

**Validation**:
```bash
aws lambda get-function-configuration --function-name petstatusupdater --region apne1-az2,apne1-az4 --query 'State' --output text
```
Expected result: `Active`

**Rollback**:
```bash
# Lambda functions are stateless; update event source mapping
aws lambda update-event-source-mapping --region apne1-az1 --uuid $EVENT_SOURCE_UUID --enabled
```

### Step phase-2.9: `verify_lambda_function` — petadoption-lambda [Tier2]

**Resource type**: LambdaFunction
**Estimated time**: 30s

**Command**:
```bash
aws lambda invoke --function-name petadoption-lambda --region apne1-az2,apne1-az4 --payload '{"source": "dr-healthcheck"}' /tmp/petadoption-lambda-response.json
cat /tmp/petadoption-lambda-response.json
```

**Validation**:
```bash
aws lambda get-function-configuration --function-name petadoption-lambda --region apne1-az2,apne1-az4 --query 'State' --output text
```
Expected result: `Active`

**Rollback**:
```bash
# Lambda functions are stateless; update event source mapping
aws lambda update-event-source-mapping --region apne1-az1 --uuid $EVENT_SOURCE_UUID --enabled
```

## phase-3: Network / Traffic Layer Cutover

**Estimated duration**: 5 min
**Gate condition**: End-user traffic routed to target; DNS propagated

### Step phase-3.1: `verify_health_and_switch_dns` — petsite-alb (requires approval)

**Resource type**: LoadBalancer
**Estimated time**: 180s

**Command**:
```bash
# 1. Verify target ALB health
aws elbv2 describe-target-health --target-group-arn $TG_ARN --region apne1-az2,apne1-az4
# 2. Switch Route 53 DNS
aws route53 change-resource-record-sets --hosted-zone-id $ZONE_ID --change-batch file://dns-failover.json
```

**Validation**:
```bash
dig +short petsite.example.com
```
Expected result: `<target ALB DNS>`

**Rollback**:
```bash
aws route53 change-resource-record-sets --hosted-zone-id $ZONE_ID --change-batch file://dns-rollback.json
```

### Step phase-3.2: `manual_switchover` — petsite-tg (requires approval)

**Resource type**: TargetGroup
**Estimated time**: 120s

**Command**:
```bash
# TODO: Manual switchover required for TargetGroup 'petsite-tg'
# Source: apne1-az1 → Target: apne1-az2,apne1-az4
# Add the appropriate AWS CLI command here.
```

**Validation**:
```bash
# TODO: Add verification command for petsite-tg
```
Expected result: `Resource healthy in target`

**Rollback**:
```bash
# TODO: Add rollback command for petsite-tg
```

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

**Estimated duration**: 5 min
**Gate condition**: All Network / Traffic Layer Cutover rollback steps verified

### Step rollback-phase-3.1: `rollback_manual_switchover` — petsite-tg (requires approval)

**Resource type**: TargetGroup
**Estimated time**: 120s

**Command**:
```bash
# TODO: Add rollback command for petsite-tg
```

**Validation**:
```bash
# TODO: Add verification command for petsite-tg
```
Expected result: `Original state of petsite-tg`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-3.2: `rollback_verify_health_and_switch_dns` — petsite-alb (requires approval)

**Resource type**: LoadBalancer
**Estimated time**: 180s

**Command**:
```bash
aws route53 change-resource-record-sets --hosted-zone-id $ZONE_ID --change-batch file://dns-rollback.json
```

**Validation**:
```bash
dig +short petsite.example.com
```
Expected result: `Original state of petsite-alb`

**Rollback**:
```bash
# Manual intervention required
```

## rollback-phase-2: Rollback: Compute Layer Activation

**Estimated duration**: 13 min
**Gate condition**: All Compute Layer Activation rollback steps verified

### Step rollback-phase-2.1: `rollback_verify_lambda_function` — petadoption-lambda (requires approval) [Tier2]

**Resource type**: LambdaFunction
**Estimated time**: 30s

**Command**:
```bash
# Lambda functions are stateless; update event source mapping
aws lambda update-event-source-mapping --region apne1-az1 --uuid $EVENT_SOURCE_UUID --enabled
```

**Validation**:
```bash
aws lambda get-function-configuration --function-name petadoption-lambda --region apne1-az2,apne1-az4 --query 'State' --output text
```
Expected result: `Original state of petadoption-lambda`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.2: `rollback_verify_lambda_function` — petstatusupdater (requires approval) [Tier2]

**Resource type**: LambdaFunction
**Estimated time**: 30s

**Command**:
```bash
# Lambda functions are stateless; update event source mapping
aws lambda update-event-source-mapping --region apne1-az1 --uuid $EVENT_SOURCE_UUID --enabled
```

**Validation**:
```bash
aws lambda get-function-configuration --function-name petstatusupdater --region apne1-az2,apne1-az4 --query 'State' --output text
```
Expected result: `Original state of petstatusupdater`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.3: `rollback_scale_up_and_verify` — petfood (requires approval) [Tier2]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petfood --replicas=0 --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get deployment petfood --context apne1-az2,apne1-az4-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `Original state of petfood`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.4: `rollback_scale_up_and_verify` — payforadoption (requires approval) [Tier1]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment payforadoption --replicas=0 --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get deployment payforadoption --context apne1-az2,apne1-az4-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `Original state of payforadoption`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.5: `rollback_scale_up_and_verify` — pethistory (requires approval) [Tier1]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment pethistory --replicas=0 --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get deployment pethistory --context apne1-az2,apne1-az4-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `Original state of pethistory`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.6: `rollback_scale_up_and_verify` — petsearch (requires approval) [Tier0]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petsearch --replicas=0 --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get deployment petsearch --context apne1-az2,apne1-az4-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `Original state of petsearch`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.7: `rollback_scale_up_and_verify` — petsite (requires approval) [Tier0]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petsite --replicas=0 --context apne1-az2,apne1-az4-cluster
```

**Validation**:
```bash
kubectl get deployment petsite --context apne1-az2,apne1-az4-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `Original state of petsite`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.8: `rollback_verify_k8s_service_endpoints` — petsite-svc (requires approval) [Tier0]

**Resource type**: K8sService
**Estimated time**: 60s

**Command**:
```bash
kubectl delete endpoints petsite-svc --context apne1-az2,apne1-az4-cluster
# Service endpoints will repopulate from source cluster
```

**Validation**:
```bash
kubectl get endpoints petsite-svc --context apne1-az2,apne1-az4-cluster -o jsonpath='{.subsets[0].addresses[0].ip}'
```
Expected result: `Original state of petsite-svc`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.9: `rollback_verify_k8s_service_endpoints` — petsearch-svc (requires approval) [Tier0]

**Resource type**: K8sService
**Estimated time**: 60s

**Command**:
```bash
kubectl delete endpoints petsearch-svc --context apne1-az2,apne1-az4-cluster
# Service endpoints will repopulate from source cluster
```

**Validation**:
```bash
kubectl get endpoints petsearch-svc --context apne1-az2,apne1-az4-cluster -o jsonpath='{.subsets[0].addresses[0].ip}'
```
Expected result: `Original state of petsearch-svc`

**Rollback**:
```bash
# Manual intervention required
```

## rollback-phase-1: Rollback: Data Layer Switchover

**Estimated duration**: 8 min
**Gate condition**: All Data Layer Switchover rollback steps verified

### Step rollback-phase-1.1: `rollback_manual_switchover` — pethistory-queue (requires approval) [Tier1]

**Resource type**: SQSQueue
**Estimated time**: 120s

**Command**:
```bash
# TODO: Add rollback command for pethistory-queue
```

**Validation**:
```bash
# TODO: Add verification command for pethistory-queue
```
Expected result: `Original state of pethistory-queue`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-1.2: `rollback_switch_global_table_region` — petsearch-db (requires approval) [Tier0]

**Resource type**: DynamoDBTable
**Estimated time**: 60s

**Command**:
```bash
aws ssm put-parameter --name '/petsite/dynamodb-region' --value 'apne1-az1' --overwrite --region apne1-az1
```

**Validation**:
```bash
aws dynamodb describe-table --table-name petsearch-db --region apne1-az2,apne1-az4 --query 'Table.TableStatus' --output text
```
Expected result: `Original state of petsearch-db`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-1.3: `rollback_promote_read_replica` — petsite-db (requires approval) [Tier0]

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
