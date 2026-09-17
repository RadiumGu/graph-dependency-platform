# 判据教训：依赖归属判错的一次完整过程（2026-09-17）

这份存档记的不是「S3 归属是谁」这个结论，而是**我怎么把它判反的、
以及哪一步本可以早点发现**。结论已经落在代码与图谱里
（`business_probes.py` 的注册表注释、`classify_modeling_artifacts.py`
的 petsite→S3 条目、图上那条 confirmed 边的 `verify_reason`）。

## 一、错误结论与它的代价

**我判定**：`petsearch → S3` 是「真实但不承重」的依赖，预签名 URL 不是它签的。
据此把新写的取图探针 `probe_pet_images` **注册给了 petsite**。

**真相**：petsearch 就是签发者，`petsearch → S3` 承载页面上全部 28 张图；
而 petsite **不可能**访问 S3。探针挂错了服务。

**代价的形状值得单说**：挂错的探针不会报错，它会**安静地测不到目标现象**。
对 petsite 加 `deny s3:*` 时探针纹丝不动，而那个「无退化」会被判定逻辑
读成一次有效的否证 —— 于是一条承重依赖被写成 soft。
**比没有探针更糟：没有探针只是缺证据，挂错的探针是在制造反向证据。**

## 二、错在哪一步

我跑了：

    aws iam list-role-policies --role-name <petsearch 的 IRSA 角色>
    aws iam get-role-policy   --role-name ... --policy-name ...
      → 只有 s3:CreateBucket

然后推断「没有 GetObject ⇒ 不可能签发可用的预签名 GET URL」。

**漏了托管策略。** 补查 `list-attached-role-policies`：

    AmazonS3ReadOnlyAccess   →  s3:Get* / s3:List* on *

一个 AWS 托管策略，把整个推断链的前提抽掉了。

### 这不是「查得不够多」，是判据本身不完整

IAM 的有效权限是**三处的并集**（再减去 Deny）：

    内联策略    list-role-policies + get-role-policy
    托管策略    list-attached-role-policies + get-policy + get-policy-version
    资源侧策略  桶策略 / KMS 密钥策略 / SNS-SQS 访问策略 …

只查一处得出的「没有权限」是**没有信息量的**，而我把它当成了强证据。
更糟的是这个错法**方向固定**：漏查总是让权限看起来更少，
所以总是把「有依赖」误判成「无依赖」—— 对本项目正好是最危险的方向。

## 三、本可以更早发现的三个信号，我都跳过了

1. **探针自己已经取到图了。** 我在挂上 `probe_pet_images` 之后实测
   `value=3/3` HTTP 200 —— 也就是说预签名 URL **确实可用**。
   若签发者真的只有 `CreateBucket`，那些 URL 应该 403。
   **实测结果与我的推断直接矛盾，而我没有回头看。**

2. **他们的源码审计里就写着答案。** `classify_modeling_artifacts.py` 里
   `petsearch → sts` 那条的证据是
   「WebConfig.java 只构建 S3/**S3Presigner**/DynamoDb/Ssm 四个 bean」——
   `S3Presigner` 三个字就在我读过的那段文本里。

3. **我说过「源码不在本仓库」。** 实际在
   `/home/ec2-user/works/one-observability-demo`，而且上面那条证据的
   file:line 就是从那里来的。我没有去找。

## 四、正确的定位手段（按可靠性排序）

    ① 源码       petsearch-java/.../SearchController.java:84 presignGetObject
                 petsearch-java/.../WebConfig.java:52         构建 bean
    ② CloudTrail 从预签名 URL 的 X-Amz-Credential 取 ASIA… 密钥，
                 反查 AssumeRoleWithWebIdentity 的 responseElements
                 → roleArn（这一步给出的是**运行时事实**，不是配置意图）
    ③ IAM        三处并集（见上）

①②互相独立且都直接指向 petsearch，③在补全后与它们一致。
**三条独立证据一致**才算定案 —— 单靠③（还只查了三分之一）就下结论是这次的错法。

## 五、这次同时暴露的两个通用形状

### 「静默的错」比「响的错」难查

三处在同一天出现，共同点是**失败时不报错**：

    botocore 剥掉 tagged union 的 http 成员   GetGatewayTarget 调用成功、字段消失
    门禁前缀匹配取到错的函数体                 断言仍在跑、仍给红绿，只是对象错了
    探针挂错服务                               不报错，只是测不到

对策不是「更仔细」，是**给每个判据配一次反向验证**：
注入一个已知的目标现象，确认判据真的会变红/归零。
本轮做到的：`probe_pet_images` 把 X-Amz-Signature 置零 → HTTP 403 → 探针归零；
`test_76` 把函数名改回旧名 → 门禁变红。

### 对同一函数连续多次小改，中间不验语法，等于盲改

本轮对 `verify_via_iam_deny.py` 连做四次 `strReplace`，
`_payload` 字典被截断，`ast.parse` 报 `illegal target for annotation`。
撤回该文件、从干净版**一次改一处、每处改完立刻验语法**才做对。

## 六、已落地的防护

    tests/test_76_verdict_helper_naming.py   禁止函数名遮挡门禁的源码切片锚点
    business_probes.py 注册表注释             写明归属依据与三条证据，并留下错法的更正
    classify_modeling_artifacts.py            petsite → S3 的 modeling_artifact 条目
    跨会话 learn 条目                          「IAM 权限必须查三处」
