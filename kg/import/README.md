# kg/import —— OpenSPG 灌图管线（pilot 骨架层）

把 `data/pilot`（法人 481,501 事项）+ `build/shared_ids/pilot/` 共享 id 重写产物 +
路由层元数据灌入本机 OpenSPG（namespace `ZwdmxGJ`，Neo4j 独立库 `zwdmxgj`）。

## 文件

| 文件 | 作用 |
|---|---|
| `spg_client.py` | 登录（POST /v1/accounts/login，Cookie `OPEN_SPG_TOKEN`）+ REST 封装：上传、Builder Job 提交/轮询、schema 查询、payload catalog 解析 |
| `publish_schema.py` | 幂等发布：确保项目 2 存在且 config 正确 → 提交 `schemas/ZwdmxGJ-v0.3.schema`（MarkLang）→ 输出类型核对报告 `reports/schema_types.md` |
| `generate_routing_metadata.py` | 生成路由层 CSV（ServiceDomain/CategoryScheme/ServiceCategory/KnowledgeModel + 9 张关系表）到 `routing_metadata/` |
| `build_manifest.py` | 生成 `manifests/pilot_import_manifest.json`（36 任务：路由→共享→弱实体→services→关系），校验列映射与实时 Schema 一致 |
| `run_import.py` | 导入器：行数+字节双上限滚动分片 → 上传 MinIO → Builder Job → 轮询 → checkpoint（SHA256 断点续传，多进程文件锁合并写） |
| `verify_graph.py` | 对账：Neo4j 实际数 vs 源 CSV 期望数（节点=去重主键，边=distinct(s,o)，大表 sqlite 落盘去重）+ 5 条多跳链抽查 → `reports/pilot_reconciliation.md` |
| `mock_llm_threaded.py` | kg/deploy/mock_llm.py 的多线程变体（Builder 向量化阶段 10-20 并发打 /v1/embeddings，单线程版是瓶颈） |

`checkpoints/`（state.json + 运行日志）与 `shards/` 已 gitignore，可随时删除后由源数据重建。

## 标准执行顺序

```bash
python3 kg/import/publish_schema.py                  # 0. 项目+schema 幂等发布
python3 kg/import/generate_routing_metadata.py       # 1. 路由层 CSV
python3 kg/import/build_manifest.py                  # 2. manifest 生成+校验
python3 kg/import/run_import.py --tables KEY --execute --wait   # 3. 逐表导入
python3 kg/import/run_import.py --all --execute --wait --continue-on-error  # 或全量
python3 kg/import/verify_graph.py                    # 4. 对账报告
```

## 本机环境要点（2026-08-30 实测）

1. **登录**：UI 账号 `openspg / openspg@kag2026`（与 kg/deploy/README.md §8 一致）。
   DB 种子哈希（`kg_user.dw_access_key = sha256Hex(password + salt)`，salt 取 `kg_user.salt`）
   与该密码不匹配（MySQL 卷曾重置），已在 DB 中重置回 README 记录的密码。
   `/public/**` 免认证；`/v1/**` 需 Cookie `OPEN_SPG_TOKEN`（登录后 12h 有效）。
2. **项目 2 config 陷阱**：kag Python 侧 `init_env()` 优先读项目 config 内 `project` 段的
   `id/namespace`。从 GovAffair 复制 config 若保留 `id:"1"`，Builder Mapping 会拉错 schema
   （`SPG type does not exist`）并把 `KAG_PROJECT_ID` 回写为 1。`publish_schema.py` 会自动修正。
3. **schema 适配**：本机 OpenSPG 基础类型无 `Double`，发布时内存等价替换为 `Float`
   （仅 3 处 confidence，属阶段三/四实体，pilot 骨架不含）。
4. **OOM 边界**：scanner/mapping 把整片行载入 JVM+Python 内存。实测 1.3M 行/片 OOM、
   350K 行/片安全 → 分片用 `max_rows=350K` 与 `128MiB` 双上限（`--max-rows-part`）。
5. **吞吐瓶颈**：server 每次 Pemja invoke 新建 Python 解释器（无复用），单任务
   scanner/mapping/vectorize/write 各阶段约 50 行/s；靠 3-5 个并发导入进程聚合吞吐。
   并发过高（6+，或 services 这类长行表）会触发堆抖动（GC overhead），需降并发等自愈。
6. **实体缺 name 会被静默丢弃**（`Neo4jSinkWriter - write Node ignore node`）：
   conditions.csv 的 condition_name 全空，分片时回退 `name = condition_text[:64]`。
7. **mock 嵌入服务**：vectorize 阶段会为 TextAndVector 属性调 `/v1/embeddings`
   （哪怕表内没有该属性也会逐批过 vectorize operator）。宿主机需跑
   `python3 kg/import/mock_llm_threaded.py 18999`（server 配置指向 192.168.31.80:18999，
   本机 IP 变化时需同步项目 config 的 vectorizer/openie_llm base_url）。
8. **断点续传**：checkpoint 键 = `job_key:part_no:sha256`。杀进程/重启 server 容器后重跑
   同一命令，SUCCESS 分片自动跳过；服务端调度器也会从 MinIO checkpoint 恢复未完成 job。

## 对账口径

- 节点期望 = 源表去重主键数（共享实体取重写产物行数 = 共享节点数）。
- 边期望 = 源表 distinct (start, end) 对数 —— SPG (s,p,o) UPSERT 语义：重复三元组合并为
  一条边（同 (s,o) 不同边属性取后写覆盖），`service_based_on_out` 6.3M 行按
  distinct (service, LC) 对去重后为期望值。
