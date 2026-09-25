#!/usr/bin/env python3.11
"""从东京活集群生成韩国侧的工作负载清单。

## 为什么是脚本而不是手写清单

手写的清单是一份**会悄悄过期的快照**:东京改了镜像、加了环境变量、换了 SA,
韩国那份不会跟着动,而过期的表现往往是「pod 起来了，行为不对」——
和「配置没问题」难以区分。

所以清单由这个脚本从活集群生成,并把生成时间与来源写进文件头。
东京变更后重跑一次即可,diff 会直接告诉你变了什么。

## 刻意做的改动（每一处都有理由）

| 改动 | 为什么 |
|---|---|
| 镜像 registry 换成 ap-northeast-2 | ECR 复制到韩国的是同名仓库、同 digest；<br>照抄东京的 registry 会让韩国去跨 region 拉，<br>而灾备场景下东京可能不可达 |
| `replicas` → 1 | 守夜灯扩容时通常只有 1 个节点 |
| 去掉 CDK 的 prune 标签 | 那些标签属于 CDK 的 `kubectl apply --prune` 机制，<br>带过来会让韩国的对象被东京侧的 apply 误删 |
| 去掉 `last-applied-configuration` 注解 | 它是东京那次 apply 的快照，带过来纯噪音且巨大 |
| 去掉 `status` / `metadata.uid` / `resourceVersion` 等 | 服务端字段，带上会被拒 |
| **SA 名字保持不变** | 7 个 IRSA 的信任策略里 `:sub` 写死了<br>`system:serviceaccount:petadoptions:<sa>`；改名等于永远 403 |

## 刻意**不**改的

- 容器端口、探针、资源请求 —— 与东京一致，否则演练的不是同一个东西
- 环境变量的**键**保持原样。值里指向东京资源的那些（SSM 前缀等）不动，
  因为改它们属于手册第五节⑤（region 内后端），不是本脚本的范围。

用法:
    python3.11 scripts/gen_korea_workloads.py --out infra/dr-korea/15-korea-workloads.yaml
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TOKYO_REGION = "ap-northeast-1"
KOREA_REGION = "ap-northeast-2"
TOKYO_CLUSTER = "PetSite"
NAMESPACE = "petadoptions"

# 要搬的 6 个 —— petsite 已经在 14-… 里单独处理了。
# 这个清单**不是猜的**:2026-09-25 读东京 petadoptions 下的 Deployment 得到 7 个，
# 去掉 petsite-deployment 剩这 6 个。
WANTED = [
    "list-adoptions",
    "pay-for-adoption",
    "petfood",
    "pethistory-deployment",
    "search-service",
    "traffic-generator",
]

# CDK 的 prune 机制用的标签前缀 —— 带到韩国会让对象被东京侧 apply 误删
PRUNE_LABEL_PREFIXES = ("aws.cdk.eks/prune-key",)

DROP_ANNOTATIONS = (
    "kubectl.kubernetes.io/last-applied-configuration",
    "deployment.kubernetes.io/revision",
)


def _aws(*args: str) -> str:
    """跑 aws CLI。**不抑制 stderr** —— 错误文本往往就是答案。"""
    r = subprocess.run(
        ["aws", *args], capture_output=True, text=True, timeout=90, check=False
    )
    if r.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:3])} 失败：{r.stderr.strip()}")
    return r.stdout.strip()


class Cluster:
    """用 k8s REST API 读集群（不依赖 kubectl 在不在）。"""

    def __init__(self, region: str, name: str) -> None:
        self.region, self.name = region, name
        self.endpoint = _aws(
            "eks", "describe-cluster", "--region", region, "--name", name,
            "--query", "cluster.endpoint", "--output", "text",
        )
        ca = _aws(
            "eks", "describe-cluster", "--region", region, "--name", name,
            "--query", "cluster.certificateAuthority.data", "--output", "text",
        )
        self._ca = Path(tempfile.mkstemp(suffix=".crt")[1])
        self._ca.write_bytes(base64.b64decode(ca))

    def get(self, path: str) -> dict:
        token = _aws(
            "eks", "get-token", "--region", self.region, "--cluster-name", self.name,
            "--query", "status.token", "--output", "text",
        )
        r = subprocess.run(
            ["curl", "-s", "--max-time", "30", "--cacert", str(self._ca),
             "-H", f"Authorization: Bearer {token}", f"{self.endpoint}{path}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"curl 失败：{r.stderr.strip()}")
        d = json.loads(r.stdout)
        if d.get("kind") == "Status":
            raise RuntimeError(f"k8s 返回 Status：{d.get('reason')} {d.get('message','')}")
        return d


# ═══════════════════════════════════════════════════════════════════
# 容器环境变量里的 region 绑定值
#
# ## 为什么删掉而不是改写
#
# 2026-09-25 读 petadoptionshistory-py/config.py 源码（不是猜的）:
#
#     if cfg['update_adoption_url'] == None or cfg['rds_secret_arn'] == None:
#         return fetch_config_from_parameter_store(cfg['region'])
#
# 应用**本来就支持**从 Parameter Store 取配置 —— 只在环境变量**缺失**时才走。
# 清单里写死了东京的值，所以那条路从没被走过。
#
# 所以处置是**删掉**这些环境变量，让它回落到（韩国的）参数存储 ——
# 而不是我们去改写它们的值。改写等于把同一份配置维护两遍。
#
# ⚠️ AWS_REGION 必须**改写**而不是删掉:它决定 boto3 去哪个 region 读
# 参数存储。留着东京的值，回落之后读的还是东京的参数。
# ═══════════════════════════════════════════════════════════════════

# 删掉:应用会回落到参数存储
DROP_ENV_FOR_PARAM_STORE_FALLBACK = (
    "RDS_SECRET_ARN",
    "UPDATE_ADOPTION_URL",
)

# 改写:值必须是韩国的
REWRITE_ENV = {
    "AWS_REGION": KOREA_REGION,
    "S3_REGION": KOREA_REGION,
}


def fix_env(container: dict, notes: list[str], who: str) -> None:
    """就地修正一个容器的 region 绑定环境变量。"""
    envs = container.get("env")
    if not envs:
        return
    kept = []
    for e in envs:
        name = e.get("name")
        if name in DROP_ENV_FOR_PARAM_STORE_FALLBACK:
            notes.append(
                f"{who}: 删掉环境变量 {name} —— 让应用回落到韩国的参数存储"
                "（源码里本来就有这条回落路径）"
            )
            continue
        if name in REWRITE_ENV and e.get("value") != REWRITE_ENV[name]:
            notes.append(f"{who}: {name} {e.get('value')} → {REWRITE_ENV[name]}")
            e = {**e, "value": REWRITE_ENV[name]}
        kept.append(e)
    container["env"] = kept


def korea_image(img: str) -> tuple[str, bool]:
    """把镜像的 registry 换到韩国。返回 (新镜像, 是否用了可变 tag)。

    ⚠️ 仓库**名字**不动 —— ECR 跨 region 复制不支持改名，所以韩国侧的
    仓库名里仍带 `ap-northeast-1`（那是 CDK bootstrap 命名的一部分）。
    只换 registry 主机名里的 region 段。
    """
    host, _, rest = img.partition("/")
    new_host = host.replace(f".{TOKYO_REGION}.", f".{KOREA_REGION}.")
    tag = rest.rsplit(":", 1)[-1] if ":" in rest else "latest"
    mutable = not tag.startswith("sha256") and tag in ("latest", "main", "master")
    return f"{new_host}/{rest}", mutable


def clean_metadata(meta: dict) -> dict:
    out = {"name": meta["name"], "namespace": NAMESPACE}
    labels = {
        k: v for k, v in (meta.get("labels") or {}).items()
        if not any(k.startswith(p) for p in PRUNE_LABEL_PREFIXES)
    }
    if labels:
        out["labels"] = labels
    anns = {
        k: v for k, v in (meta.get("annotations") or {}).items()
        if k not in DROP_ANNOTATIONS
        and not any(k.startswith(p) for p in PRUNE_LABEL_PREFIXES)
    }
    if anns:
        out["annotations"] = anns
    return out


def convert_deployment(dep: dict) -> tuple[dict, list[str]]:
    """东京的 Deployment → 韩国的 Deployment。返回 (对象, 注意事项)."""
    notes: list[str] = []
    spec = dep["spec"]
    tpl = spec["template"]

    containers = []
    for c in tpl["spec"]["containers"]:
        c = json.loads(json.dumps(c))  # 深拷贝，绝不原地改
        new_img, mutable = korea_image(c["image"])
        if mutable:
            notes.append(
                f"{c['name']}: 镜像用可变 tag `{c['image'].rsplit(':', 1)[-1]}` —— "
                "主备两侧可能指向不同镜像而看不出来"
            )
        c["image"] = new_img
        fix_env(c, notes, f"{dep['metadata']['name']}/{c['name']}")
        containers.append(c)

    # ═══════════════════════════════════════════════════════════════
    # ⚠️ 用**黑名单**拷 pod spec，不用白名单。
    #
    # 第一版是白名单:只拷 serviceAccountName / volumes / nodeSelector /
    # tolerations / securityContext。结果**悄悄丢掉了 enableServiceLinks: False**，
    # 而东京是显式关掉它的。
    #
    # 丢掉之后 k8s 给 pod 注入了 service-link 环境变量
    # （namespace 里有名为 `petfood` 的 Service → 注入
    #   `PETFOOD_PORT=tcp://172.20.21.75:80`），而 petfood 的配置前缀正好是
    # `PETFOOD_`，于是 `port` 读到一个字符串:
    #
    #     Error: LoadError { message: "Failed to deserialize server config:
    #       invalid type: string \"tcp://172.20.21.75:80\",
    #       expected an integer for key `port` in the environment" }
    #
    # **那条报错指不到「你丢了 enableServiceLinks」。**
    #
    # 白名单的问题是「忘了的字段会静默消失」。黑名单反过来:默认全带，
    # 只去掉明确有害的 —— 新字段出现时默认是被带上的，而不是被丢掉的。
    # ═══════════════════════════════════════════════════════════════
    pod_spec = json.loads(json.dumps(tpl["spec"]))  # 深拷贝
    pod_spec["containers"] = containers

    # nodeName 会把 pod 钉在东京的某个节点上（那个节点在韩国不存在）
    pod_spec.pop("nodeName", None)
    # serviceAccount 是 serviceAccountName 的废弃别名，留着会重复
    pod_spec.pop("serviceAccount", None)

    if not pod_spec.get("serviceAccountName"):
        notes.append(f"{dep['metadata']['name']}: **没有 serviceAccountName** —— 不走 IRSA")

    if pod_spec.get("enableServiceLinks") is False:
        notes.append(
            f"{dep['metadata']['name']}: enableServiceLinks=False 已带过来 —— "
            "丢了它会让 service-link 环境变量污染应用配置"
        )

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": clean_metadata(dep["metadata"]),
        "spec": {
            # 守夜灯扩容时通常只有 1 个节点
            "replicas": 1,
            "selector": spec["selector"],
            "template": {
                "metadata": clean_metadata(
                    {**tpl["metadata"], "name": dep["metadata"]["name"]}
                ),
                "spec": pod_spec,
            },
        },
    }, notes


def service_accounts_from_mapping() -> list[dict]:
    """从 irsa-korea-mapping.json 生成 ServiceAccount 对象。

    ## 为什么必须与信任策略同源

    2026-09-25 踩过:IRSA 的**两侧**都要具备 ——
      ① IAM 角色的信任策略信任韩国 OIDC 且 `:sub` 对上（7 个都做了）
      ② k8s 里**存在**带 `eks.amazonaws.com/role-arn` 注解的 ServiceAccount
    当时只建了 `petsite-sa` 一个，另外 6 个没建。

    失效形态很刁:**pod 根本不会被创建**（ReplicaSet 层
    `FailedCreate: serviceaccount "…" not found`），所以
    `kubectl get pod` 里一个异常 pod 都看不到 —— 任何遍历 pod 的健康检查
    对这个缺陷**完全失明**。

    所以 SA 对象改由同一份映射生成:两半永远不会再走散。
    """
    mapping = json.loads(
        (Path(__file__).resolve().parent.parent
         / "infra" / "dr-korea" / "irsa-korea-mapping.json").read_text(encoding="utf-8")
    )
    out = []
    for e in mapping["service_accounts"]:
        out.append({
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": e["name"],
                "namespace": e["namespace"],
                "annotations": {
                    # 少了这个注解的表现是:pod 起得来、每次 AWS 调用 403
                    "eks.amazonaws.com/role-arn":
                        f"arn:aws:iam::926093770964:role/{e['role']}",
                },
            },
        })
    return out


def referenced_configs(dep: dict) -> tuple[set[str], set[str]]:
    """找出这个 Deployment 引用的 ConfigMap 与 Secret 名字。

    ## 为什么必须扫

    2026-09-25 踩过:生成器拷了 `volumes`，但**没拷卷引用的 ConfigMap**。
    pethistory 挂 `otel-config`，韩国没有，于是:

        FailedMount: configmap "otel-config" not found

    pod 永远停在 `ContainerCreating`，而**原因只出现在 pod 事件里** ——
    容器状态里是空的 `ContainerCreating`，Deployment 的 conditions 也不提。
    任何只看容器状态的诊断对它失明。

    ⚠️ 只有 pethistory 挂了它，另外 5 个的 otel sidecar 用默认配置 ——
    所以「其它几个起来了」完全不能说明这一个也会起来。
    """
    cms: set[str] = set()
    secrets: set[str] = set()
    spec = dep["spec"]["template"]["spec"]

    for v in spec.get("volumes") or []:
        if "configMap" in v and v["configMap"].get("name"):
            cms.add(v["configMap"]["name"])
        if "secret" in v and v["secret"].get("secretName"):
            secrets.add(v["secret"]["secretName"])
        for src in (v.get("projected") or {}).get("sources") or []:
            if (src.get("configMap") or {}).get("name"):
                cms.add(src["configMap"]["name"])
            if (src.get("secret") or {}).get("name"):
                secrets.add(src["secret"]["name"])

    for c in spec.get("containers") or []:
        for ef in c.get("envFrom") or []:
            if (ef.get("configMapRef") or {}).get("name"):
                cms.add(ef["configMapRef"]["name"])
            if (ef.get("secretRef") or {}).get("name"):
                secrets.add(ef["secretRef"]["name"])
        for e in c.get("env") or []:
            vf = e.get("valueFrom") or {}
            if (vf.get("configMapKeyRef") or {}).get("name"):
                cms.add(vf["configMapKeyRef"]["name"])
            if (vf.get("secretKeyRef") or {}).get("name"):
                secrets.add(vf["secretKeyRef"]["name"])

    # kube-root-ca.crt 是每个 namespace 自带的，不需要搬
    cms.discard("kube-root-ca.crt")
    return cms, secrets


def convert_configmap(cm: dict) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": clean_metadata(cm["metadata"]),
        "data": cm.get("data") or {},
    }


def convert_service(svc: dict) -> dict:
    ports = []
    for p in svc["spec"]["ports"]:
        q = {"port": p["port"], "protocol": p.get("protocol", "TCP")}
        if p.get("name"):
            q["name"] = p["name"]
        if p.get("targetPort") is not None:
            q["targetPort"] = p["targetPort"]
        ports.append(q)
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": clean_metadata(svc["metadata"]),
        "spec": {
            # 与东京一致:ClusterIP。入口不靠 Service 类型，靠 TargetGroupBinding。
            "type": "ClusterIP",
            "selector": svc["spec"]["selector"],
            "ports": ports,
        },
    }


def is_private_ecr(img: str) -> bool:
    """这个镜像在我们自己的 ECR 里吗。

    ⚠️ 第一版的核实脚本**假设所有镜像都在私有 ECR**，于是把
    `public.ecr.aws/aws-observability/aws-otel-collector:v0.47.0` 报成
    「韩国缺失」—— 那是判据的错，不是真缺口。公共镜像每个 region 都能拉
    （只要有出网，韩国有 NAT），不需要回填。
    """
    host = img.partition("/")[0]
    return ".dkr.ecr." in host and ".amazonaws.com" in host


def verify_images(path: Path) -> int:
    """预检:清单引用的私有镜像，在韩国 ECR 里是不是都有。

    少一个的表现是 ImagePullBackOff —— 真切换时才发现就太晚了。
    """
    import yaml

    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    private: dict[tuple[str, str], list[str]] = {}
    public: dict[str, list[str]] = {}
    for d in docs:
        if d["kind"] != "Deployment":
            continue
        for c in d["spec"]["template"]["spec"]["containers"]:
            who = f"{d['metadata']['name']}/{c['name']}"
            img = c["image"]
            if is_private_ecr(img):
                repo = img.split("/", 1)[1].rsplit(":", 1)[0]
                tag = img.rsplit(":", 1)[-1]
                private.setdefault((repo, tag), []).append(who)
            else:
                public.setdefault(img, []).append(who)

    bad = 0
    for (repo, tag), users in sorted(private.items()):
        try:
            digest = _aws(
                "ecr", "describe-images", "--region", KOREA_REGION,
                "--repository-name", repo, "--image-ids", f"imageTag={tag}",
                "--query", "imageDetails[0].imageDigest", "--output", "text",
            )
        except RuntimeError as e:
            print(f"  ❌ 韩国 ECR 里没有 {repo}:{tag[:16]}…  用者={users}")
            print(f"      {e}")
            bad += 1
            continue
        print(f"  ✅ {repo.split('/')[-1][:34]:<34} {tag[:14]}…  {digest[7:21]}…")

    for img, users in sorted(public.items()):
        print(f"  ⬜ 公共镜像（每 region 可拉，无需回填）:{img}")
        print(f"      用者={len(users)} 个")

    if bad:
        print(f"\n  ❌ {bad} 个私有镜像在韩国缺失 —— 切换时会 ImagePullBackOff")
    else:
        print(f"\n  ✅ {len(private)} 个私有镜像齐全，{len(public)} 个走公共 registry")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--verify-images", action="store_true",
        help="只做镜像预检（不重新生成）",
    )
    args = ap.parse_args()

    if args.verify_images:
        return verify_images(Path(args.out))

    tokyo = Cluster(TOKYO_REGION, TOKYO_CLUSTER)
    deps = tokyo.get(f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments")["items"]
    svcs = tokyo.get(f"/api/v1/namespaces/{NAMESPACE}/services")["items"]

    by_name = {d["metadata"]["name"]: d for d in deps}
    missing = [w for w in WANTED if w not in by_name]
    if missing:
        print(f"❌ 东京集群里找不到:{missing}")
        print(f"   现有的是:{sorted(by_name)}")
        print("   —— 这说明东京侧变了，清单要重新确认，不要生成半份")
        return 2

    # ⚠️ ServiceAccount 必须排在 Deployment **前面** —— kubectl apply 按文件顺序，
    #    SA 不存在时 ReplicaSet 会 FailedCreate，而那时连 pod 都没有。
    objs: list[dict] = service_accounts_from_mapping()
    all_notes: list[str] = []
    # Service 要按 selector 匹配到对应的 Deployment，不按名字猜
    for name in WANTED:
        dep = by_name[name]
        obj, notes = convert_deployment(dep)
        objs.append(obj)
        all_notes += notes

        sel = dep["spec"]["selector"].get("matchLabels") or {}
        for svc in svcs:
            ssel = svc["spec"].get("selector") or {}
            if ssel and all(sel.get(k) == v for k, v in ssel.items()):
                objs.append(convert_service(svc))
                break
        else:
            all_notes.append(f"{name}: 没有匹配的 Service（selector={sel}）")

    # ── 把引用到的 ConfigMap 搬过来 ─────────────────────────────────
    need_cms: set[str] = set()
    need_secrets: set[str] = set()
    for name in WANTED:
        cms, secs = referenced_configs(by_name[name])
        need_cms |= cms
        need_secrets |= secs

    cm_objs: list[dict] = []
    for cm_name in sorted(need_cms):
        try:
            cm = tokyo.get(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{cm_name}")
        except RuntimeError as e:
            all_notes.append(f"ConfigMap `{cm_name}` 在东京也读不到:{e}")
            continue
        cm_objs.append(convert_configmap(cm))

    if need_secrets:
        # ⚠️ **刻意不拷 Secret 的内容** —— 那会把密钥material写进 git 跟踪的文件。
        #    只列出名字，作为切换前必须自行准备的前置条件。
        all_notes.append(
            "需要以下 Secret，**本文件刻意不含它们的内容**（不把密钥写进仓库）:"
            + ", ".join(f"`{x}`" for x in sorted(need_secrets))
        )

    # ConfigMap 要排在 Deployment 前面
    objs = objs[:len(service_accounts_from_mapping())] + cm_objs + objs[len(service_accounts_from_mapping()):]

    import yaml

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = f"""# 韩国侧的 petsite 周边工作负载（{len(WANTED)} 个 Deployment + 对应 Service）
#
# ⚠️⚠️ 这个文件是**生成的，不要手改**。
#      生成器:scripts/gen_korea_workloads.py
#      重新生成:python3.11 scripts/gen_korea_workloads.py --out {args.out}
#      本次生成:{stamp}
#      来源:{TOKYO_REGION} 集群 {TOKYO_CLUSTER} 的 namespace {NAMESPACE}（活集群）
#           + infra/dr-korea/irsa-korea-mapping.json（ServiceAccount 部分）
#
# ⚠️ ServiceAccount 与 IRSA 信任策略**同源**（都出自那份映射）。
#    2026-09-25 踩过:只建了 petsite-sa，另外 6 个没建，于是
#    ReplicaSet 报 FailedCreate、**pod 根本不会被创建** ——
#    任何遍历 pod 的健康检查对这个缺陷完全失明。
#
# 手改会在下次重新生成时丢掉。东京侧变更后重跑，diff 会直接告诉你变了什么。
#
# ── 这不是一份能让站点真正服务用户的配置 ──────────────────────────
# 环境变量的**值**仍指向东京的资源（SSM 前缀 /petstore 等），而韩国那些参数
# 不存在 —— 实测 petsite 会返回 HTTP 200 加一个错误页
# （根因 /petstore/searchapiurl ParameterNotFound）。
# 补齐 region 内后端属于手册第五节⑤，不是这个文件的范围。
"""
    if all_notes:
        header += "#\n# ── 生成时发现的注意事项 ────────────────────────────────────────\n"
        for n in sorted(set(all_notes)):
            header += f"#   - {n}\n"

    body = "\n---\n".join(
        yaml.safe_dump(o, sort_keys=False, allow_unicode=True, width=200)
        for o in objs
    )
    Path(args.out).write_text(header + "---\n" + body, encoding="utf-8")

    kinds: dict[str, int] = {}
    for o in objs:
        kinds[o["kind"]] = kinds.get(o["kind"], 0) + 1
    print(f"  已生成 {args.out}")
    print(f"  对象:{kinds}")
    for n in sorted(set(all_notes)):
        print(f"  ⚠️  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
