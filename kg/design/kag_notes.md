# KAG / OpenSPG 研读笔记（结构化建图路线）

> 来源：本地浅克隆仓库 `kg/_ref/KAG`（master，2026-08-09 克隆，--depth 1），
> 重点研读 `kag/examples/supplychain`、`kag/examples/medicine`、`kag/examples/domain_kg`，
> 以及 `knext/command`、`kag/builder/component/{mapping,scanner,writer}`、`knext/schema/marklang/schema_ml.py` 源码。
> 官方文档（语雀）：快速上手/开发者模式 https://openspg.yuque.com/ndx6g9/cwh47i/rs7gr8g4s538b1n7 ，
> Schema 语法 https://openspg.yuque.com/ndx6g9/cwh47i/fiq6zum3qtzr7cne 。

## 1. 开发者模式全貌

- 服务端：OpenSPG Server 以 docker-compose 部署（`openspg.yuque.com` 文档或
  `docker-compose-west.yml`），产品入口 `http://127.0.0.1:8887`（默认账号 openspg / openspg@kag）。
- 客户端：Python 3.10 环境 `pip install -e .`（KAG 仓库根），得到 `knext` CLI 与 `kag` python 包。
- 一个 KAG 项目 = 服务端 project（有数字 id）+ 本地项目目录（含 `kag_config.yaml`、`schema/<Namespace>.schema`、
  `builder/`、`solver/`、`reasoner/`）。`knext` 命令从当前目录向上找最近的 `kag_config.yaml`
  定位项目（见 `knext/common/env.py`），因此**所有 knext/builder 命令必须在项目目录内执行**。

## 2. .schema 文件语法（SPG MarkLang）

经 `knext/schema/marklang/schema_ml.py` 与示例验证的语法要点：

```
namespace SupplyChain                      # 首行命名空间（= 项目 namespace）

Industry(产业): ConceptType                 # 类型声明：英文名(中文名): EntityType|ConceptType|EventType
    hypernymPredicate: isA                 # ConceptType 专用，isA / locateAt / mannerOf

Product(产品): EntityType
    properties:                            # 属性块：标量属性 + 语义属性（值为其他 SPG 类型）
        name(名称): Text
        belongToIndustry(所属产业): Industry          # 语义属性：值是 ConceptType/EntityType
        hasSupplyChain(供应链): Product
            constraint: MultiValue                    # 约束：MultiValue / NotNull / Enum(...) / Regular(...)
        IND#belongTo(所属分类): TaxOfProduct          # 可带语义谓词前缀 IND#/CAU#/STD# 等
        desc(描述): Text
            index: TextAndVector           # 索引标注：Text / Vector / SparseVector / TextAndVector / TextAndSparseVector
        amount(金额): Float                # 基础类型只有 Text / Integer / Float（另有内置 Chunk）
    relations:                             # 关系块：可携带边属性、可挂规则
        fundTrans(资金往来): Company
            properties:                    # 边子属性
                transDate(交易日期): Text
                transAmt(交易金额): Integer
        mainSupply(主要客户): Company
            rule: [[                        # 逻辑规则（可选），DSL 定义推导关系
                Define (s:Company)-[p:mainSupply]->(o:Company) { ... }
            ]]
```

- 规则体关键字：`STRUCTURE/Structure`、`CONSTRAINT/Constraint`、`ACTION/Action`（大小写两可，示例中混用）。
- 概念规则（按概念实例定义隶属规则）写在单独的 `concept.rule` 文件，用 `knext schema reg_concept_rule --file xxx.dsl` 注册。
- 类型级可配项：`desc / properties / relations / hypernymPredicate / regular / spreadable / autoRelate`。
- `knext schema commit` 读取 `<项目目录>/schema/<Namespace>.schema` 与服务端 diff 同步（幂等，无 diff 时提示）。

## 3. 结构化构建链与输入数据格式

### 3.1 链结构

```
Scanner(文件→List[Dict])  >>  Mapping(Dict→SubGraph)  >>  [Vectorizer]  >>  KGWriter(写图)
```

- 框架入口：`BuilderChainRunner(scanner=..., chain=...).invoke(file_path)`（supplychain 的 `builder/indexer.py`），
  或在 `kag_config.yaml` 里声明 runner（medicine 的 `spg_runner`/`spo_runner`）后 `BuilderChainRunner.from_config(cfg)`。
- 无 `knext builder` 命令（源码中被注释），**建图就是跑自己写的 python 脚本**。
- Vectorizer 可整段省略（`DefaultStructuredBuilderChain(mapping, writer)` 支持 vectorizer=None），
  纯结构建图不走向量化。

### 3.2 Scanner

- `CSVScanner`（注册名 `csv_scanner`）：`pandas.read_csv(dtype=str)`，首行表头，默认逗号分隔，
  输出 `List[Dict[str,str]]`；支持 `delimiter`、`col_names`、`col_ids` 与分片参数。
  **坑**：空单元格会被 pandas 解析成 float NaN，而 `SPGTypeMapping` 只过滤 falsy 与 `pandas.NaT`，
  `bool(NaN)=True` 会把 NaN 当值写入属性。对策：自定义子类 scanner 传 `na_filter=False`/`keep_default_na=False`，
  或保证单元格无空缺。试点计划采用自定义 `SafeCSVScanner`。
- `JSONScanner`（注册名 `json_scanner`）：输入必须是 **JSON 数组**（list of dict）或单个 JSON 字符串，
  不支持 JSONL（每行一个对象）。数据量大时还得整体读入内存，故本工程选 CSV 路线。

### 3.3 SPGTypeMapping（实体/概念节点导入，注册名 `spg_mapping`）

源码 `kag/builder/component/mapping/spg_type_mapping.py`，行为逐条核实：

- 输入一行 dict；不配置 `property_mapping` 时原样使用全部列；配置后按 `add_property_mapping(源列名, 目标属性名)` 挑选/改名。
- `id` 列 → 节点 id；`name` 列 → 节点 name（缺省取 id）。
- **语义属性自动转边**：属性值的 SPG 类型不是基础类型（Text/Integer/Float）时，把单元格按英文逗号 `,` 切分，
  每个值作为目标节点 id 生成一条边（`s -[prop]-> o`）。即“一列写多个 id，逗号分隔”。
  可用 `link_func` 做名称→id 的实体链接（supplychain 的 `company_link_func`，调 SearchClient 检索）。
- 基础类型属性原样存为节点属性（整份 properties dict 都会挂在节点上）。
- **ConceptType 的 isA 链自动构建**：导入概念类数据时，把概念 id 按 `-` 切分层级路径，
  逐级建节点并连成 `(子)-[isA]->(父)`。例：id `原材料-原材料-化学制品-商品化工` 建 4 级节点。
  推论：概念 id 即其层级路径，**概念值中若含 `-` 会被误切层**，设计时概念取值要避免 `-`。

### 3.4 RelationMapping（带边属性的关系导入，注册名 `relation_mapping`）

- 专用于“主语-关系-宾语 + 边子属性”的 CSV：列固定为 `srcId,dstId,<子属性列...>`。
- 链的 `spg_type_name` 形如 `"Company_fundTrans_Company"`，按 `_` 切成 (subject, predicate, object)；
  predicate 必须是 subject 类型 properties/relations 中声明过的名字。
- `add_src_id_mapping/add_dst_id_mapping` 可改列名；`add_sub_property_mapping(源列, 目标子属性)` 映射边属性。
- 只生成边，两端节点需已由各自实体 CSV 导入（或依赖服务端按 id 补空节点——未验证，按“先点后边”组织导入顺序）。

### 3.5 SPOMapping（通用三元组导入，注册名 `spo_mapping`）

- medicine 示例 `SPO.csv`：`S,P,O,properties`，如 `Panic_disorder,has_symptom,Anxiety_and_nervousness,"{""confidence"": 1.0}"`。
- `properties` 列是 **JSON dict 字符串**（整体作为边属性）。可配置列名 `s_id_col/p_type_col/o_id_col/sub_property_col` 等。
- 适合一次性灌多种关系；但边属性有明确 schema 时 RelationMapping 更直观。

### 3.6 KGWriter（注册名 `kg_writer`）

- 把 SubGraph 经 `GraphClient` upsert 到服务端（`delete=True` 可删）。
- 同一 id 重复导入 = 覆盖式更新，适配器仍需自行去重以控制体积。

### 3.7 输入格式结论（供适配器实现）

| 数据 | 文件 | 关键列 |
|---|---|---|
| 实体节点 | `<Type>.csv` | `id,name,<标量属性...>,<语义属性列=逗号分隔的目标id>` |
| 概念节点 | `<ConceptType>.csv` | 仅 `id`（id 即 `-` 分隔的层级路径，isA 链自动生成） |
| 带属性关系 | `<S>_<p>_<O>.csv` | `srcId,dstId,<边子属性...>` |
| 通用三元组 | 任意 | `S,P,O,properties(JSON)` |

CSV 用标准引号转义（长文本含换行/逗号没问题，pandas 可正确解析）。

## 4. knext 命令与配置流程

```
pip install -e .                        # 安装 kag/knext（python3.10）
knext project create --config_path <yaml> [--tmpl default|medical]
                                        # 在服务端建项目 + 渲染本地模板目录；namespace 须 ^[A-Z][A-Za-z0-9]{0,15}$
knext project restore --host_addr http://127.0.0.1:8887 --proj_path .   # 已有本地目录时恢复/登记
knext project list / update
knext schema commit                     # 同步 schema/<Namespace>.schema 到服务端
knext schema reg_concept_rule --file concept.rule   # （可选）注册概念规则
cd builder && python indexer.py         # 跑自建建图脚本
knext reasoner execute ...              # 推理/查询（后续阶段）
```

**关键约束（读源码确认）**：`knext project create` 和 `knext project update` 都会
**实际调用** `chat_llm`（发一条 "who are you?"）与 `vectorizer`（对 "hello" 向量化和维度校验），
失败即退出（`knext/command/sub_command/project.py` + `kag/common/llm/llm_config_checker.py` +
`vectorize_model_config_checker.py`）。配置里的 `enable_check: false` 只影响 LLMClient 自身运行期检查，
**不能绕过 create/update 时的连通性校验**。因此即使是纯结构建图试点，也必须提供可连通的
OpenAI 兼容 chat 接口与 embedding 接口（可用本地 ollama 等替代）。LLMConfigChecker 只看 `chat_llm`，
`openie_llm` 不在校验范围。

## 5. 不确定点（试点时需验证）

1. RelationMapping 导入边时，若端点节点尚未写入，服务端是自动补空节点还是报错——脚本按“先实体后关系”排序规避，不做依赖。
2. `TextAndVector` 索引标注在 schema commit 后，是否需要相应索引构建任务/向量模型配置才生效；试点不配向量，长文本先只存属性，索引标注保留但预期检索增强暂不生效。
3. KGWriter upsert 对“同名不同来源”的共享节点（材料/法条）属性合并语义未验证——共享节点只放稳定属性（名称/文号），易变信息全放边属性上。
4. 概念树多级路径（`父-子`）导入顺序：`hypernym_predicate` 单条记录内即可建全链，未见跨记录合并问题，但多级概念与实体语义边混用时建议在试点抽查 isA 结构。
5. 官方 yuque 文档未逐节抓取（需代理且页面为 JS 渲染），以上结论均以仓库 master 源码与示例为准；若服务端版本不同（如 release 分支差异），以实际服务端行为为准。
