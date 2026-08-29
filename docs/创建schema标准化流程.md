# 创建 Schema 标准化流程

> 本文总结 Wikontic、SocraticKG、SynthKG 等论文对知识图谱建模的共同启示，形成一套适用于任意主题文档的 Schema 构建方法论。
>
> 本流程适用于政务、法律、医疗、金融、制造、科研等主题。它不要求一开始就建立完整本体，而是从业务问题和真实文档出发，逐步形成“可查询、可约束、可溯源、可扩展”的知识模型。

---

## 一、核心思想

一个可落地的 Schema 不等于实体和关系清单，而是一套完整的知识生产约定：

```text
业务问题
    ↓
数据画像
    ↓
实体 / 属性 / 关系划分
    ↓
稳定骨架 Schema
    ↓
文档分块与证据保留
    ↓
命题和结构化事实抽取
    ↓
约束校验与实体归一
    ↓
小规模验证
    ↓
版本化迭代
```

推荐采用三层结构：

```text
稳定骨架层：高频、稳定、需要精确查询的实体和关系
        ↓
证据层：原文、文档、Chunk、来源位置
        ↓
开放知识层：命题、长尾事实、抽取结果和审核信息
```

### 最重要的边界

```text
确定性结构化字段 → 规则 Mapping
复杂长文本 → Chunk + 命题抽取
高频稳定事实 → 固定 Schema 关系
长尾和不确定事实 → 开放命题
所有抽取结果 → 必须保留来源证据
```

不要直接把所有文档交给 LLM，然后让 LLM 自由生成一套不可控的 Schema。

---

## 二、三篇论文分别提供什么方法

### 2.1 Wikontic：先建立约束和归一体系

Wikontic 的主要启示是：Schema 应该有明确的类型边界和质量控制，而不是只依赖模型生成结果。

需要吸收的内容：

```text
实体类型约束
关系域和值域约束
实体 ID 归一
别名管理
层级关系
错误类型检测
本体错位率监控
```

它主要解决：

```text
抽出来的实体到底属于哪一类？
这条关系的两端类型是否正确？
两个名称不同的实体是否是同一个对象？
同一个对象的不同版本是否应该合并？
```

### 2.2 SocraticKG：复杂事实先展开，再抽取

SocraticKG 的主要启示是：复杂文本中的事实往往不是直接出现的，需要先通过问题驱动的方式把隐含信息展开。

流程：

```text
文档段落
    ↓
5W1H 问题生成
    ↓
上下文独立的问答事实
    ↓
三元组或条件事实
```

它主要适合：

```text
资格条件
限制条件
例外规则
因果关系
流程规则
适用对象
时间和数量限制
```

### 2.3 SynthKG：固定骨架之外保留开放命题

SynthKG 的主要启示是：不要为了覆盖长文本中的所有事实，不断增加固定关系和实体类型。

推荐：

```text
稳定事实 → 固定关系
长尾事实 → Proposition
原文依据 → Chunk
```

它主要解决：

```text
Schema 无限膨胀
新领域事实无法提前穷举
抽取结果无法回溯
长文本细节被固定字段压缩或丢失
```

---

## 三、标准化 Schema 构建流程

## 第 1 步：定义业务目标和问题清单

不要从“有哪些实体”开始，而应先列出系统必须回答的问题。

问题应分为三类：

### 1. 精确查询问题

```text
某对象由谁负责？
某对象需要哪些材料？
某对象属于哪个类别？
某对象的状态是什么？
某对象有哪些标准属性？
```

这类问题通常需要固定实体和固定关系。

### 2. 多跳推理问题

```text
对象 A 关联的机构还负责哪些对象？
对象 A 所属的法规有哪些其他引用对象？
对象 A 的前置步骤是什么？
对象 A 依赖的对象具有什么共同属性？
```

这类问题决定哪些实体适合作为图中的桥接节点。

### 3. 开放语义问题

```text
什么情况下可以执行某操作？
有哪些例外情况？
文档中还隐含了哪些限制？
某个规则对哪些对象生效？
```

这类问题通常需要 Chunk、Proposition 或 ServiceCondition。

### 本步骤产物

```text
requirements.md
question_catalog.csv
```

`question_catalog.csv` 建议包含：

```csv
question_id,question,answer_type,required_entities,required_relations,evidence_required
```

---

## 第 2 步：进行数据画像

对所有输入文档和结构化数据进行统计，不要先凭经验设计 Schema。

至少检查：

```text
文档数量
文档类型
字段名称
字段覆盖率
空值比例
字段长度分布
枚举值分布
重复记录
主键候选
实体名称分布
关系端点完整性
时间字段格式
数值字段格式
```

### 对字段进行分类

| 字段形态 | 典型内容 | 初始处理方式 |
|---|---|---|
| 稳定标识 | 编码、编号、唯一键 | 实体 ID |
| 短文本 | 名称、状态、类型 | 属性或概念 |
| 数值 | 金额、数量、序号 | Integer / Float |
| 时间 | 发布日期、生效日期 | Date 或规范化 Text |
| 长文本 | 条件、说明、法规内容 | Chunk / TextAndVector |
| 结构化列表 | 材料、步骤、参与方 | 实体和关系 |
| 天然问答 | 问题、答案 | FAQ 实体 + Chunk |
| 来源信息 | URL、文件、行号 | 证据属性 |

### 本步骤产物

```text
data_profile.json
data_profile.md
field_inventory.csv
```

---

## 第 3 步：划分实体、属性、关系和命题

### 3.1 什么时候建实体

满足以下任意条件时，优先考虑建成实体：

```text
具有独立身份
需要被多个对象复用
需要单独检索
需要作为多跳路径节点
与其他对象存在关系
关系本身需要携带属性
```

例如：

```text
部门
法规
材料
产品
机构
人员
地点
设备
```

### 3.2 什么时候建属性

以下内容通常适合作为属性：

```text
只描述当前实体
不需要独立关系
不被其他对象复用
不会单独参与多跳查询
```

例如：

```text
名称
状态
发布日期
说明
编码
```

### 3.3 什么时候建关系

两个独立实体之间存在稳定语义联系时，建关系：

```text
属于
负责
依赖
使用
产生
引用
位于
前置于
```

关系需要明确：

```text
关系名称
主体类型
客体类型
是否允许多值
是否允许重复
关系属性
关系属性类型
```

### 3.4 什么时候使用 Proposition

以下事实不要急于扩展固定 Schema：

```text
只在少量文档出现
谓词形式不稳定
语义仍需要人工确认
可能随着领域变化而变化
无法提前穷举
```

可以先保存为：

```text
Proposition(
    subjectId,
    subjectType,
    predicate,
    objectId,
    objectType,
    objectValue,
    statement,
    sourceChunkId,
    confidence,
    reviewStatus
)
```

### 一个实用判断

```text
如果这个关系未来需要写进查询白名单，先确认它是否真的稳定高频。
如果暂时不能确认，先放入 Proposition。
```

---

## 第 4 步：设计稳定骨架层

稳定骨架层只保留领域中最重要、最稳定的实体和关系。

推荐结构：

```text
核心对象
    ├── 关联组织
    ├── 关联资源
    ├── 关联流程
    ├── 关联法规
    └── 关联结果
```

每个核心实体至少需要：

```text
业务 ID
可读名称
稳定属性
来源字段
```

每个核心关系至少需要：

```text
主体类型
关系名
客体类型
端点 ID
关系属性
来源信息
```

### 骨架层设计原则

1. 实体类型数量保持克制；
2. 关系命名使用有区分度的动词；
3. 节点保存稳定属性；
4. 事项、场景或上下文相关属性放在关系上；
5. 不把长文本全部拆成固定关系；
6. 不因单个文档的特殊表达直接新增类型；
7. 所有关系必须可以被约束和验证。

---

## 第 5 步：设计证据层

知识图谱中的每个事实都应能够回到原文。

### 5.1 Chunk 设计

Chunk 是文档检索和事实溯源的基本单元，建议包含：

```text
chunkId
name
content
docId
sourceId
sourceType
sourceField
chunkIndex
chunkCount
sourceUrl
sourceFile
sourceLine
```

至少保留：

```text
来源对象 ID
来源对象类型
来源字段
来源文件
来源行号
```

### 5.2 分块原则

分块不能只按固定字符数切割，还要尽量保持语义边界：

```text
标题完整
段落完整
列表完整
条件句完整
步骤完整
条款完整
```

推荐保留：

```text
max_chars
overlap_chars
chunking_version
```

### 5.3 来源优先级

```text
原始文档
    >
规范化文档
    >
LLM 改写文本
    >
LLM 推断结果
```

下游回答必须优先引用原文或可定位的 Chunk，而不能只引用 LLM 生成的陈述。

---

## 第 6 步：设计开放知识层

### 6.1 Proposition

推荐字段：

```text
propositionId
subjectId
subjectType
predicate
objectId
objectType
objectValue
statement
sourceChunkId
confidence
extractionMethod
extractionModel
extractionVersion
reviewStatus
```

字段规则：

- `subjectId` 必须指向已知主体，或者进入待审核队列；
- `predicate` 保持开放，不直接成为固定关系；
- `objectId` 用于已有实体；
- `objectValue` 用于金额、时间、条件等字面值；
- `statement` 必须是上下文独立的完整陈述；
- `sourceChunkId` 必须能够回到原文；
- `confidence` 不能替代人工审核；
- `reviewStatus` 必须明确记录。

### 6.2 ServiceCondition

复杂条件可以单独设计为实体：

```text
ServiceCondition
    ├── conditionType
    ├── statement
    ├── sourceChunkId
    ├── confidence
    └── reviewStatus
```

建议第一版只使用粗粒度条件类型：

```text
适用对象
资格要求
数量限制
时间限制
禁止情形
例外情形
材料要求
```

不要在没有足够样本时直接固化复杂逻辑表达式。

### 6.3 固定关系和 Proposition 的去重

如果事实已经有固定关系表达，就不要重复生成同义 Proposition：

```text
GovernmentService --handledBy--> Department
```

不再重复生成：

```text
Proposition(predicate="负责办理")
```

只有当 Proposition 包含固定关系无法表达的限定条件时，才保留它。

---

## 第 7 步：设计实体归一和别名体系

实体归一应在导入图谱之前完成。

标准流程：

```text
原始名称
    ↓
规则清洗
    ↓
候选别名生成
    ↓
相似度匹配
    ↓
人工确认或高置信确认
    ↓
canonicalId
    ↓
导入图谱
```

建议建立：

```text
entity_aliases.csv
```

字段：

```text
entityType
rawName
canonicalId
canonicalName
normalizationRule
confidence
reviewStatus
source
```

### 不允许直接自动合并

```text
仅凭字符串相似度
仅凭向量相似度
仅凭名称相同
仅凭一个文档中的上下文
```

尤其需要谨慎处理：

```text
法规不同版本
机构上下级关系
材料的规格差异
同名但不同地区的对象
同名但不同时间版本的对象
```

---

## 第 8 步：设计关系域和值域约束

每条固定关系必须定义域和值域：

```text
关系：handledBy
主体：GovernmentService
客体：Department
规则：客体必须存在于 Department 实体集合
```

建议使用约束表：

```csv
relation,source_type,target_type,cardinality,rule,severity
handledBy,GovernmentService,Department,one-to-many,target_exists,error
requiresMaterial,GovernmentService,Material,one-to-many,target_exists,error
hasCondition,GovernmentService,ServiceCondition,one-to-many,target_exists,error
nextStep,ProcessStep,ProcessStep,one-to-many,no_self_loop,error
extractedFrom,Proposition,Chunk,many-to-one,source_chunk_exists,error
```

至少校验：

```text
实体 ID 非空
实体 ID 唯一
关系端点存在
关系类型正确
关系属性类型正确
禁止自环
必填属性完整
命题来源存在
Chunk 来源字段合法
```

### 违例处理

不能静默删除错误数据，应区分：

```text
error       → 不进入正式图谱，进入 rejects
warning     → 可进入隔离区，等待审核
needs_review → 进入人工审核队列
```

建议输出指标：

```text
valid_rows
invalid_rows
missing_endpoint_count
ontology_mismatch_rate
duplicate_fact_count
conflict_count
manual_review_count
```

---

## 第 9 步：按字段形态选择建图路线

### 9.1 结构化字段

```text
实体 ID
名称
状态
分类
部门
材料清单
流程列表
文号
日期
```

使用：

```text
规则 Mapping
```

不需要 LLM。

### 9.2 半结构化长文本

```text
条件
说明
流程描述
审查标准
法规条文
产品限制
```

使用：

```text
Chunk
去语境化
5W1H 展开
命题抽取
约束校验
```

### 9.3 天然 QA

```text
question + answer
```

使用：

```text
结构化 FAQ 或 QA 实体
同时生成检索 Chunk
```

不需要使用 LLM 重新生成问题，除非目标是扩展问题表达或生成评测集。

---

## 第 10 步：建立抽取流水线

通用抽取流水线：

```text
原始文档
    ↓
文档清洗
    ↓
语义分块
    ↓
去语境化
    ↓
5W1H 问题展开
    ↓
生成上下文独立命题
    ↓
实体识别和归一
    ↓
命题 / 条件 / 关系抽取
    ↓
域值域校验
    ↓
重复和冲突检测
    ↓
人工审核
    ↓
输出 CSV / JSONL
    ↓
导入图谱
```

### 抽取结果必须保存版本

```text
extractionMethod
extractionModel
extractionVersion
promptVersion
schemaVersion
```

### 抽取结果必须支持回滚

如果某个模型版本产生错误，应该可以按以下字段删除或重建：

```text
extractionVersion
sourceChunkId
reviewStatus
```

不能把不同模型版本的结果混在一起而不做区分。

---

## 第 11 步：使用小样本验证 Schema

不要直接对全量数据执行第一次建图或 LLM 抽取。

建议至少准备：

```text
不同文档类型
不同类别
不同长度
不同结构
不同难度
```

样本应覆盖：

```text
普通事实
多跳事实
复杂条件
例外规则
重复实体
冲突文本
缺失端点
多值属性
```

### 验证内容

#### Schema 验证

```text
实体类型存在
关系域值域正确
属性类型正确
索引配置正确
```

#### 数据验证

```text
实体 ID 唯一
关系端点完整
重复事实可解释
冲突事实可追踪
来源字段完整
```

#### 检索验证

```text
实体名称可检索
Chunk 可全文检索
Chunk 可向量检索
Proposition 可按来源回溯
```

#### 问答验证

```text
答案是否能从图中推导
答案是否引用正确证据
多跳路径是否完整
检索为空时是否拒答
```

---

## 第 12 步：定义 Schema 升级规则

### 12.1 什么时候新增实体类型

只有当一个对象长期满足以下条件时，才新增实体：

```text
具有稳定身份
在多个文档中重复出现
需要独立查询
需要参与多跳关系
已有命题层无法满足查询需求
```

### 12.2 什么时候新增固定关系

只有当谓词满足以下条件时，才提升为固定关系：

```text
高频出现
语义稳定
主体和客体类型稳定
查询需求明确
域值域可以约束
人工审核结果稳定
```

### 12.3 版本化策略

推荐：

```text
Schema v1.0：稳定骨架
Schema v1.1：增加约束或属性
Schema v1.2：增加经过验证的关系
Schema v2.0：结构性模型变化
```

每次变更同步更新：

```text
Schema 文件
字段说明
约束表
实体归一表
抽取脚本
导入 Mapping
查询白名单
测试集
验收报告
```

---

## 四、通用 Schema 模板

以下模板不是某个具体领域的最终 Schema，而是创建新领域 Schema 时的起点：

```text
namespace <Namespace>

# 1. 核心实体
CoreObject(核心对象): EntityType
    properties:
        name(名称): Text
            index: Text
        objectId(业务ID): Text
            index: Text
        status(状态): Text
            index: Text
        sourceUrl(来源链接): Text

# 2. 共享实体
Organization(组织): EntityType
    properties:
        name(名称): Text
            index: Text
        organizationId(组织ID): Text
            index: Text

Resource(资源): EntityType
    properties:
        name(名称): Text
            index: Text
        resourceId(资源ID): Text
            index: Text

# 3. 证据层
Chunk(文本块): EntityType
    properties:
        name(名称): Text
        content(内容): Text
            index: TextAndVector
        chunkId(文本块ID): Text
            index: Text
        sourceId(来源ID): Text
            index: Text
        sourceField(来源字段): Text
        sourceFile(来源文件): Text
        sourceLine(来源行号): Text

# 4. 开放知识层
Proposition(知识命题): EntityType
    properties:
        name(名称): Text
        propositionId(命题ID): Text
            index: Text
        subjectId(主体ID): Text
        subjectType(主体类型): Text
        predicate(开放谓词): Text
            index: Text
        objectId(客体ID): Text
        objectType(客体类型): Text
        objectValue(客体值): Text
        statement(完整陈述): Text
            index: TextAndVector
        sourceChunkId(来源文本块ID): Text
        confidence(置信度): Double
        extractionVersion(抽取版本): Text
        reviewStatus(审核状态): Text

# 5. 固定关系
CoreObject(核心对象): EntityType
    relations:
        handledBy(负责机构): Organization
        usesResource(使用资源): Resource
        hasChunk(原文块): Chunk
        statesProposition(事实命题): Proposition

Proposition(知识命题): EntityType
    relations:
        extractedFrom(抽取自): Chunk
```

> 实际发布前应根据目标图数据库和 OpenSPG MarkLang 版本确认具体语法、属性类型和多值约束。

---

## 五、完整数据样本：从一条文档到知识图谱

本节用一个完整的政务事项样本说明数据如何流动。这个例子来自当前项目的法人服务数据，展示的流程同样适用于法律、医疗、药品、金融等其他主题。

### 7.1 原始业务对象

原始 JSON 通常包含事项、办理、申请、法律依据和来源等部分。为了便于理解，下面只保留与建模有关的字段：

```json
{
  "事项": {
    "编码": "2a8c7a95e01f57b8d43a558642e0cae5",
    "名称": "高等职业学校设立（含设置分校区）",
    "事项类型": "行政许可",
    "服务对象": "企业法人,事业法人,社会组织法人",
    "行使层级": "省级",
    "实施主体": "广东省教育厅"
  },
  "办理": {
    "承诺办结时限": "4工作日",
    "法定办结时限": "20工作日",
    "可网上办理": "是",
    "是否收费": "否",
    "窗口办理流程": "收件：核对申请材料；受理：确认材料齐全；审查：审核申请条件；决定：作出许可决定。"
  },
  "申请": {
    "受理条件": "符合《中华人民共和国高等教育法》等规定和高等职业学校设置标准的组织可以提出申请。",
    "材料": [
      {
        "名称": "申请正式建校的请示报告",
        "是否必要": "必要",
        "提交形式": "纸质/电子化"
      }
    ]
  },
  "法律依据": [
    {
      "名称": "中华人民共和国高等教育法",
      "文号": "中华人民共和国主席令第23号",
      "条款": "第二十四条",
      "内容": "设立高等学校，应当符合国家高等教育发展规划，符合国家利益和社会公共利益。"
    }
  ],
  "来源": {
    "详情页URL": "https://example.gov.cn/service/2a8c..."
  }
}
```

这条原始记录首先经过清洗和归一化，生成稳定的事项 ID：

```text
service_id = 2a8c7a95e01f57b8d43a558642e0cae5
```

### 7.2 数据画像：先判断每个字段是什么

不要看到字段就直接创建实体。先判断字段的身份、复用性和查询价值：

| 原始字段 | 数据形态 | 建模判断 | 目标位置 |
|---|---|---|---|
| `事项.编码` | 稳定唯一标识 | 事项主键 | `GovernmentService.id` |
| `事项.名称` | 短文本 | 核心对象名称 | `GovernmentService.name` |
| `事项.实施主体` | 可跨事项复用的对象 | 独立实体 | `Department` |
| `申请.材料` | 列表对象 | 独立实体 + 上下文关系 | `Material` + `requiresMaterial` |
| `办理.窗口办理流程` | 半结构化长文本 | 原文证据 + 命题候选 | `Chunk` → `Proposition` |
| `申请.受理条件` | 规则性长文本 | 原文证据 + 条件实体 | `Chunk` → `ServiceCondition` |
| `法律依据` | 法规和条款对象 | 法规文件 + 条款引用 | `LegalBasis` + `LegalCitation` |
| `办理.是否收费` | 当前事项的状态属性 | 普通属性 | `GovernmentService` |
| `来源.详情页URL` | 溯源信息 | 证据属性 | `sourceUrl` |

这一步的核心不是“字段越多越好”，而是确定：

```text
什么是可复用对象？
什么是当前事项的属性？
什么需要成为可走的图边？
什么必须保留原文？
```

### 7.3 规则 Mapping：结构化数据进入骨架层

#### 事项节点

```csv
service_id,service_name,category_l1,category_l2,department_name,service_object,exercise_level,service_status,online_available,promise_time_limit,legal_time_limit
2a8c7a95e01f57b8d43a558642e0cae5,高等职业学校设立（含设置分校区）,法人服务,设立变更,广东省教育厅,"企业法人,事业法人,社会组织法人",省级,,是,4工作日,20工作日
```

Mapping 后得到：

```text
GovernmentService(
    id = "2a8c7a95e01f57b8d43a558642e0cae5",
    name = "高等职业学校设立（含设置分校区）",
    serviceObject = "企业法人,事业法人,社会组织法人",
    onlineAvailable = "是",
    promiseTimeLimit = "4工作日",
    legalTimeLimit = "20工作日"
)
```

#### 部门节点和负责关系

```csv
# departments.csv
department_id,department_name,department_code
department:b6ca52843ef1aaca2c9117ae,广东省教育厅,006940116
```

```csv
# service_handled_by.csv
service_id,department_id,department_role
2a8c7a95e01f57b8d43a558642e0cae5,department:b6ca52843ef1aaca2c9117ae,主管部门
```

图中的结果是：

```text
GovernmentService(2a8c7a...)
    ── handledBy {departmentRole: "主管部门"} ──>
Department(department:b6ca...)
```

#### 材料节点和带上下文的关系

```csv
# materials.csv
material_id,material_name,material_type,source_type,submission_format
material:request-report,申请正式建校的请示报告,,,纸质/电子化
```

```csv
# service_requires_material.csv
service_id,material_id,required,order_no,material_description,acceptance_standard
2a8c7a95e01f57b8d43a558642e0cae5,material:request-report,必要,1,,
```

这里的设计重点是：

```text
Material 节点：材料是什么
requiresMaterial 关系：这个事项如何要求该材料
```

因此关系属性不会污染共享的 Material 节点。

图中的结果是：

```text
GovernmentService(2a8c7a...)
    ── requiresMaterial {
           required: "必要",
           orderNo: 1
        } ──>
Material(material:request-report)
```

### 7.4 法律依据：为什么要拆成法规文件和条款引用

原始法律依据同时包含法规名称、文号和具体条款：

```csv
legal_basis_id,law_name,article,document_number,clause_content
96c1ff39-24d9-436c-bf74-d063c5ca5aa7,中华人民共和国高等教育法,第二十四条,中华人民共和国主席令第23号,设立高等学校，应当符合国家高等教育发展规划，符合国家利益和社会公共利益。
```

如果直接建成一个 `LegalBasis` 节点，多个事项引用同一法规的不同条款时容易发生覆盖。因此按两层转换：

```csv
# LegalBasis.csv
legal_basis_id,name,document_number
law:chairman-order-23,中华人民共和国高等教育法,中华人民共和国主席令第23号
```

```csv
# LegalCitation.csv
citation_id,name,article,content,source_chunk_id
LC-chairman-order-23-24,中华人民共和国高等教育法 第二十四条,第二十四条,设立高等学校，应当符合国家高等教育发展规划，符合国家利益和社会公共利益。,
```

```csv
# GovernmentService_citesLegal_LegalCitation.csv
start_id,end_id,order_no,basis_source
2a8c7a95e01f57b8d43a558642e0cae5,LC-chairman-order-23-24,1,结构化法律依据
```

```csv
# LegalCitation_partOf_LegalBasis.csv
start_id,end_id
LC-chairman-order-23-24,law:chairman-order-23
```

最终路径：

```text
GovernmentService
    ── citesLegal ──>
LegalCitation(第二十四条)
    ── partOf ──>
LegalBasis(中华人民共和国高等教育法)
```

这里要注意：`legal_basis_id` 只能作为原始数据追踪信息，法规节点的稳定 ID 和条款引用 ID 必须按统一规则生成，并且同一条款出现不同文本时必须记录冲突，不能静默覆盖。

### 7.5 长文本分块：原文先进入 Chunk

受理条件原文：

```text
符合《中华人民共和国高等教育法》等规定和高等职业学校设置标准的组织可以提出申请。
```

经过分块后，得到一个 Chunk：

```csv
chunk_id,doc_id,title,content,source_id,source_type,source_field,chunk_index,service_id,source_url
service:2a8c7a...#acceptCondition#0001,service:2a8c7a...,高等职业学校设立（含设置分校区）-受理条件,符合《中华人民共和国高等教育法》等规定和高等职业学校设置标准的组织可以提出申请。,2a8c7a95e01f57b8d43a558642e0cae5,GovernmentService,acceptCondition,1,2a8c7a95e01f57b8d43a558642e0cae5,https://example.gov.cn/service/2a8c...
```

它进入：

```text
Chunk.content -> TextAndVector
```

Chunk 的作用是：

```text
可检索
可定位
可回到原始事项
可作为后续抽取依据
```

Chunk 不是抽取结果的替代品。即使后续命题抽取失败，原文 Chunk 仍然保留。

### 7.6 SocraticKG：5W1H 展开受理条件

针对 Chunk，不直接让模型自由生成三元组，而是先生成上下文独立的问题和答案：

| 问题 | 答案 |
|---|---|
| 谁可以提出申请？ | 符合相关规定和设置标准的组织 |
| 需要满足什么条件？ | 符合《中华人民共和国高等教育法》等规定和高等职业学校设置标准 |
| 可以进行什么操作？ | 提出申请 |
| 申请针对什么事项？ | 高等职业学校设立（含设置分校区） |
| 依据来自哪里？ | 当前受理条件文本 Chunk |

再将结果归纳为 `ServiceCondition`：

```csv
condition_id,name,description,condition_type,statement,source_chunk_id,confidence,review_status
condition:2a8c7a...:001,申请资格,符合《中华人民共和国高等教育法》等规定和高等职业学校设置标准的组织可以提出申请。,资格要求,符合相关规定和设置标准的组织可以提出高等职业学校设立申请。,service:2a8c7a...#acceptCondition#0001,0.96,pending
```

图中的结果是：

```text
GovernmentService(2a8c7a...)
    ── hasCondition ──>
ServiceCondition(
    conditionType = "资格要求",
    statement = "符合相关规定和设置标准的组织可以提出高等职业学校设立申请。"
)
    ── hasChunk ──>
Chunk(受理条件原文)
```

这里同时保留：

```text
原始文本：description
上下文独立陈述：statement
证据位置：sourceChunkId
抽取可信度：confidence
审核状态：reviewStatus
```

### 7.7 SynthKG：从窗口流程中抽取开放命题

窗口办理流程原文：

```text
收件：核对申请材料；受理：确认材料齐全；审查：审核申请条件；决定：作出许可决定。
```

先分成流程 Chunk，再做去语境化和命题抽取：

```json
[
  {
    "proposition_id": "prop:2a8c7a...:001",
    "subject_id": "2a8c7a95e01f57b8d43a558642e0cae5",
    "subject_type": "GovernmentService",
    "predicate": "受理阶段检查",
    "object_value": "确认申请材料是否齐全",
    "statement": "在高等职业学校设立事项的受理阶段，办理人员需要确认申请材料是否齐全。",
    "source_chunk_id": "service:2a8c7a...#windowProcess#0001",
    "confidence": 0.91,
    "review_status": "pending"
  }
]
```

命题进入：

```text
GovernmentService
    ── statesProposition ──>
Proposition
    ── extractedFrom ──>
Chunk
```

为什么不直接新增固定关系：

```text
hasReceivingCheck
requiresCompleteMaterial
checksApplicationCondition
```

因为这些谓词目前还没有足够证据证明是跨数据集稳定的关系。先保存在 `Proposition.predicate` 中，等经过统计和审核后，再决定是否提升为固定关系。

### 7.8 Wikontic：导入前的约束校验

对上面的结果进行校验：

```text
检查 1：GovernmentService.id 是否存在？
检查 2：handledBy 的目标是否为 Department？
检查 3：requiresMaterial 的目标是否为 Material？
检查 4：requiresMaterial.orderNo 是否为整数？
检查 5：citesLegal 的目标是否为 LegalCitation？
检查 6：LegalCitation.partOf 的目标是否为 LegalBasis？
检查 7：ServiceCondition.sourceChunkId 是否存在？
检查 8：Proposition.sourceChunkId 是否存在？
检查 9：是否出现同一实体 ID 的属性冲突？
检查 10：是否出现同一条款 ID 的内容冲突？
```

一个关系约束表可以写成：

```csv
relation,source_type,target_type,rule,severity
handledBy,GovernmentService,Department,target_exists,error
requiresMaterial,GovernmentService,Material;orderNo_is_integer,error
hasCondition,GovernmentService,ServiceCondition,target_exists,error
citesLegal,GovernmentService,LegalCitation,target_exists,error
partOf,LegalCitation,LegalBasis,target_exists,error
hasChunk,GovernmentService,Chunk,chunk.sourceId_equals_subject_id,error
extractedFrom,Proposition,Chunk,source_chunk_exists,error
nextStep,ProcessStep,ProcessStep,no_self_loop,error
```

错误不能静默删除：

```text
严重错误 → rejects/，不进入正式图谱
需要人工确认 → review/，保留原始行和错误原因
警告 → 隔离区，允许进入试验图但不进入正式图
```

### 7.9 完整数据流总览

把这条事项串起来，完整数据流是：

```text
原始 JSON / CSV
    ↓
清洗、归一和主键确认
    ↓
数据画像
    ↓
结构化字段规则 Mapping
    ↓
GovernmentService / Department / Material / ProcessStep / LegalBasis / FAQ 等骨架实体
    ↓
固定关系和关系属性
    ↓
长文本语义分块
    ↓
Chunk 证据层
    ↓
SocraticKG 5W1H（复杂条件）
    ↓
SynthKG 命题抽取（长尾事实）
    ↓
实体归一和别名校验
    ↓
关系域值域校验
    ↓
人工审核和冲突处理
    ↓
输出 CSV / JSONL
    ↓
OpenSPG Schema Mapping
    ↓
图查询、向量检索和可溯源问答
```

最终，这一条事项会形成类似的局部图：

```text
GovernmentService
├── handledBy ───────────────> Department
├── requiresMaterial ────────> Material
├── hasCondition ─────────────> ServiceCondition ── hasChunk ──> Chunk
├── hasProcessStep ───────────> ProcessStep ── nextStep ──> ProcessStep
├── producesResult ───────────> ServiceResult
├── citesLegal ───────────────> LegalCitation ── partOf ──> LegalBasis
├── hasFaq ───────────────────> FAQ
├── hasChannel ───────────────> ServiceChannel
├── hasFee ───────────────────> Fee
├── hasChunk ─────────────────> Chunk
└── statesProposition ────────> Proposition ── extractedFrom ──> Chunk
```

---

## 六、如何把同一方法迁移到其他主题

方法不依赖“政务事项”这个领域，只需要替换核心对象、共享对象和业务关系。

### 8.1 法律文档示例

原文：

```text
用人单位不得无故解除劳动合同。
```

数据流：

```text
法律文档
    ↓
Chunk
    ↓
Proposition(
    subject = 用人单位,
    predicate = 不得解除,
    objectValue = 劳动合同,
    statement = 用人单位不得无故解除劳动合同
)
```

如果“禁止解除”在大量法律文档中都具有稳定语义，可以进一步设计固定关系：

```text
Employer --prohibitedFrom--> TerminationAction
```

但在确认之前，先使用 Proposition 更安全。

### 8.2 药品文档示例

原文：

```text
本品应密封、避光保存，儿童不宜使用。
```

数据流：

```text
Drug
    ├── hasStorageRequirement ──> Proposition
    └── hasUsageRestriction ────> Proposition
```

初期也可以使用开放命题：

```text
subject = 某药品
predicate = 储存要求
objectValue = 密封、避光保存
```

当药品领域中“储存要求”结构稳定、查询频繁后，再提升为固定实体：

```text
Drug --hasStorageRequirement--> StorageRequirement
```

### 8.3 医疗文档示例

原文：

```text
患者出现持续高热并伴有呼吸困难时，应及时就医。
```

通过 5W1H 展开：

```text
什么人？患者
什么情况？持续高热并伴有呼吸困难
应采取什么动作？及时就医
```

可以形成：

```text
MedicalCondition
    conditionType = 就医条件
    statement = 患者出现持续高热并伴有呼吸困难时应及时就医
```

### 8.4 通用替换表

| 政务项目中的对象 | 通用领域中的对应概念 |
|---|---|
| `GovernmentService` | 核心业务对象，例如法规、药品、疾病、产品 |
| `Department` | 组织、机构、责任主体 |
| `Material` | 资源、部件、文件、药品成分 |
| `ServiceCondition` | 条件、适应症、限制、前置要求 |
| `ProcessStep` | 流程、治疗阶段、生产步骤 |
| `LegalBasis` | 标准、法规、文献、规范 |
| `Chunk` | 原文证据片段 |
| `Proposition` | 开放事实和长尾知识 |

---

## 七、一个 Schema 是否设计合理的判断标准

### 9.1 可查询

```text
能否回答核心业务问题？
能否通过 1~3 跳路径得到答案？
能否区分相近关系？
```

### 9.2 可验证

```text
实体类型是否明确？
关系域和值域是否明确？
属性类型是否正确？
错误数据是否能被识别？
```

### 9.3 可溯源

```text
每条事实是否能回到 Chunk？
每个 Chunk 是否能回到原始文档？
是否保留来源文件和行号？
是否保留抽取版本？
```

### 9.4 可扩展

```text
新事实是否可以进入 Proposition？
是否避免频繁修改 Schema？
是否可以从开放谓词统计出稳定关系？
```

### 9.5 可维护

```text
能否重复导入？
能否按版本回滚？
能否区分规则事实和 LLM 事实？
能否进行人工审核？
```


---

## 八、建模决策检查表

### 业务层

- [ ] 是否明确系统需要回答的问题？
- [ ] 是否明确哪些问题需要多跳路径？
- [ ] 是否明确哪些答案必须有原文依据？

### 数据层

- [ ] 是否完成字段画像？
- [ ] 是否确认主键候选？
- [ ] 是否统计重复、空值和冲突？
- [ ] 是否区分结构化字段和长文本？

### Schema 层

- [ ] 每个实体是否有稳定 ID？
- [ ] 每条关系是否有明确域和值域？
- [ ] 关系属性是否放在正确位置？
- [ ] 是否避免为长尾谓词无限扩展 Schema？
- [ ] 是否保留 Chunk 和来源字段？

### 抽取层

- [ ] 是否先分块再抽取？
- [ ] 是否对复杂文本进行去语境化？
- [ ] 是否对隐含条件使用 5W1H 展开？
- [ ] 是否保存抽取模型和版本？
- [ ] 是否保留人工审核状态？

### 质量层

- [ ] 是否检查实体归一？
- [ ] 是否检查关系端点？
- [ ] 是否检查属性数据类型？
- [ ] 是否记录冲突而不是静默覆盖？
- [ ] 是否准备小规模试点？
- [ ] 是否定义本体错位率和命题覆盖率？

### 工程层

- [ ] Schema 是否可以单独发布？
- [ ] Mapping 是否可以重复执行？
- [ ] 数据导入是否支持断点和回滚？
- [ ] Schema、数据、Prompt 和抽取结果是否版本化？
- [ ] 是否可以从任意答案回溯到原文？

---

## 九、最终方法论

对于任意主题的文档，推荐采用以下标准流程：

```text
1. 先列业务问题，不先列实体
2. 再做数据画像，不凭空猜 Schema
3. 把稳定对象建成实体
4. 把稳定语义联系建成关系
5. 把当前实体属性建成属性
6. 把长文本保存为 Chunk
7. 把复杂隐含事实展开为上下文独立命题
8. 把高频稳定谓词提升为固定关系
9. 把长尾谓词保留在 Proposition
10. 给实体和关系增加域值域约束
11. 在导入前做实体归一和冲突检测
12. 用小样本验证，再进行全量建图
13. 对 Schema、数据和抽取流程进行版本化
14. 让所有事实都可以回到原始证据
```

最终形成的不是一个“看起来完整”的 Schema，而是：

```text
少量稳定骨架
    +
可回溯证据层
    +
可扩展开放知识层
    +
可执行约束
    +
可验证迭代流程
```

一句话总结：

> **先用业务问题和数据画像确定稳定骨架，再用 Chunk 保留证据、用 5W1H 展开隐含事实、用 Proposition 承接长尾知识，最后通过实体归一、域值域约束和小样本验收，把 Schema 从一次性设计变成可持续演进的知识建模系统。**
