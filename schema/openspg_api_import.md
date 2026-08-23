# OpenSPG 规则化 CSV 接口批量导入

## 结论

规则化 CSV 不需要逐个通过图形界面上传和匹配 Schema。本项目使用：

```text
CSV 文件
  -> POST /public/v1/reasoner/dialog/uploadFile
  -> 获得 MinIO fileUrl
  -> 读取已成功 Builder Job 的 Schema 元数据
  -> 按 openspg_import_manifest.json 覆盖字段映射
  -> POST /public/v1/builder/job/submit
  -> GET /public/v1/builder/job/get 轮询状态
```

项目固定参数：

```text
project_id: 2
namespace: ZwdmxGJ
OpenSPG: http://127.0.0.1:8887
```

## 文件

```text
schema/openspg_import_manifest.json  CSV、模板任务、Schema字段映射清单
scripts/import_openspg_csvs.py       流式、分片、断点续传导入程序
scripts/shard_openspg_csvs.py        按完整 CSV 记录预生成分片
schema/openspg_sharded_import_manifest.json  分片策略参数
build/openspg-import-state.json      分片 SHA256 与任务状态
build/openspg-import-preview/        dry-run请求体和执行报告
```

脚本只依赖 Python 标准库，不需要安装 `requests`、PyYAML 或 OpenSPG SDK。

## 为什么复用模板任务

OpenSPG 的映射请求除了字段名，还包含 Schema 内部元数据：

```text
实体：s、sId、sZhName
关系：p、pId、pZhName、s、o
```

这些 ID 可能在 Schema 重建或重新发布后变化。脚本从已经在界面创建并成功执行的任务中读取这些元数据，再用 manifest 中的映射覆盖 `mappingConfig.config[0].mapping`。因此不需要把内部 ID 手写到脚本中。

## 安全默认值

不带 `--execute` 时只执行：

```text
检查文件存在
检查CSV表头
检查CSV列与manifest完全一致
检查关系起点/终点没有重复映射
读取模板Builder Job
检查namespace
生成提交payload
生成SHA-256和报告
```

不会上传文件，也不会修改图谱。

```bash
cd /Users/mac/CODE/kg-research/GovGraphRAG-SubLab
python3 scripts/import_openspg_csvs.py --all
```

## 常用命令

### 1. 检查单个文件

```bash
python3 scripts/import_openspg_csvs.py --only services
```

### 2. 检查全部实体CSV

```bash
python3 scripts/import_openspg_csvs.py --group entities
```

### 3. 检查全部关系CSV

```bash
python3 scripts/import_openspg_csvs.py --group relations
```

### 4. 新建一个Builder Job并等待完成

默认 `clone` 会新建任务，但导入动作仍是 `UPSERT`：

```bash
python3 scripts/import_openspg_csvs.py \
  --only services \
  --execute \
  --wait \
  --name-suffix=-法人数据更新-20260812
```

### 5. 更新并重跑已有任务

任务已经存在时优先使用 `update`，避免任务列表不断增加：

```bash
python3 scripts/import_openspg_csvs.py \
  --only services \
  --mode update \
  --execute \
  --wait
```

### 6. 按顺序更新全部实体

```bash
python3 scripts/import_openspg_csvs.py \
  --group entities \
  --mode update \
  --execute \
  --wait
```

### 7. 按顺序更新全部关系

关系两端实体必须先存在：

```bash
python3 scripts/import_openspg_csvs.py \
  --group relations \
  --mode update \
  --execute \
  --wait
```

### 8. 全量更新

全量脚本强制按“实体 → 关系 → Chunk”排序。一般建议分组执行，便于定位失败；确认需要全量执行时：

```bash
python3 scripts/import_openspg_csvs.py \
  --all \
  --mode update \
  --execute \
  --wait
```

## 任务Key

文档：

```text
documents_chunks
```

实体：

```text
services
departments
materials
conditions
process_steps
results
legal_bases
faqs
service_channels
fees
```

关系：

```text
service_handled_by
service_collaborates_with
service_requires_material
service_has_condition
service_has_process_step
process_step_next
service_produces_result
service_based_on
service_has_faq
service_has_channel
service_has_fee
```

`--only` 可以重复使用：

```bash
python3 scripts/import_openspg_csvs.py \
  --only services \
  --only departments \
  --only materials \
  --mode update \
  --execute \
  --wait
```

## Chunk与Retrieval

个人事务全量 `documents.csv` 约 18 GiB。为避免再落盘一份体积相近或更大的完整 `documents_chunks.csv`，个人 manifest 为 `documents_chunks` 声明：

```json
"rolling_source_file": "dataset/normalized/openspg/personal/documents.csv",
"rolling_transform": "documents_to_chunks"
```

导入器会执行以下流水线：

```text
documents.csv 流式读取
  -> 按 2,000 字符、200 字符 overlap 生成 Chunk
  -> 写入单个 128 MiB CSV 分片
  -> 流式上传并提交 Builder
  -> 等待成功
  -> 删除该分片
  -> 继续下一片
```

因此磁盘峰值只增加约一个分片，而不是完整 Chunk CSV。执行命令：

```bash
python3 scripts/import_openspg_csvs.py \
  --manifest schema/openspg_personal_import_manifest.json \
  --group documents \
  --rolling-shards \
  --target-mib 128 \
  --min-free-gib 15 \
  --chunk-max-chars 2000 \
  --chunk-overlap-chars 200 \
  --execute --wait --delete-successful-shards \
  --name-suffix=-个人事务Chunk-20260812
```

`--rolling-shards --execute` 强制要求 `--wait`，防止 Builder 尚未成功时删除本地分片。断点状态仍按 `job_key + part_no + SHA256` 记录；重跑时会重新流式扫描源文档，并跳过 SHA256 已成功的分片。

`documents_chunks` 复用任务7，保留：

```text
retrievals: [1]
```

对应 `chunk_index`。其他实体和关系任务不绑定 Retrieval。这样可以保持：

```text
业务实体/关系 -> 图查询
Chunk.content -> 文本和向量召回
```

## 当前发现的映射修正

原任务23把 `process_step_next.csv` 错误映射为：

```text
service_id            -> start_id
from_process_step_id  -> end_id
to_process_step_id    -> 未映射
```

manifest 已修正为：

```text
service_id            -> 不导入，仅作来源追踪
from_process_step_id  -> start_id
to_process_step_id    -> end_id
```

下次更新该关系应执行：

```bash
python3 scripts/import_openspg_csvs.py \
  --only process_step_next \
  --mode update \
  --execute \
  --wait
```

## 修改CSV或Schema后的维护方式

### CSV列不变

直接替换同名CSV，再运行对应 `--only KEY --mode update --execute --wait`。

### CSV新增列

在 `schema/openspg_import_manifest.json` 的对应 `mapping` 中增加：

```json
"csv_column": ["schemaProperty"]
```

不导入的追踪字段使用：

```json
"csv_column": []
```

### Schema重新发布

如果 EntityType/Relation 的内部 ID 发生变化：

1. 通过界面为该类型建立一次新的正确模板任务；
2. 把 manifest 中的 `template_job_id` 改成新任务 ID；
3. 重新 dry-run；
4. 再执行接口导入。

## 认证

当前本机接口实测无需登录也能调用。如果以后启用认证：

```bash
export OPENSPG_TOKEN='TOKEN'
python3 scripts/import_openspg_csvs.py --only services --execute --wait
```

或传完整Cookie：

```bash
python3 scripts/import_openspg_csvs.py \
  --cookie 'OPEN_SPG_TOKEN=TOKEN; ctoken=TOKEN' \
  --only services \
  --execute \
  --wait
```

## 不采用的方式

不直接写 Neo4j，因为这会绕过 Builder、Schema 校验、任务状态和 Retrieval/向量构建；也不直接向 `kg_builder_job` 插入任务，因为完整执行还涉及 MinIO、Scheduler 和构建 DAG。


## 大文件分片、流式上传与断点续传

Builder 单文件上限约 200 MiB。全量数据推荐 128 MiB 分片；分片始终在完整 CSV 记录边界结束，并重复 UTF-8 BOM 与表头。

### 低磁盘峰值：滚动分片导入

一次只生成一个分片；上传、提交并确认 Builder 成功后立即删除该本地分片：

```bash
python3 scripts/import_openspg_csvs.py \
  --manifest schema/openspg_personal_import_manifest.json \
  --group entities \
  --rolling-shards \
  --target-mib 128 \
  --min-free-gib 15 \
  --execute --wait --delete-successful-shards \
  --name-suffix=-个人事务全量-20260812
```

实体成功后依次执行关系和 Chunk：

```bash
python3 scripts/import_openspg_csvs.py --manifest schema/openspg_personal_import_manifest.json --group relations --rolling-shards --target-mib 128 --execute --wait --delete-successful-shards --name-suffix=-个人事务全量-20260812
python3 scripts/import_openspg_csvs.py --manifest schema/openspg_personal_import_manifest.json --group documents --rolling-shards --target-mib 128 --execute --wait --delete-successful-shards --name-suffix=-个人事务全量-20260812
```

状态默认写入 `build/openspg-import-state.json`。键由任务 key、分片序号和 SHA256 组成；重跑相同命令时跳过已成功分片。`SUBMITTED` 状态在带 `--wait` 重跑时会先查询原任务，确认成功后再跳过。

### 预生成分片

空间充足或需要离线审计时：

```bash
python3 scripts/shard_openspg_csvs.py \
  --manifest schema/openspg_personal_import_manifest.json \
  --group entities --target-mib 128 --overwrite

python3 scripts/import_openspg_csvs.py \
  --manifest schema/openspg_personal_import_manifest.json \
  --group entities \
  --shard-manifest build/openspg-shards/shard_manifest.json \
  --execute --wait --delete-successful-shards
```

上传请求不使用 `Path.read_bytes()`，默认以 1 MiB 文件块流式发送 multipart body。可用 `--upload-chunk-mib` 调整网络读取块大小，用 `--retries` 和 `--retry-backoff` 控制指数退避重试。
