#!/bin/bash
# deploy.sh - RCA Lambda Phase 1 部署脚本
# 环境：petsite ap-northeast-1
# 前置：需先运行: aws ssm put-parameter --name /petsite/slack/webhook --value "https://hooks.slack.com/..." --type SecureString --region ap-northeast-1

set -e

REGION="ap-northeast-1"
ACCOUNT="926093770964"
FUNCTION_NAME="petsite-rca-engine"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/neptune-etl-lambda-role"
SUBNET_IDS="subnet-0f801fa79077eb277,subnet-047a94f9c5ab6302a"
SG_IDS="sg-078f24929b25f09cd"
NEPTUNE_ENDPOINT="petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com"
SNS_TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT}:petsite-rca-alerts"

echo "=== Step 1: 打包 Lambda ==="
cd /home/ubuntu/tech/rca_engine
pip install requests -t . -q
zip -r /tmp/rca-engine.zip . -x "*.pyc" -x "__pycache__/*"
echo "Package size: $(du -sh /tmp/rca-engine.zip | cut -f1)"

echo ""
echo "=== Step 2: 创建/更新 Lambda ==="
if aws lambda get-function --function-name $FUNCTION_NAME --region $REGION > /dev/null 2>&1; then
    echo "Updating existing function..."
    aws lambda update-function-code \
        --function-name $FUNCTION_NAME \
        --zip-file fileb:///tmp/rca-engine.zip \
        --region $REGION
    aws lambda wait function-updated --function-name $FUNCTION_NAME --region $REGION
    echo "Code updated."
else
    echo "Creating new function..."
    aws lambda create-function \
        --function-name $FUNCTION_NAME \
        --runtime python3.12 \
        --role $ROLE_ARN \
        --handler handler.lambda_handler \
        --zip-file fileb:///tmp/rca-engine.zip \
        --timeout 60 \
        --memory-size 256 \
        --environment "Variables={
            NEPTUNE_ENDPOINT=$NEPTUNE_ENDPOINT,
            NEPTUNE_PORT=8182,
            REGION=$REGION
        }" \
        --vpc-config "SubnetIds=$SUBNET_IDS,SecurityGroupIds=$SG_IDS" \
        --region $REGION
    echo "Function created."
fi

echo ""
echo "=== Step 3: 更新环境变量（从 SSM 读取 Webhook）==="
WEBHOOK_URL=$(aws ssm get-parameter --name /petsite/slack/webhook --with-decryption --query 'Parameter.Value' --output text --region $REGION 2>/dev/null || echo "")
if [ -z "$WEBHOOK_URL" ]; then
    echo "⚠️  /petsite/slack/webhook not found in SSM — Slack 通知将以 dry-run 模式运行"
else
    aws lambda update-function-configuration \
        --function-name $FUNCTION_NAME \
        --environment "Variables={
            NEPTUNE_ENDPOINT=$NEPTUNE_ENDPOINT,
            NEPTUNE_PORT=8182,
            REGION=$REGION,
            SLACK_WEBHOOK_URL=$WEBHOOK_URL,
            SLACK_CHANNEL=#petsite-alerts
        }" \
        --region $REGION > /dev/null
    echo "Slack webhook configured."
fi

echo ""
echo "=== Step 4: 创建 SNS Topic（RCA 触发器）==="
SNS_TOPIC_ARN=$(aws sns create-topic --name petsite-rca-alerts --region $REGION --query 'TopicArn' --output text)
echo "SNS Topic: $SNS_TOPIC_ARN"

echo ""
echo "=== Step 5: 订阅 Lambda 到 SNS ==="
aws sns subscribe \
    --topic-arn $SNS_TOPIC_ARN \
    --protocol lambda \
    --notification-endpoint "arn:aws:lambda:${REGION}:${ACCOUNT}:function:${FUNCTION_NAME}" \
    --region $REGION > /dev/null

aws lambda add-permission \
    --function-name $FUNCTION_NAME \
    --statement-id rca-sns-trigger \
    --action lambda:InvokeFunction \
    --principal sns.amazonaws.com \
    --source-arn $SNS_TOPIC_ARN \
    --region $REGION 2>/dev/null || echo "Permission already exists"

echo ""
echo "=== Step 6: 冒烟测试（本地调用）==="
aws lambda invoke \
    --function-name $FUNCTION_NAME \
    --payload '{"source":"manual","affected_resource":"payforadoption","metric":"error_rate","value":0.87,"threshold":0.05}' \
    --region $REGION \
    /tmp/rca-test-output.json --log-type Tail 2>/dev/null | python3 -c "
import sys, json, base64
d = json.load(sys.stdin)
print('Status:', d.get('StatusCode'))
log = base64.b64decode(d.get('LogResult','')).decode()
print('Log tail:')
for line in log.strip().split('\n')[-10:]:
    print(' ', line)
"
echo "Output:"
cat /tmp/rca-test-output.json | python3 -m json.tool

echo ""
echo "✅ Phase 1 部署完成！"
echo "SNS Topic ARN: $SNS_TOPIC_ARN"
echo "Lambda ARN: arn:aws:lambda:${REGION}:${ACCOUNT}:function:${FUNCTION_NAME}"
