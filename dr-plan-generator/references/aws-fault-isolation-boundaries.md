# AWS Fault Isolation Boundaries — Reference Summary

> Source: [AWS Fault Isolation Boundaries Whitepaper](https://docs.aws.amazon.com/whitepapers/latest/aws-fault-isolation-boundaries/aws-fault-isolation-boundaries.html)
> Published: November 16, 2022 (Amazon Web Services)
> Purpose: Guide DR plan generation with correct fault isolation assumptions

---

## 1. AWS Infrastructure Hierarchy

```
Partition (aws / aws-cn / aws-us-gov)
  └── Region (e.g. ap-northeast-1)
       └── Availability Zone (e.g. apne1-az1)
            └── Data Center(s)
```

- **Partition**: Hard IAM boundary. Cross-partition operations NOT supported.
- **Region**: Isolated from other Regions. Failures contained to single Region.
- **AZ**: Independent power, networking, connectivity. Designed for independent failure.

---

## 2. Service Scope Classification

### ⚡ THIS IS THE CRITICAL TABLE FOR DR PLAN GENERATION

| Scope | Fault Domain | Examples | AZ SPOF Risk? |
|-------|-------------|----------|---------------|
| **Zonal** | Single AZ | EC2, EBS, RDS (single-AZ), EKS nodes | **YES** — bound to specific AZ |
| **Regional** | Single Region (spans AZs) | DynamoDB, SQS, SNS, S3, Lambda, ALB/NLB, API Gateway | **NO** — AWS manages multi-AZ redundancy |
| **Global** | Partition-wide | IAM, Route 53, CloudFront, Global Accelerator | **NO** — distributed across Regions/PoPs |

### Zonal Services (AZ-bound → SPOF candidates)

Resources are deployed into a **specific AZ** and fail with that AZ:

- **Amazon EC2** instances
- **Amazon EBS** volumes
- **RDS Single-AZ** instances (NOT Multi-AZ)
- **EKS worker nodes** (EC2-based, bound to node's AZ)
- **ElastiCache** single-node
- **Neptune** single instance
- **Directory Service** (single AZ deployment)

**DR implication**: These are the primary SPOF candidates. If only deployed in one AZ, they are single points of failure.

### Regional Services (Multi-AZ by design → NOT SPOF for AZ failure)

AWS builds these on top of multiple AZs. You interact with a **single Regional endpoint**:

- **Amazon DynamoDB** — data spread across multiple AZs automatically
- **Amazon SQS** — Regional service, multi-AZ by design
- **Amazon SNS** — Regional service
- **Amazon S3** — spreads data across multiple AZs, auto-recovers from AZ loss
- **AWS Lambda** — runs across multiple AZs within the Region
- **Amazon API Gateway** — Regional endpoint
- **Elastic Load Balancing (ALB/NLB)** — distributes across AZs (but target instances are zonal!)
- **AWS Step Functions** — Regional service
- **Amazon Kinesis** — Regional service
- **Amazon EventBridge** — Regional service

**DR implication**: These services do NOT need AZ-level failover. They are NOT single-AZ SPOF risks. For Region-level DR, they need cross-Region replication or re-creation.

### Global Services (Partition-wide)

Control plane in a single Region, data plane globally distributed:

- **AWS IAM** — CP in us-east-1, DP in every Region
- **Route 53 Public DNS** — CP in us-east-1, DP in hundreds of PoPs
- **Amazon CloudFront** — CP in us-east-1, DP at edge locations
- **AWS Global Accelerator** — CP in us-west-2, DP at edge
- **AWS Organizations** — CP in us-east-1

**DR implication**: Data plane operations continue during CP failure. Do NOT depend on control plane operations (create/update/delete) in recovery path.

---

## 3. Control Plane vs Data Plane

| Aspect | Control Plane | Data Plane |
|--------|--------------|------------|
| Function | CRUDL operations (Create, Read, Update, Delete, List) | Primary service function |
| Complexity | High (workflows, business logic, databases) | Low (intentionally simple) |
| Failure rate | Higher (more moving parts) | Lower (fewer components) |
| Examples | Launch EC2 instance, create S3 bucket, describe SQS queue | Running EC2 instance, reading S3 objects, Route 53 DNS resolution |

**Critical DR principle**: 
> **Prefer data plane operations in your recovery path. Flag control plane operations as risks — especially those with cross-Region dependencies (e.g., Route 53 CP in us-east-1). Pre-provision resources when possible, but acknowledge that 100% data-plane-only recovery is an ideal, not always achievable.**

---

## 4. Static Stability — The Core DR Principle

**Definition**: System continues to work without needing dynamic changes during a failure.

**Key rules**:
1. Pre-provision enough capacity to handle AZ loss (e.g., 3 AZs × 3 instances = survive loss of 1 AZ)
2. Pre-provision all resources (ELBs, S3 buckets, DNS records) BEFORE disaster
3. Do NOT rely on auto-scaling or resource creation during recovery
4. Do NOT depend on control plane operations during failover

**Cost trade-off**: Static stability requires ~50% more capacity for single-AZ resilience (N+1 across AZs).

---

## 5. Common Anti-Patterns (DO NOT DO in DR recovery)

| Anti-Pattern | Why It Fails | Correct Approach |
|-------------|-------------|-----------------|
| Changing Route 53 records for failover | Depends on Route 53 CP in us-east-1 | Use health-check-based failover (data plane), pre-provision records |
| Creating/updating IAM roles during failover | IAM CP in us-east-1 | Pre-provision all IAM resources |
| Creating new ELBs during disaster | Depends on Route 53 CP for DNS records | Pre-provision ELBs in DR region |
| Creating new S3 buckets | CreateBucket depends on us-east-1 | Pre-provision all buckets |
| Provisioning RDS instances during disaster | Depends on RDS CP + Route 53 for DNS | Pre-provision read replicas |
| Relying on STS global endpoint | Defaults to us-east-1 | Configure Regional STS endpoints |
| Updating CloudFront origin for failover | Depends on CF CP in us-east-1 | Use origin groups with failover |
| Changing Global Accelerator weights | Depends on AGA CP in us-west-2 | Use health-check-based routing |

---

## 6. Cross-Region Replication Considerations

- AWS does NOT provide synchronous cross-Region replication
- Async replication = potential data loss during failover (RPO > 0)
- Cross-Region latency is 100s-1000s of miles → significant performance impact
- Multi-Region failover requires strict stack separation and coordinated failover
- Regular failover practice is essential

---

## 7. Service-Specific DR Guidance

### RDS/Aurora
- **Single-AZ**: Zonal, SPOF risk
- **Multi-AZ**: Automated failover within Region (~60s for Aurora, minutes for RDS)
- **Cross-Region read replicas**: Manual promotion needed; depends on RDS CP
- **Aurora Global Database**: Managed cross-Region replication with ~1s lag

### DynamoDB
- **Standard table**: Regional, multi-AZ automatic. NOT an AZ SPOF
- **Global Table**: Multi-Region replication, < 1 second lag. Active-active

### S3
- **Standard**: Regional, multi-AZ automatic. NOT an AZ SPOF
- **Cross-Region Replication (CRR)**: Async, requires pre-configuration
- **Bucket creation/deletion**: Depends on us-east-1 — pre-provision!

### SQS / SNS
- **Regional services**: Multi-AZ automatic. NOT an AZ SPOF
- No native cross-Region replication
- Must re-create or use fan-out for multi-Region

### Lambda
- **Regional service**: Runs across multiple AZs. NOT an AZ SPOF
- Function URLs depend on Route 53 CP (us-east-1) for creation
- Pre-provision all Lambda resources for DR

### ELB (ALB/NLB)
- **Regional service**: Distributes across AZs
- **IMPORTANT**: The ELB itself is Regional, but **target instances are zonal**
- Creating new ELBs depends on Route 53 CP — pre-provision!
- Health checks are data plane (reliable)

### EKS
- **Control plane**: Regional, managed by AWS
- **Worker nodes**: Zonal (EC2-based), AZ-bound
- **Node failure**: Use multi-AZ node groups
- **Managed K8s CP**: Regional endpoint, dependencies on Route 53 for CP creation

### Neptune
- **Single instance**: Zonal, SPOF risk
- **Cluster with replicas**: Multi-AZ within Region
- **No native cross-Region replication** (as of this writing)

---

## 8. Impact on DR Plan Generator

### SPOF Detection Rules (corrected)

```python
# Resources that ARE AZ-bound → candidates for AZ-level SPOF
AZ_BOUND_TYPES = {
    "EC2Instance",
    "EBSVolume",
    "RDSInstance",      # Single-AZ only; Multi-AZ has standby
    "RDSCluster",       # Writer instance is in one AZ
    "NeptuneInstance",
    "NeptuneCluster",   # Writer in one AZ
    "ElastiCacheNode",
    "EKSNodeGroup",     # EC2-based, AZ-bound
}

# Resources that are NOT AZ-bound → NEVER flag as AZ SPOF
REGIONAL_TYPES = {
    "DynamoDBTable",    # Regional, multi-AZ automatic
    "SQSQueue",         # Regional, multi-AZ automatic
    "SNSTopic",         # Regional, multi-AZ automatic
    "S3Bucket",         # Regional, multi-AZ automatic
    "LambdaFunction",   # Regional, runs across AZs
    "StepFunction",     # Regional
    "LoadBalancer",     # Regional (targets are zonal, but LB itself is not)
    "APIGateway",       # Regional
    "EventBridgeRule",  # Regional
}
```

### Phase 0 Pre-flight Checks (informed by whitepaper)

1. **Verify DR resources are pre-provisioned** (not created on-the-fly)
2. **Check replication lag** for cross-Region data stores
3. **Validate Regional STS endpoints** (not global)
4. **Verify Route 53 health checks** are data-plane based
5. **Confirm IAM roles/policies** exist in target Region
6. **Check ELBs** are pre-provisioned in target

### Recovery Path Rules

1. **ONLY use data plane operations** in Phases 1-4
2. **Pre-provision everything** in Phase 0 (before disaster)
3. **RDS failover** = data plane (promote replica) ✅
4. **DynamoDB Global Table** = data plane (already active) ✅
5. **Route 53 failover** via health checks = data plane ✅
6. **Route 53 record update** = control plane ❌ (avoid in recovery)
7. **Creating new ELBs** = control plane ❌ (pre-provision)
8. **Creating new S3 buckets** = control plane ❌ (pre-provision)

---

## 9. Summary Decision Matrix for DR Plan Generator

| Question | If YES | If NO |
|----------|--------|-------|
| Is resource zonal (EC2, EBS, RDS single-AZ)? | Flag as potential AZ SPOF | Not an AZ SPOF risk |
| Is resource Regional (DynamoDB, SQS, S3, Lambda)? | Skip AZ SPOF detection | Check if zonal |
| Does recovery step create new resources? | ⚠️ Flag as control-plane dependency | ✅ Safe for recovery path |
| Does recovery step modify Route 53 records? | ⚠️ Depends on us-east-1 CP | ✅ No cross-Region CP dependency |
| Is resource pre-provisioned in target? | ✅ Statically stable | ⚠️ Risk: CP dependency at disaster time |

---

## 10. Appendix A — Partitional Service Guidance (Static Stability)

Each partitional service has CP in a single Region; DP is distributed. Key: **never use CP operations in recovery path**.

| Service | CP Location | DP Behavior During CP Failure | Static Stability Pattern |
|---------|-------------|-------------------------------|--------------------------|
| **IAM** | us-east-1 | Auth & authz continue working. STS (separate DP) works. | Pre-provision break-glass users with credentials in vault. Don't create/modify roles during recovery. |
| **AWS Organizations** | us-east-1 | SCPs continue evaluating. Delegated admin works. | Use session tags for dynamic perms (DP). Don't modify SCPs during recovery. |
| **Account Management** | us-east-1 | Existing accounts work. | Pre-provision all DR accounts. Don't create new accounts during failure. |
| **Route 53 ARC** | us-west-2 | Recovery cluster DP works. Routing controls queryable. | Bookmark/hardcode 5 Regional cluster endpoints. Use CLI/SDK not console. |
| **Network Manager** | us-west-2 | Cloud WAN data plane unaffected. | Don't use NM for network changes during recovery. Export CW metrics to S3 proactively. |
| **Route 53 Private DNS** | us-east-1 | Same as public DNS — resolution continues. | Same as Route 53 public: use health-check-based failover. |

---

## 11. Appendix B — Edge Network Global Service Guidance

| Service | CP Location | DP During CP Failure | Static Stability Pattern |
|---------|-------------|----------------------|--------------------------|
| **Route 53 Public DNS** | us-east-1 | DNS resolution + health checks continue. Record changes via health check status work. | Use health-check-based failover (ARC routing controls). Pre-provision DNS records. Never ChangeResourceRecordSets in recovery. |
| **CloudFront** | us-east-1 | Caching + serving continue. Origin failover works. Invalidations may fail. | Use origin failover groups. Don't modify distribution config during recovery. |
| **ACM (for CloudFront)** | us-east-1 | Existing certs work. Auto-renewal works. | Don't create/change certs during recovery. |
| **WAF / WAF Classic** | us-east-1 | Existing web ACLs + rules continue. | Don't update WAF rules during recovery. |
| **Global Accelerator** | us-west-2 | Anycast routing continues. Health checks work. Traffic dials/weights applied. | Use health-check-based failover. Don't modify traffic dial or endpoints during recovery. |
| **Shield Advanced** | us-east-1 | DDoS protection continues. Health check responses work. | Pre-configure DR resources in protection groups. Don't add protections during recovery. |

---

## 12. Appendix C — Single-Region Services (DR Risk)

These services exist in **only one Region** — no multi-Region option:
- AWS Marketplace (Catalog API, Commerce Analytics, Entitlement)
- Billing & Cost Management (Cost Explorer, CUR, Budgets, Savings Plans)
- AWS Chatbot, AWS DeepRacer, AWS Device Farm
- Alexa for Business, Amazon Chime, Amazon Mechanical Turk

**DR implication**: If your workflow depends on these services, there is no failover option. Plan accordingly.

---

## 13. Static Stability — Deep Dive

**Core AWS design principle**: Systems continue operating without changes during dependency failures.

Key properties:
1. **Data plane independence**: Once provisioned, resources don't need control plane to function
2. **No circular dependencies**: Services designed to recover without mutual blocking
3. **State maintenance**: DP maintains existing state during CP failure

**Examples of static stability**:
- EC2 instance once launched: stays running regardless of EC2 CP health
- VPCs, S3 buckets/objects, EBS volumes: all data plane, no CP dependency
- Route 53 health checks: data plane, continue evaluating during CP failure
- IAM auth/authz: data plane in each Region, works during IAM CP failure

**For DR Plan Generator**: Every step in Phase 1-4 should be checked:
- Does it require resource creation? → **Not statically stable** → flag as risk
- Does it only use existing resources? → **Statically stable** → safe
- Does it modify configuration? → **Depends on CP** → pre-configure before disaster

---

*This reference should be consulted whenever modifying SPOF detection logic, step builder commands, or pre-flight checks in dr-plan-generator.*
