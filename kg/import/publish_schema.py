#!/usr/bin/env python3
"""幂等发布 ZwdmxGJ schema 到本机 OpenSPG（project 2）。

步骤：
1. 登录（凭据 kg/deploy/README.md §8，DB 哈希已重置一致）
2. 确认/创建 namespace=ZwdmxGJ 项目（复用 GovAffair 的模型连通性配置；
   graph_store 指向独立 Neo4j 库 zwdmxgj）
3. 读取 schemas/ZwdmxGJ-v0.3.schema，做本机适配后 POST /v1/schemas
   （saveSchema 按 schema 文本里的 namespace 定位项目，全量对账差异后提交）
4. 输出类型清单核对报告 kg/import/reports/schema_types.md

适配说明（不修改 schemas/ 源文件）：
- 本机 OpenSPG 基础类型无 Double，等价替换为 Float（仅影响 3 处 confidence，
  属阶段三/四 LLM 抽取实体，pilot 骨架层数据不含该字段）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from spg_client import SpgClient, SpgClientError, load_schema_catalog  # noqa: E402

SCHEMA_FILE = ROOT / "schemas" / "ZwdmxGJ-v0.3.schema"
REPORT_FILE = ROOT / "kg" / "import" / "reports" / "schema_types.md"
NAMESPACE = "ZwdmxGJ"
PROJECT_NAME = "ZwdmxGJ"
GRAPH_DATABASE = "zwdmxgj"
NEO4J_PASSWORD = "neo4j@openspg"  # kg/deploy docker-compose 实测
TEMPLATE_PROJECT_ID = 1  # GovAffair，复用其模型连通性配置


PROJECT_ID_FIXED = 2  # Mac 上 ZwdmxGJ 是项目 2；Windows 全新库自增为 1，以 _ensure_config 传入的实际 id 为准


def _rewrite_model_urls(cfg: dict[str, Any]) -> dict[str, Any]:
    """把 Mac 局域网地址改写为 host.docker.internal（Windows 上 mock_llm 端口 18999）。"""
    import json as _json
    text = _json.dumps(cfg, ensure_ascii=False)
    for stale in ("http://192.168.111.177:18999", "http://192.168.31.80:18999",
                  "http://192.168.111.177:11434", "http://192.168.31.80:11434"):
        text = text.replace(stale, "http://host.docker.internal:18999")
    return _json.loads(text)


def corrected_config(raw_config: dict[str, Any] | None, *, for_create: bool = False,
                      project_id: int = PROJECT_ID_FIXED) -> dict[str, Any]:
    """返回可用的项目 config：修正 project 段 id/namespace 与 graph_store。

    关键坑（2026-08-30 实测）：kag Python 侧 init_env() 优先读项目 config 内
    "project" 段的 id/namespace；从 GovAffair 复制的 config 若保留 id=1，
    会导致 Builder 的 Mapping 组件拉错 schema（SPG type does not exist），
    并把 KAG_PROJECT_ID 环境变量回写为 1。
    """
    config = dict(raw_config or {})
    config["project"] = {
        "biz_scene": "default",
        "namespace": NAMESPACE,
        "language": "zh",
        "checkpoint_path": "./builder/ckpt",
        "id": str(project_id),
        "host_addr": "http://127.0.0.1:8887",
    }
    # graph_store 密码在 API 返回中被打码；用真实密码覆盖并指向独立库
    config["graph_store"] = {
        "uri": "neo4j://release-openspg-neo4j:7687",
        "user": "neo4j",
        "password": NEO4J_PASSWORD,
        "database": GRAPH_DATABASE,
    }
    config = _rewrite_model_urls(config)
    return config


def adapt_schema_text(text: str) -> tuple[str, list[str]]:
    """本机 OpenSPG 版本适配，返回 (适配后文本, 变更说明列表)。"""
    changes: list[str] = []
    adapted = re.sub(r":\s*Double\s*$", ": Float", text, flags=re.M)
    if adapted != text:
        count = sum(1 for a, b in zip(text.splitlines(), adapted.splitlines()) if a != b)
        changes.append(f"Double -> Float（{count} 行，confidence 字段，本机基础类型无 Double）")
    return adapted, changes


def parse_declared_types(text: str) -> tuple[set[str], set[tuple[str, str, str]]]:
    """从 MarkLang 文本解析声明的实体与关系（按 properties:/relations: 段落区分）。"""
    entities: set[str] = set()
    relations: set[tuple[str, str, str]] = set()
    current: str | None = None
    section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            header = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\([^)]*\):\s*EntityType\s*$", stripped)
            if header:
                current = header.group(1)
                entities.add(current)
                section = None
            else:
                current = None
                section = None
            continue
        indent = len(line) - len(line.lstrip())
        if stripped.endswith(":") and indent <= 8:
            # 实体直属段落标记；关系块内嵌套 properties:（缩进 >8）不改变段落
            section = stripped.rstrip(":")
            continue
        pair = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\([^)]*\):\s*([A-Za-z][A-Za-z0-9_.]*)\s*$", stripped)
        if pair and current and section == "relations" and indent == 8:
            relations.add((current, pair.group(1), pair.group(2).rsplit(".", 1)[-1]))
    return entities, relations


def load_reference_config() -> dict[str, Any] | None:
    """Windows 全新栈：模板项目 1 不存在时，回退用 Mac 迁移来的项目 2 config
    （checkpoints/mac_p2_config_reference.json），并把 192.168.x 网段的
    mock/ollama 地址改写为 host.docker.internal（mock_llm_threaded.py 端口 18999）。"""
    import json
    ref = Path(__file__).resolve().parent / "checkpoints" / "mac_p2_config_reference.json"
    if not ref.exists():
        return None
    raw = ref.read_text(encoding="utf-8")
    start = raw.index("{")
    text = raw[start:]
    for stale in ("http://192.168.111.177:18999", "http://192.168.31.80:18999",
                  "http://192.168.111.177:11434", "http://192.168.31.80:11434"):
        text = text.replace(stale, "http://host.docker.internal:18999")
    return json.loads(text)


def ensure_project(client: SpgClient) -> dict[str, Any]:
    projects = client.retry("读取项目列表", lambda: _list_projects(client))
    existing = next((item for item in projects if item.get("namespace") == NAMESPACE), None)
    if existing is None:
        template = client.get_project(TEMPLATE_PROJECT_ID)
        base_config = (template or {}).get("config") if template else load_reference_config()
        if not base_config:
            raise SpgClientError(
                f"模板项目 {TEMPLATE_PROJECT_ID} 不存在，且找不到 checkpoints/mac_p2_config_reference.json"
            )
        config = corrected_config(base_config, for_create=True)
        payload = {
            "name": PROJECT_NAME,
            "description": "政务服务统一知识图谱（法人+个人）v0.3 骨架层",
            "namespace": NAMESPACE,
            "visibility": "PRIVATE",
            "tag": "LOCAL",
            "config": config,
        }
        project_id = client.retry("创建项目", lambda: client.create_project(payload))
        print(f"已创建项目 {NAMESPACE}: id={project_id}（Neo4j 库 {GRAPH_DATABASE}）")
        created = client.get_project(project_id)
        _ensure_config(client, created or {"id": project_id, "namespace": NAMESPACE})
        return created or {"id": project_id, "namespace": NAMESPACE}
    _ensure_config(client, existing)
    return existing


def _ensure_config(client: SpgClient, project: dict[str, Any]) -> None:
    """确保项目 config 的 project 段与 graph_store 正确（幂等自愈）。"""
    full = client.get_project(int(project["id"]))
    if not full:
        return
    raw = full.get("config") or {}
    project_section = raw.get("project") or {}
    graph_store = raw.get("graph_store") or {}
    needs_fix = (
        str(project_section.get("id")) != str(full["id"])
        or project_section.get("namespace") != full.get("namespace")
        or graph_store.get("database") != GRAPH_DATABASE
        or graph_store.get("password") in (None, "", "******")
    )
    if not needs_fix:
        return
    config = corrected_config(raw, project_id=int(full["id"]))
    payload = {
        "name": full["name"],
        "description": full.get("description") or "",
        "namespace": full["namespace"],
        "visibility": full.get("visibility") or "PRIVATE",
        "tag": full.get("tag") or "LOCAL",
        "config": config,
    }
    client.json_request(f"/v1/projects/{int(full['id'])}", method="PUT", payload=payload, timeout=300)
    print(f"已修正项目 {full['id']} config（project 段 id/namespace 或 graph_store）")


def _list_projects(client: SpgClient) -> list[dict[str, Any]]:
    data = client.json_request("/v1/projects/list", params={"pageSize": 200, "pageNum": 1})
    result = data.get("result")
    items: list[dict[str, Any]] = []
    if isinstance(result, dict):
        items = list(result.get("data") or result.get("records") or result.get("list") or [])
    elif isinstance(result, list):
        items = list(result)
    return [x for x in items if isinstance(x, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8887")
    parser.add_argument("--schema-file", type=Path, default=SCHEMA_FILE)
    parser.add_argument("--skip-publish", action="store_true", help="只核对不提交")
    args = parser.parse_args()

    schema_text = args.schema_file.read_text(encoding="utf-8")
    ns_line = schema_text.splitlines()[0].strip()
    if f"namespace {NAMESPACE}" not in ns_line:
        raise SystemExit(f"schema 文件首行 namespace 不符: {ns_line}")
    adapted, changes = adapt_schema_text(schema_text)
    declared_entities, declared_relations = parse_declared_types(schema_text)

    client = SpgClient(args.base_url)
    client.login()
    project = ensure_project(client)
    project_id = int(project["id"])
    print(f"项目: id={project_id} namespace={project.get('namespace')}")

    schema_now = client.query_project_schema(project_id)
    catalog = load_schema_catalog(schema_now)
    present_entities = set(catalog["entities"])
    present_relations = set(catalog["relations"])

    missing_entities = declared_entities - present_entities
    if missing_entities and not args.skip_publish:
        print(f"缺少实体 {len(missing_entities)} 个，提交 schema …")
        result = client.save_schema(adapted)
        diff = result.get("result") or {}
        print(f"提交结果: ADD={len(diff.get('ADD') or [])} "
              f"UPDATE={len(diff.get('UPDATE') or [])} DELETE={len(diff.get('DELETE') or [])}")
        schema_now = client.query_project_schema(project_id)
        catalog = load_schema_catalog(schema_now)
        present_entities = set(catalog["entities"])
        present_relations = set(catalog["relations"])
        missing_entities = declared_entities - present_entities
    if missing_entities:
        print(f"ERROR: schema 发布后仍缺实体: {sorted(missing_entities)}")
        return 1

    # 核对报告
    lines = [
        "# ZwdmxGJ schema 发布核对报告",
        "",
        f"- 项目: id={project_id} namespace={NAMESPACE}",
        f"- schema 文件: `{args.schema_file.relative_to(ROOT)}`",
        f"- 声明实体: {len(declared_entities)}；已发布实体: {len(present_entities)}",
        f"- 声明关系: {len(declared_relations)}；已发布关系: {len(present_relations)}",
    ]
    lines.append("")
    lines.append("适配变更：" + ("；".join(changes) if changes else "无"))
    lines.append("")
    lines.append("| 实体 | 属性数（含 id） | 关系 | 缺失 |")
    lines.append("|---|---|---|---|")
    for name in sorted(declared_entities):
        entity = catalog["entities"][name]
        rels = [k for k in catalog["relations"] if k[0] == name]
        lines.append(
            f"| {name} | {len(entity['properties'])} | "
            f"{', '.join(f'{r}->{t}' for _, r, t in sorted(rels)) or '-'} | |"
        )
    extra = present_entities - declared_entities
    lines.append("")
    lines.append(f"项目内多余实体（不删除）: {sorted(extra) or '无'}")
    missing_rels = declared_relations - present_relations
    if missing_rels:
        lines.append(f"缺失关系: {sorted(missing_rels)}")
    else:
        lines.append("缺失关系: 无（声明关系全部已发布）")
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"核对报告: {REPORT_FILE}")
    if missing_rels:
        print(f"ERROR: 缺失关系 {sorted(missing_rels)}")
        return 1
    print("schema 发布核对通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
