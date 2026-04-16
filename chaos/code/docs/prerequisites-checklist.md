# 混沌实验前置条件检查清单

> 在执行任何混沌实验之前，请逐项确认以下条件已满足。
>
> Prerequisites checklist before running any chaos experiment.

---

## 1. 通用前置条件（General Prerequisites）

基础运行环境与 AWS 配置。
*Base runtime environment and AWS configuration.*

- [ ] Python >= 3.12 已安装
  ```bash
  python3 --version  # 应输出 Python 3.12.x 或以上
  ```
- [ ] 依赖包已安装（boto3、pyyaml、requests）
  ```bash
  pip3 show boto3 pyyaml requests | grep -E "^Name|^Version"
  ```
- [ ] AWS CLI 已配置（Region = ap-northeast-1）
  ```bash
  aws configure list
  aws sts get-caller-identity
  ```
- [ ] IAM 权限覆盖 FIS / EKS / DynamoDB / CloudWatch / S3
  ```bash
  aws iam simulate-principal-policy \
    --policy-source-arn $(aws sts get-caller-identity --query Arn --output text) \
    --action-names fis:CreateExperimentTemplate fis:StartExperiment \
    eks:DescribeCluster dynamodb:PutItem cloudwatch:DescribeAlarms
  ```
- [ ] DynamoDB 表 `chaos-experiments` 已创建且可写
  ```bash
  aws dynamodb describe-table --table-name chaos-experiments \
    --query "Table.TableStatus" --output text
  # 应输出 ACTIVE
  ```
- [ ] Neptune 集群可达（HTTPS 8182）
  ```bash
  curl -sk https://petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com:8182/status \
    | python3 -m json.tool | grep status
  # 应包含 "healthy"
  ```

---

## 2. EKS 微服务场景（EKS Microservice Scenarios）

使用 Chaos Mesh 向 K8s Pod 注入故障前需确认。
*Required before injecting faults into K8s Pods via Chaos Mesh.*

- [ ] kubeconfig 已配置 PetSite EKS 集群
  ```bash
  aws eks update-kubeconfig --name petsite-eks --region ap-northeast-1
  kubectl config current-context
  ```
- [ ] kubectl 可访问 `default` namespace
  ```bash
  kubectl get pods -n default
  ```
- [ ] Chaos Mesh >= 2.6 已安装到集群
  ```bash
  kubectl get pods -n chaos-mesh
  # 应看到 chaos-controller-manager / chaos-daemon 均为 Running
  kubectl get crd | grep chaos-mesh.org | wc -l
  # 应 >= 20
  ```
- [ ] Chaos Mesh CRD 版本兼容（PodChaos / NetworkChaos / StressChaos）
  ```bash
  kubectl api-resources --api-group=chaos-mesh.org \
    | grep -E "PodChaos|NetworkChaos|StressChaos"
  ```
- [ ] EKS OIDC Provider 已配置（Chaos Mesh ServiceAccount 用）
  ```bash
  aws eks describe-cluster --name petsite-eks \
    --query "cluster.identity.oidc.issuer" --output text
  # 应返回 https://oidc.eks.ap-northeast-1.amazonaws.com/id/...
  ```
- [ ] FIS EKS ServiceAccount 已创建（用于 FIS EKS Pod action）
  ```bash
  kubectl get serviceaccount -n default fis-experiment-sa 2>/dev/null \
    || echo "ServiceAccount 不存在，需创建"
  ```
- [ ] 目标服务 Pod 全部处于 Running/Ready 状态
  ```bash
  kubectl get pods -n default -l app=petsite
  kubectl get pods -n default -l app=pay-for-adoption
  kubectl get pods -n default -l app=search-service
  ```

---

## 3. AWS FIS 场景（AWS FIS Scenarios）

使用 AWS Fault Injection Service 执行基础设施级故障前需确认。
*Required before running infrastructure-level faults via AWS FIS.*

- [ ] FIS IAM Role `chaos-fis-experiment-role` 已创建
  ```bash
  aws iam get-role --role-name chaos-fis-experiment-role \
    --query "Role.Arn" --output text
  ```
- [ ] FIS Role 信任策略包含 `fis.amazonaws.com`
  ```bash
  aws iam get-role --role-name chaos-fis-experiment-role \
    --query "Role.AssumeRolePolicyDocument" --output json \
    | grep "fis.amazonaws.com"
  ```
- [ ] CloudWatch Alarms `chaos-*` 系列已创建（用作 Stop Conditions）
  ```bash
  aws cloudwatch describe-alarms \
    --alarm-name-prefix chaos- \
    --query "MetricAlarms[].AlarmName" --output table
  ```
- [ ] S3 Bucket `chaos-fis-config-926093770964` 已创建且可访问
  ```bash
  aws s3 ls s3://chaos-fis-config-926093770964/ 2>&1 | head -5
  ```
- [ ] 目标 Lambda 函数存在且状态正常
  ```bash
  aws lambda list-functions \
    --query "Functions[?starts_with(FunctionName,'petsite')].{Name:FunctionName,State:State}" \
    --output table
  ```
- [ ] 目标 EC2/EBS 实例存在且运行
  ```bash
  aws ec2 describe-instances \
    --filters "Name=tag:Project,Values=PetSite" "Name=instance-state-name,Values=running" \
    --query "Reservations[].Instances[].{ID:InstanceId,State:State.Name}" --output table
  ```
- [ ] 目标 RDS 集群状态为 available
  ```bash
  aws rds describe-db-clusters \
    --query "DBClusters[?contains(DBClusterIdentifier,'petsite')].{ID:DBClusterIdentifier,Status:Status}" \
    --output table
  ```

---

## 4. FIS Scenario Library（AZ 级 / 跨区域场景）

执行 AZ 模拟、子网中断、跨 AZ 切换等场景前需额外确认。
*Additional checks for AZ impairment, subnet disruption, and cross-AZ failover scenarios.*

- [ ] 目标 EC2 实例已打标签 `AzImpairmentPower: StopInstances`
  ```bash
  aws ec2 describe-instances \
    --filters "Name=tag-key,Values=AzImpairmentPower" \
    --query "Reservations[].Instances[].{ID:InstanceId,Tag:Tags[?Key=='AzImpairmentPower']|[0].Value}" \
    --output table
  ```
- [ ] 实验模板已从 AWS FIS Console Scenario Library 导入（不能纯靠 API 创建）
  ```bash
  aws fis list-experiment-templates \
    --query "experimentTemplates[].{ID:id,Description:description}" --output table
  # 检查 AZ Impairment / Network Disruption 类模板存在
  ```
- [ ] FIS Role 权限覆盖所有 sub_action（StopInstances / DisableVpcEndpoints / ModifySubnet）
  ```bash
  aws iam simulate-principal-policy \
    --policy-source-arn arn:aws:iam::926093770964:role/chaos-fis-experiment-role \
    --action-names ec2:StopInstances ec2:ModifySubnetAttribute \
    ec2:ModifyVpcEndpoint ec2:CreateNetworkAcl
  ```
- [ ] 多 AZ 部署已确认（EKS 节点跨 1a / 1c）
  ```bash
  kubectl get nodes \
    --label-columns topology.kubernetes.io/zone \
    --no-headers | awk '{print $NF}' | sort | uniq -c
  # 应看到 1a 和 1c 均有节点
  ```
- [ ] ALB / Target Group 跨 AZ 健康检查已通过
  ```bash
  aws elbv2 describe-target-health \
    --target-group-arn $(aws elbv2 describe-target-groups \
      --query "TargetGroups[0].TargetGroupArn" --output text) \
    --query "TargetHealthDescriptions[].{ID:Target.Id,AZ:Target.AvailabilityZone,Health:TargetHealth.State}" \
    --output table
  ```
- [ ] 备用 AZ 中已有足够副本承接流量（至少 1 个 Tier0 服务副本在非故障 AZ）
  ```bash
  kubectl get pods -n default \
    -o custom-columns="NAME:.metadata.name,NODE:.spec.nodeName,STATUS:.status.phase" \
    | grep -E "petsite|pay-for-adoption"
  ```

---

## 5. DeepFlow 可观测性（DeepFlow Observability）

确保实验期间可采集指标。
*Ensure metrics can be collected during the experiment.*

- [ ] DeepFlow ClickHouse 可达（11.0.2.30:8123）
  ```bash
  curl -s "http://11.0.2.30:8123/?query=SELECT+1" 2>&1
  # 应返回 1
  ```
- [ ] `flow_log.l7_flow_log` 表存在
  ```bash
  curl -s "http://11.0.2.30:8123/?query=SHOW+TABLES+FROM+flow_log" \
    | grep l7_flow_log
  # 应看到 l7_flow_log
  ```
- [ ] 目标服务最近 5 分钟有 L7 流量记录
  ```bash
  curl -s "http://11.0.2.30:8123/?query=SELECT+count()+FROM+flow_log.l7_flow_log+WHERE+pod_group_0+LIKE+'%petsite%'+AND+time_>now()-300"
  # 应返回 > 0
  ```
- [ ] DeepFlow Agent 在所有 EKS 节点上运行
  ```bash
  kubectl get pods -n deepflow -l app=deepflow-agent
  # 所有节点均应有对应的 DaemonSet Pod
  ```

---

## 快速全量检查脚本

```bash
#!/bin/bash
# 运行前置检查（快速模式，跳过需要 stdin 的交互命令）
echo "=== AWS Identity ==="
aws sts get-caller-identity --output table

echo "=== DynamoDB Table ==="
aws dynamodb describe-table --table-name chaos-experiments \
  --query "Table.TableStatus" --output text 2>&1

echo "=== FIS Role ==="
aws iam get-role --role-name chaos-fis-experiment-role \
  --query "Role.Arn" --output text 2>&1

echo "=== Chaos Mesh ==="
kubectl get pods -n chaos-mesh --no-headers 2>&1 | awk '{print $1, $3}'

echo "=== EKS Nodes per AZ ==="
kubectl get nodes --label-columns topology.kubernetes.io/zone \
  --no-headers 2>&1 | awk '{print $NF}' | sort | uniq -c

echo "=== DeepFlow ClickHouse ==="
curl -s "http://11.0.2.30:8123/?query=SELECT+1" 2>&1
```
