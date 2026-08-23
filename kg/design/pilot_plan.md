# 试点执行计划：1 万条政务事项结构化建图

> 目标：用 `kg/pilot/pilot_10000.jsonl`（10,000 条分层抽样）跑通
> OpenSPG/KAG 纯结构化建图（无 LLM 抽取、不挂向量化），验证 Schema 与适配器。
> 依据：`kg/design/kag_notes.md`（机制研读）、`kg/design/schema_design.md`（建模决策）。
> 前置状态：OpenSPG Server 已以 docker 部署，`http://127.0.0.1:8887` 产品界面正常。

## 0. 模型配置清单（KAG 的强制项与可占位项）

读源码确认（`knext/command/sub_command/project.py`）：`knext project create` 与
`knext project update` 会**实际调用** `chat_llm`（发一条 "who are you?"）和
`vectorizer`（对 "hello" 向量化并校验维度），不通即退出；`enable_check: false` 不能绕过。

| 配置项 | 试点是否需要 | 说明 |
|---|---|---|
| `chat_llm` | **必须可连通** | project create/update 强校验。任何 OpenAI 兼容 chat 端点即可（type: maas/openai），试点建图本身不消费它 |
| `vectorizer` | **必须可连通** | 同上强校验（含维度回写）。任何 OpenAI 兼容 embedding 端点即可 |
| `openie_llm` | 可占位 | 只有非结构化抽取链用它；纯结构建图不消费，create 也不校验 |
| `chain_vectorizer` / solver 各节 | 不需要 | 自建 chain 不挂 BatchVectorizer；solver 配置试点阶段用不到 |

可选落地方案（任选其一）：a) 现有云端 key（阿里百炼/硅基流动等 OpenAI 兼容端点）；
b) 本地 ollama 起一个小 chat 模型 + bge-m3 embedding 充当连通性桩；
c) 任何返回合法 OpenAI 响应的本地 mock 服务（仅用于通过 create/update 校验）。

## 1. 环境与项目初始化

```bash
# 若未装 KAG 客户端（python3.10 环境）：
# conda create -n kag python=3.10 -y && conda activate kag
# cd kg/_ref/KAG && pip install -e .        # 用本地已克隆仓库，或重新 git clone

REPO=/Volumes/f/AllMyData/MyUnderGraduate/政务大模型
cd $REPO/kg/pilot
```

准备项目配置 `kg/pilot/project_init.yaml`（占位项按上表填真实可连通端点）：

```yaml
openie_llm: &openie_llm
  type: maas
  base_url: <任一OpenAI兼容端点>     # 占位即可，试点不消费
  api_key: <key>
  model: <model>
  enable_check: false

chat_llm: &chat_llm
  type: maas
  base_url: <可连通的OpenAI兼容端点>  # create/update 会实际调用
  api_key: <key>
  model: <model>
  enable_check: false

vectorize_model: &vectorize_model
  type: openai
  base_url: <可连通的embedding端点>   # create/update 会实际调用并校验维度
  api_key: <key>
  model: <model>
  vector_dimensions: <维度>           # 校验后会被自动回写
  enable_check: false
vectorizer: *vectorize_model

log:
  level: INFO

project:
  biz_scene: default
  host_addr: http://127.0.0.1:8887
  language: zh
  namespace: GovAffair                # 须 ^[A-Z][A-Za-z0-9]{0,15}$，当前值合规
  checkpoint_path: ./builder/ckpt
```

创建项目（在服务端登记 + 渲染本地模板目录 `GovAffair/`）：

```bash
knext project create --config_path project_init.yaml
# 预期：GovAffair/ 目录生成，内含 kag_config.yaml（已写入 project.id）、schema/、builder/ 等
```

## 2. 提交 Schema

```bash
# 用我们的 schema 覆盖模板生成的同名文件（knext schema commit 只读 schema/<Namespace>.schema）
cp $REPO/kg/design/GovAffair.schema GovAffair/schema/GovAffair.schema
cd GovAffair
knext schema commit
# 预期输出：Schema is successfully committed.
# 验证点 A：产品界面 8887 → 项目 GovAffair → schema 图可见 7 实体 + 4 概念类型。
```

## 3. 生成构建输入 CSV

```bash
python3 $REPO/kg/build/adapter.py \
    --input "$REPO/kg/pilot/pilot_10000.jsonl" \
    --output $REPO/kg/pilot/csv
# 预期：12 个 CSV + adapter_stats.json；行数规模参考 §6 预期值。
```

## 4. 执行建图

```bash
cd $REPO/kg/pilot/GovAffair   # 必须在项目目录内（向上找 kag_config.yaml）
python $REPO/kg/build/indexer.py --data-dir $REPO/kg/pilot/csv
```

- 链：`SafeCSVScanner(keep_default_na=False) >> SPGTypeMapping/RelationMapping >> KGWriter`，
  不挂 vectorizer（纯结构）。
- 导入顺序（indexer.py 内置）：概念 4 表 → 共享实体 3 表 → 弱实体 3 表 → Affair → 关系 3 表。
- 中断续跑：`--only Affair,Affair_requireMaterial_Material`（同 id 重复导入为覆盖语义，可安全重跑）。
- 验证点 B：`KGWriter` 无批量报错；adapter_stats.json 各表行数与日志导入条数一致。

## 5. 图谱查验

产品界面：`http://127.0.0.1:8887` → 项目 GovAffair → 图数据/图谱浏览。

命令行抽查（在项目目录内运行）：

```python
from knext.search.client import SearchClient
from knext.graph.client import GraphClient
from kag.common.conf import KAG_PROJECT_CONF

sc = SearchClient(KAG_PROJECT_CONF.host_addr, KAG_PROJECT_CONF.project_id)
gc = GraphClient(KAG_PROJECT_CONF.host_addr, KAG_PROJECT_CONF.project_id)

# 1) 全文命中：按事项名检索
print(sc.search_text("中小学地方课程教材初审", label_constraints=["GovAffair.Affair"], topk=3))

# 2) 按 id 取节点 + 一跳展开（材料/法条/环节/概念挂载）
v = gc.query_vertex(type_name="Affair", biz_id="<事项编码>")
print(v)
print(gc.expend_one_hop(vertex=v, type_names=["Material", "LegalBasis", "ProcessStep"]))
```

查验清单（预期值以 `kg/pilot/csv/adapter_stats.json` 为准）：

- [ ] Affair 节点数 = affairs_written（≈10,000 减去编码重复）；
- [ ] 随机抽 5 个 Affair：affairType/serviceTarget/exerciseLevel/theme 概念边存在，
      概念 isA 链可查（如 `法人-企业法人` isA `法人`、`行政权力-行政许可` isA `行政权力`）；
- [ ] requireMaterial 边属性（份数/是否必要/说明）可读；hasLegalBasis 边属性 article 可读；
- [ ] ProcessStep 的 nextStep 链长度 = 该事项环节数-1；
- [ ] 同一文号 LegalBasis 被多个 Affair 引用（跨事项共享生效）；材料同名共享同理；
- [ ] 无 "nan" 字符串属性（SafeCSVScanner 生效）；无悬空边（端点均存在）。

## 6. 试点通过标准与后续

- 通过标准：上述查验全绿 + 图规模与 adapter_stats.json 一致。
- 不通过的典型排查：schema 未 commit（SPGTypeMapping assert 失败）→ 回 §2；
  列名不匹配（KeyError）→ 对表头与 schema 属性名；乱码/错位 → 确认 UTF-8 与 csv 转义。
- 通过后进入全量评估：对 `data/unified/*.jsonl` 全量跑 adapter（157 万条），
  分批灌图；随后再考虑向量索引生效化与 supporting_chunks 非结构化补充链。

## 7. 清理（如需重跑）

```bash
rm -rf GovAffair/builder/ckpt     # checkpoint
# 图数据删除：产品界面项目内删除，或 KGWriter(delete=True) 反向跑一遍（本计划暂不需要）
```
