# DR Switchover Plan — REGION Level

> Generated: 2026-03-28T13:17:16.030131+00:00
> Failure scope: ap-northeast-1 → DR target: us-west-2
> Estimated RTO: 55 minutes
> Estimated RPO: 15 minutes
> Graph snapshot: 2026-03-28T13:17:16.030131+00:00
> Plan ID: `dr-region-apne1-to-usw2`

## Impact Summary

| Dimension | Value |
|-----------|-------|
| Affected services | 7 |
| Affected resources | 22 |
| Tier0 services | 7 |
| Total phases | 5 |
| Total steps | 28 |

### Single Point of Failure Risks

- **petsite-db** (RDSCluster) — Only in `apne1-az1`, affects 4 service(s)

## phase-0: Pre-flight Check

**Estimated duration**: 1 min
**Gate condition**: All preflight checks passed, replication lag within threshold

### Step phase-0.1: `check_target_connectivity` — us-west-2

**Resource type**: AWS
**Estimated time**: 10s

**Command**:
```bash
aws sts get-caller-identity --region us-west-2
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
aws rds describe-db-clusters --db-cluster-identifier petsite-db --region ap-northeast-1 --query 'DBClusters[0].ReplicationSourceIdentifier'
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

### Step phase-0.3: `check_replication_lag` — petsite-db-replica [Tier0]

**Resource type**: RDSCluster
**Estimated time**: 15s

**Command**:
```bash
aws rds describe-db-clusters --db-cluster-identifier petsite-db-replica --region ap-northeast-1 --query 'DBClusters[0].ReplicationSourceIdentifier'
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

### Step phase-0.4: `lower_dns_ttl` — dns-ttl

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

**Estimated duration**: 17 min
**Gate condition**: All data stores reachable and writable in target region

### Step phase-1.1: `switch_global_table_region` — petsearch-db (requires approval) [Tier0]

**Resource type**: DynamoDBTable
**Estimated time**: 60s

**Command**:
```bash
# DynamoDB Global Table: switch write endpoint to us-west-2
# Update application config (env var / Parameter Store)
aws ssm put-parameter --name '/petsite/dynamodb-region' --value 'us-west-2' --overwrite --region us-west-2
```

**Validation**:
```bash
aws dynamodb describe-table --table-name petsearch-db --region us-west-2 --query 'Table.TableStatus' --output text
```
Expected result: `ACTIVE`

**Rollback**:
```bash
aws ssm put-parameter --name '/petsite/dynamodb-region' --value 'ap-northeast-1' --overwrite --region ap-northeast-1
```

### Step phase-1.2: `promote_read_replica` — petsite-db-replica (requires approval) [Tier0]

**Resource type**: RDSCluster
**Estimated time**: 300s

**Command**:
```bash
aws rds failover-db-cluster --db-cluster-identifier petsite-db-replica --region us-west-2
```

**Validation**:
```bash
aws rds describe-db-clusters --db-cluster-identifier petsite-db-replica --region us-west-2 --query 'DBClusters[0].Status' --output text
```
Expected result: `available`

**Rollback**:
```bash
aws rds failover-db-cluster --db-cluster-identifier petsite-db-replica --region ap-northeast-1
```

### Step phase-1.3: `promote_read_replica` — petsite-db (requires approval) [Tier0]

**Resource type**: RDSCluster
**Estimated time**: 300s

**Command**:
```bash
aws rds failover-db-cluster --db-cluster-identifier petsite-db --region us-west-2
```

**Validation**:
```bash
aws rds describe-db-clusters --db-cluster-identifier petsite-db --region us-west-2 --query 'DBClusters[0].Status' --output text
```
Expected result: `available`

**Rollback**:
```bash
aws rds failover-db-cluster --db-cluster-identifier petsite-db --region ap-northeast-1
```

### Step phase-1.4: `manual_switchover` — pethistory-queue (requires approval) [Tier1]

**Resource type**: SQSQueue
**Estimated time**: 120s

**Command**:
```bash
# TODO: Manual switchover required for SQSQueue 'pethistory-queue'
# Source: ap-northeast-1 → Target: us-west-2
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

### Step phase-1.5: `manual_switchover` — petsite-neptune (requires approval) [Tier1]

**Resource type**: NeptuneCluster
**Estimated time**: 120s

**Command**:
```bash
# TODO: Manual switchover required for NeptuneCluster 'petsite-neptune'
# Source: ap-northeast-1 → Target: us-west-2
# Add the appropriate AWS CLI command here.
```

**Validation**:
```bash
# TODO: Add verification command for petsite-neptune
```
Expected result: `Resource healthy in target`

**Rollback**:
```bash
# TODO: Add rollback command for petsite-neptune
```

### Step phase-1.6: `manual_switchover` — petsite-incidents (requires approval) [Tier2]

**Resource type**: S3Bucket
**Estimated time**: 120s

**Command**:
```bash
# TODO: Manual switchover required for S3Bucket 'petsite-incidents'
# Source: ap-northeast-1 → Target: us-west-2
# Add the appropriate AWS CLI command here.
```

**Validation**:
```bash
# TODO: Add verification command for petsite-incidents
```
Expected result: `Resource healthy in target`

**Rollback**:
```bash
# TODO: Add rollback command for petsite-incidents
```

## phase-2: Compute Layer Activation

**Estimated duration**: 21 min
**Gate condition**: All Tier0 services healthy in target

### Step phase-2.1: `manual_switchover` — petsite-ec2-3 (requires approval)

**Resource type**: EC2Instance
**Estimated time**: 120s

**Command**:
```bash
# TODO: Manual switchover required for EC2Instance 'petsite-ec2-3'
# Source: ap-northeast-1 → Target: us-west-2
# Add the appropriate AWS CLI command here.
```

**Validation**:
```bash
# TODO: Add verification command for petsite-ec2-3
```
Expected result: `Resource healthy in target`

**Rollback**:
```bash
# TODO: Add rollback command for petsite-ec2-3
```

### Step phase-2.2: `manual_switchover` — petsite-ec2-1 (requires approval)

**Resource type**: EC2Instance
**Estimated time**: 120s

**Command**:
```bash
# TODO: Manual switchover required for EC2Instance 'petsite-ec2-1'
# Source: ap-northeast-1 → Target: us-west-2
# Add the appropriate AWS CLI command here.
```

**Validation**:
```bash
# TODO: Add verification command for petsite-ec2-1
```
Expected result: `Resource healthy in target`

**Rollback**:
```bash
# TODO: Add rollback command for petsite-ec2-1
```

### Step phase-2.3: `manual_switchover` — petsite-ec2-2 (requires approval)

**Resource type**: EC2Instance
**Estimated time**: 120s

**Command**:
```bash
# TODO: Manual switchover required for EC2Instance 'petsite-ec2-2'
# Source: ap-northeast-1 → Target: us-west-2
# Add the appropriate AWS CLI command here.
```

**Validation**:
```bash
# TODO: Add verification command for petsite-ec2-2
```
Expected result: `Resource healthy in target`

**Rollback**:
```bash
# TODO: Add rollback command for petsite-ec2-2
```

### Step phase-2.4: `verify_k8s_service_endpoints` — petsite-svc [Tier0]

**Resource type**: K8sService
**Estimated time**: 60s

**Command**:
```bash
kubectl get endpoints petsite-svc --context us-west-2-cluster
kubectl describe service petsite-svc --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get endpoints petsite-svc --context us-west-2-cluster -o jsonpath='{.subsets[0].addresses[0].ip}'
```
Expected result: `<non-empty IP>`

**Rollback**:
```bash
kubectl delete endpoints petsite-svc --context us-west-2-cluster
# Service endpoints will repopulate from source cluster
```

### Step phase-2.5: `verify_k8s_service_endpoints` — petsearch-svc [Tier0]

**Resource type**: K8sService
**Estimated time**: 60s

**Command**:
```bash
kubectl get endpoints petsearch-svc --context us-west-2-cluster
kubectl describe service petsearch-svc --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get endpoints petsearch-svc --context us-west-2-cluster -o jsonpath='{.subsets[0].addresses[0].ip}'
```
Expected result: `<non-empty IP>`

**Rollback**:
```bash
kubectl delete endpoints petsearch-svc --context us-west-2-cluster
# Service endpoints will repopulate from source cluster
```

### Step phase-2.6: `scale_up_and_verify` — petsite (requires approval) [Tier0]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petsite --replicas=3 --context us-west-2-cluster
kubectl rollout status deployment/petsite --timeout=120s --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get deployment petsite --context us-west-2-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `3`

**Rollback**:
```bash
kubectl scale deployment petsite --replicas=0 --context us-west-2-cluster
```

### Step phase-2.7: `scale_up_and_verify` — petsearch (requires approval) [Tier0]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petsearch --replicas=3 --context us-west-2-cluster
kubectl rollout status deployment/petsearch --timeout=120s --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get deployment petsearch --context us-west-2-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `3`

**Rollback**:
```bash
kubectl scale deployment petsearch --replicas=0 --context us-west-2-cluster
```

### Step phase-2.8: `scale_up_and_verify` — pethistory [Tier1]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment pethistory --replicas=3 --context us-west-2-cluster
kubectl rollout status deployment/pethistory --timeout=120s --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get deployment pethistory --context us-west-2-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `3`

**Rollback**:
```bash
kubectl scale deployment pethistory --replicas=0 --context us-west-2-cluster
```

### Step phase-2.9: `manual_switchover` — pet-stepfn-adoption (requires approval) [Tier1]

**Resource type**: StepFunction
**Estimated time**: 120s

**Command**:
```bash
# TODO: Manual switchover required for StepFunction 'pet-stepfn-adoption'
# Source: ap-northeast-1 → Target: us-west-2
# Add the appropriate AWS CLI command here.
```

**Validation**:
```bash
# TODO: Add verification command for pet-stepfn-adoption
```
Expected result: `Resource healthy in target`

**Rollback**:
```bash
# TODO: Add rollback command for pet-stepfn-adoption
```

### Step phase-2.10: `scale_up_and_verify` — payforadoption [Tier1]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment payforadoption --replicas=3 --context us-west-2-cluster
kubectl rollout status deployment/payforadoption --timeout=120s --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get deployment payforadoption --context us-west-2-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `3`

**Rollback**:
```bash
kubectl scale deployment payforadoption --replicas=0 --context us-west-2-cluster
```

### Step phase-2.11: `scale_up_and_verify` — petfood [Tier2]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petfood --replicas=3 --context us-west-2-cluster
kubectl rollout status deployment/petfood --timeout=120s --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get deployment petfood --context us-west-2-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `3`

**Rollback**:
```bash
kubectl scale deployment petfood --replicas=0 --context us-west-2-cluster
```

### Step phase-2.12: `verify_lambda_function` — petstatusupdater [Tier2]

**Resource type**: LambdaFunction
**Estimated time**: 30s

**Command**:
```bash
aws lambda invoke --function-name petstatusupdater --region us-west-2 --payload '{"source": "dr-healthcheck"}' /tmp/petstatusupdater-response.json
cat /tmp/petstatusupdater-response.json
```

**Validation**:
```bash
aws lambda get-function-configuration --function-name petstatusupdater --region us-west-2 --query 'State' --output text
```
Expected result: `Active`

**Rollback**:
```bash
# Lambda functions are stateless; update event source mapping
aws lambda update-event-source-mapping --region ap-northeast-1 --uuid $EVENT_SOURCE_UUID --enabled
```

### Step phase-2.13: `verify_lambda_function` — petadoption-lambda [Tier2]

**Resource type**: LambdaFunction
**Estimated time**: 30s

**Command**:
```bash
aws lambda invoke --function-name petadoption-lambda --region us-west-2 --payload '{"source": "dr-healthcheck"}' /tmp/petadoption-lambda-response.json
cat /tmp/petadoption-lambda-response.json
```

**Validation**:
```bash
aws lambda get-function-configuration --function-name petadoption-lambda --region us-west-2 --query 'State' --output text
```
Expected result: `Active`

**Rollback**:
```bash
# Lambda functions are stateless; update event source mapping
aws lambda update-event-source-mapping --region ap-northeast-1 --uuid $EVENT_SOURCE_UUID --enabled
```

## phase-3: Network / Traffic Layer Cutover

**Estimated duration**: 8 min
**Gate condition**: End-user traffic routed to target; DNS propagated

### Step phase-3.1: `verify_health_and_switch_dns` — petsite-cdn (requires approval)

**Resource type**: LoadBalancer
**Estimated time**: 180s

**Command**:
```bash
# 1. Verify target ALB health
aws elbv2 describe-target-health --target-group-arn $TG_ARN --region us-west-2
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

### Step phase-3.2: `verify_health_and_switch_dns` — petsite-alb (requires approval)

**Resource type**: LoadBalancer
**Estimated time**: 180s

**Command**:
```bash
# 1. Verify target ALB health
aws elbv2 describe-target-health --target-group-arn $TG_ARN --region us-west-2
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

### Step phase-3.3: `manual_switchover` — petsite-tg (requires approval)

**Resource type**: TargetGroup
**Estimated time**: 120s

**Command**:
```bash
# TODO: Manual switchover required for TargetGroup 'petsite-tg'
# Source: ap-northeast-1 → Target: us-west-2
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

**Estimated duration**: 8 min
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

### Step rollback-phase-3.3: `rollback_verify_health_and_switch_dns` — petsite-cdn (requires approval)

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
Expected result: `Original state of petsite-cdn`

**Rollback**:
```bash
# Manual intervention required
```

## rollback-phase-2: Rollback: Compute Layer Activation

**Estimated duration**: 21 min
**Gate condition**: All Compute Layer Activation rollback steps verified

### Step rollback-phase-2.1: `rollback_verify_lambda_function` — petadoption-lambda (requires approval) [Tier2]

**Resource type**: LambdaFunction
**Estimated time**: 30s

**Command**:
```bash
# Lambda functions are stateless; update event source mapping
aws lambda update-event-source-mapping --region ap-northeast-1 --uuid $EVENT_SOURCE_UUID --enabled
```

**Validation**:
```bash
aws lambda get-function-configuration --function-name petadoption-lambda --region us-west-2 --query 'State' --output text
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
aws lambda update-event-source-mapping --region ap-northeast-1 --uuid $EVENT_SOURCE_UUID --enabled
```

**Validation**:
```bash
aws lambda get-function-configuration --function-name petstatusupdater --region us-west-2 --query 'State' --output text
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
kubectl scale deployment petfood --replicas=0 --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get deployment petfood --context us-west-2-cluster -o jsonpath='{.status.readyReplicas}'
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
kubectl scale deployment payforadoption --replicas=0 --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get deployment payforadoption --context us-west-2-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `Original state of payforadoption`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.5: `rollback_manual_switchover` — pet-stepfn-adoption (requires approval) [Tier1]

**Resource type**: StepFunction
**Estimated time**: 120s

**Command**:
```bash
# TODO: Add rollback command for pet-stepfn-adoption
```

**Validation**:
```bash
# TODO: Add verification command for pet-stepfn-adoption
```
Expected result: `Original state of pet-stepfn-adoption`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.6: `rollback_scale_up_and_verify` — pethistory (requires approval) [Tier1]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment pethistory --replicas=0 --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get deployment pethistory --context us-west-2-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `Original state of pethistory`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.7: `rollback_scale_up_and_verify` — petsearch (requires approval) [Tier0]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petsearch --replicas=0 --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get deployment petsearch --context us-west-2-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `Original state of petsearch`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.8: `rollback_scale_up_and_verify` — petsite (requires approval) [Tier0]

**Resource type**: Microservice
**Estimated time**: 120s

**Command**:
```bash
kubectl scale deployment petsite --replicas=0 --context us-west-2-cluster
```

**Validation**:
```bash
kubectl get deployment petsite --context us-west-2-cluster -o jsonpath='{.status.readyReplicas}'
```
Expected result: `Original state of petsite`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.9: `rollback_verify_k8s_service_endpoints` — petsearch-svc (requires approval) [Tier0]

**Resource type**: K8sService
**Estimated time**: 60s

**Command**:
```bash
kubectl delete endpoints petsearch-svc --context us-west-2-cluster
# Service endpoints will repopulate from source cluster
```

**Validation**:
```bash
kubectl get endpoints petsearch-svc --context us-west-2-cluster -o jsonpath='{.subsets[0].addresses[0].ip}'
```
Expected result: `Original state of petsearch-svc`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.10: `rollback_verify_k8s_service_endpoints` — petsite-svc (requires approval) [Tier0]

**Resource type**: K8sService
**Estimated time**: 60s

**Command**:
```bash
kubectl delete endpoints petsite-svc --context us-west-2-cluster
# Service endpoints will repopulate from source cluster
```

**Validation**:
```bash
kubectl get endpoints petsite-svc --context us-west-2-cluster -o jsonpath='{.subsets[0].addresses[0].ip}'
```
Expected result: `Original state of petsite-svc`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.11: `rollback_manual_switchover` — petsite-ec2-2 (requires approval)

**Resource type**: EC2Instance
**Estimated time**: 120s

**Command**:
```bash
# TODO: Add rollback command for petsite-ec2-2
```

**Validation**:
```bash
# TODO: Add verification command for petsite-ec2-2
```
Expected result: `Original state of petsite-ec2-2`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.12: `rollback_manual_switchover` — petsite-ec2-1 (requires approval)

**Resource type**: EC2Instance
**Estimated time**: 120s

**Command**:
```bash
# TODO: Add rollback command for petsite-ec2-1
```

**Validation**:
```bash
# TODO: Add verification command for petsite-ec2-1
```
Expected result: `Original state of petsite-ec2-1`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-2.13: `rollback_manual_switchover` — petsite-ec2-3 (requires approval)

**Resource type**: EC2Instance
**Estimated time**: 120s

**Command**:
```bash
# TODO: Add rollback command for petsite-ec2-3
```

**Validation**:
```bash
# TODO: Add verification command for petsite-ec2-3
```
Expected result: `Original state of petsite-ec2-3`

**Rollback**:
```bash
# Manual intervention required
```

## rollback-phase-1: Rollback: Data Layer Switchover

**Estimated duration**: 17 min
**Gate condition**: All Data Layer Switchover rollback steps verified

### Step rollback-phase-1.1: `rollback_manual_switchover` — petsite-incidents (requires approval) [Tier2]

**Resource type**: S3Bucket
**Estimated time**: 120s

**Command**:
```bash
# TODO: Add rollback command for petsite-incidents
```

**Validation**:
```bash
# TODO: Add verification command for petsite-incidents
```
Expected result: `Original state of petsite-incidents`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-1.2: `rollback_manual_switchover` — petsite-neptune (requires approval) [Tier1]

**Resource type**: NeptuneCluster
**Estimated time**: 120s

**Command**:
```bash
# TODO: Add rollback command for petsite-neptune
```

**Validation**:
```bash
# TODO: Add verification command for petsite-neptune
```
Expected result: `Original state of petsite-neptune`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-1.3: `rollback_manual_switchover` — pethistory-queue (requires approval) [Tier1]

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

### Step rollback-phase-1.4: `rollback_promote_read_replica` — petsite-db (requires approval) [Tier0]

**Resource type**: RDSCluster
**Estimated time**: 300s

**Command**:
```bash
aws rds failover-db-cluster --db-cluster-identifier petsite-db --region ap-northeast-1
```

**Validation**:
```bash
aws rds describe-db-clusters --db-cluster-identifier petsite-db --region us-west-2 --query 'DBClusters[0].Status' --output text
```
Expected result: `Original state of petsite-db`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-1.5: `rollback_promote_read_replica` — petsite-db-replica (requires approval) [Tier0]

**Resource type**: RDSCluster
**Estimated time**: 300s

**Command**:
```bash
aws rds failover-db-cluster --db-cluster-identifier petsite-db-replica --region ap-northeast-1
```

**Validation**:
```bash
aws rds describe-db-clusters --db-cluster-identifier petsite-db-replica --region us-west-2 --query 'DBClusters[0].Status' --output text
```
Expected result: `Original state of petsite-db-replica`

**Rollback**:
```bash
# Manual intervention required
```

### Step rollback-phase-1.6: `rollback_switch_global_table_region` — petsearch-db (requires approval) [Tier0]

**Resource type**: DynamoDBTable
**Estimated time**: 60s

**Command**:
```bash
aws ssm put-parameter --name '/petsite/dynamodb-region' --value 'ap-northeast-1' --overwrite --region ap-northeast-1
```

**Validation**:
```bash
aws dynamodb describe-table --table-name petsearch-db --region us-west-2 --query 'Table.TableStatus' --output text
```
Expected result: `Original state of petsearch-db`

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
aws sts get-caller-identity --region ap-northeast-1
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
