import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as neptune from 'aws-cdk-lib/aws-neptune';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

/**
 * NeptuneClusterStack — Creates an Amazon Neptune graph database cluster.
 *
 * Neptune is a schema-on-write graph database: no DDL or schema initialisation
 * is required.  The ETL Lambda functions create vertex/edge labels and
 * properties automatically on first write.
 *
 * Default instance: db.r6g.large (single-AZ, 1 writer instance).
 * Adjust instanceType / add read replicas as your workload grows.
 */
export class NeptuneClusterStack extends cdk.Stack {
  /** Neptune cluster writer endpoint — feed this into NeptuneEtlStack context. */
  public readonly clusterEndpoint: string;
  /** Neptune cluster resource ID — needed for IAM policy resource ARN. */
  public readonly clusterResourceId: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // =========================================================
    // Context parameters
    // =========================================================
    const vpcId = this.node.tryGetContext('vpcId') as string;

    // VPC (look up existing)
    const vpc = ec2.Vpc.fromLookup(this, 'Vpc', { vpcId });

    // =========================================================
    // Security Groups
    // =========================================================

    // Neptune cluster SG — allows inbound 8182 from Lambda SG
    const neptuneSg = new ec2.SecurityGroup(this, 'NeptuneSg', {
      vpc,
      securityGroupName: 'neptune-cluster-sg',
      description: 'Neptune cluster — allows Gremlin (8182) from Lambda SG',
      allowAllOutbound: false, // Neptune doesn't need outbound
    });

    // Lambda SG — will be used by ETL Lambda functions
    const lambdaSg = new ec2.SecurityGroup(this, 'LambdaSg', {
      vpc,
      securityGroupName: 'neptune-lambda-sg',
      description: 'Lambda functions that connect to Neptune',
      allowAllOutbound: true,
    });

    // Neptune ← Lambda on port 8182
    neptuneSg.addIngressRule(
      lambdaSg,
      ec2.Port.tcp(8182),
      'Allow Gremlin from Lambda SG',
    );

    // =========================================================
    // DB Subnet Group (private subnets)
    // =========================================================
    const subnetGroup = new neptune.CfnDBSubnetGroup(this, 'NeptuneSubnetGroup', {
      dbSubnetGroupDescription: 'Private subnets for Neptune cluster',
      dbSubnetGroupName: 'neptune-graph-subnet-group',
      subnetIds: vpc.privateSubnets.map(s => s.subnetId),
    });

    // =========================================================
    // IAM — NeptuneETLPolicy (managed policy for Lambda → Neptune)
    // =========================================================
    // We create the policy here so there's no manual prerequisite.
    // The resource ARN uses a wildcard initially; after deployment you can
    // tighten it to the actual cluster resource ID from the stack outputs.
    const neptuneEtlPolicy = new iam.ManagedPolicy(this, 'NeptuneETLPolicy', {
      managedPolicyName: 'NeptuneETLPolicy',
      description: 'Allows Lambda to connect and read/write Neptune graph data',
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'neptune-db:connect',
            'neptune-db:ReadDataViaQuery',
            'neptune-db:WriteDataViaQuery',
            'neptune-db:DeleteDataViaQuery',
            'neptune-db:GetGraphSummary',
          ],
          resources: [
            // Tighten after first deploy using NeptuneClusterResourceId output
            `arn:aws:neptune-db:${this.region}:${this.account}:*/*`,
          ],
        }),
      ],
    });

    // =========================================================
    // Neptune Cluster (provisioned, single-AZ)
    // =========================================================
    const cluster = new neptune.CfnDBCluster(this, 'NeptuneCluster', {
      dbClusterIdentifier: 'graph-dp-neptune',
      engineVersion: '1.3.4.0',            // Latest Neptune 1.3 (TinkerPop 3.7)
      dbSubnetGroupName: subnetGroup.dbSubnetGroupName,
      vpcSecurityGroupIds: [neptuneSg.securityGroupId],
      iamAuthEnabled: true,                 // SigV4 authentication
      storageEncrypted: true,
      deletionProtection: false,            // Set true for production!
      preferredBackupWindow: '18:00-19:00', // 02:00-03:00 CST (UTC+8)
      backupRetentionPeriod: 7,
      // Single-AZ: specify one AZ to avoid multi-AZ charges
      availabilityZones: [`${this.region}a`],
    });
    cluster.addDependency(subnetGroup);

    // =========================================================
    // Neptune Instance (db.r6g.large — adjust as needed)
    // =========================================================
    //
    // Instance sizing guide:
    //   db.r6g.medium  — dev/test (< 10M edges)
    //   db.r6g.large   — small production (10M–100M edges)  ← default
    //   db.r6g.xlarge  — medium production (100M–500M edges)
    //   db.r6g.2xlarge — large graphs or high concurrency
    //
    const instance = new neptune.CfnDBInstance(this, 'NeptuneInstance', {
      dbInstanceIdentifier: 'graph-dp-neptune-writer',
      dbClusterIdentifier: cluster.dbClusterIdentifier!,
      dbInstanceClass: 'db.r6g.large',
      availabilityZone: `${this.region}a`,
    });
    instance.addDependency(cluster);

    // =========================================================
    // Outputs
    // =========================================================
    new cdk.CfnOutput(this, 'NeptuneClusterEndpoint', {
      value: cluster.attrEndpoint,
      description: 'Neptune cluster writer endpoint — set as neptuneEndpoint in cdk.json',
      exportName: 'NeptuneClusterEndpoint',
    });

    new cdk.CfnOutput(this, 'NeptuneClusterPort', {
      value: cluster.attrPort,
      description: 'Neptune cluster port',
      exportName: 'NeptuneClusterPort',
    });

    new cdk.CfnOutput(this, 'NeptuneClusterResourceId', {
      value: cluster.attrClusterResourceId,
      description: 'Neptune cluster resource ID — use to tighten NeptuneETLPolicy ARN',
      exportName: 'NeptuneClusterResourceId',
    });

    new cdk.CfnOutput(this, 'NeptuneSgId', {
      value: neptuneSg.securityGroupId,
      description: 'Neptune cluster security group ID',
      exportName: 'NeptuneSgId',
    });

    new cdk.CfnOutput(this, 'LambdaSgId', {
      value: lambdaSg.securityGroupId,
      description: 'Lambda security group ID — set as lambdaSgId in cdk.json',
      exportName: 'LambdaSgId',
    });

    new cdk.CfnOutput(this, 'NeptuneETLPolicyArn', {
      value: neptuneEtlPolicy.managedPolicyArn,
      description: 'NeptuneETLPolicy ARN — set as neptuneEtlPolicyArn in cdk.json',
      exportName: 'NeptuneETLPolicyArn',
    });

    // Store for cross-stack references
    this.clusterEndpoint = cluster.attrEndpoint;
    this.clusterResourceId = cluster.attrClusterResourceId;
  }
}
