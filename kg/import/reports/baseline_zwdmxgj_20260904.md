# ZwdmxGJ 新图 QA 基线报告（2026-09-04）

> 项目首个可复现基线。数据：pilot（法人）域 zwdmxgj 图；链路：bge-m3 检索 + DeepSeek deepseek-v4-flash 生成。

## 一、评测环境

- 图：Neo4j `zwdmxgj` 库（ZwdmxGJ v0.3，17 实体 / 23 关系名）；标签带命名空间前缀（`ZwdmxGJ.GovernmentService`），边为短名。
- 边计数对账（全部精确达标）：handledBy 481,501 / requiresMaterial 2,153,859 / hasCondition 481,500 / hasProcessStep 1,556,046 / nextStep 1,274,564 / producesResult 316,829 / hasFaq 126,849 / hasChannel 1,593,314 / hasFee 470,559 / belongsToDomain 481,601 / classifiedAs 481,501 / collaboratesWith 43,633 / partOf 9,394 / citesLegal 6,305,650 / usesModel 481,501。
- 向量：OpenSPG 自动索引 10 个（`_zwdmx_g_j_*` 前缀，1024 维 COSINE）；评测涉及实体已回填真实 bge-m3 向量（1,207 条），其余节点为导入期 mock 零向量（被检索阈值 0.45/0.72 天然过滤）。
- qa 链路：`KG_SCHEMA=zwdmxgj`（qa/ 双图开关，qa/multihop.py + qa/retriever.py）。

## 二、题集

`data/pilot/testset_multihop.csv`（56 题，模板法从图生成，seed=42）：

| answer_type | 题数 | 题型 |
|---|---|---|
| materials / department / legal_basis / condition / process | 各 8 | 1 跳（事项→材料/部门/法规/条件/流程） |
| multi_hop_dept / multi_hop_material | 各 8 | 2 跳（部门→其他事项、材料→共用事项） |

未生成题型：multi_hop_legal（锚点驱动重写前池查询超时）、faq（FAQ 池查询超时，待加采样前置）。

## 三、基线结果（56 题）

| 模式 | EM | F1 | 备注 |
|---|---|---|---|
| 单轮检索 | 0.0000 | **0.3195** | 四类索引并行检索 → 图扩展 → 生成 |
| KAG 式多跳 | 0.0000 | **0.3852** | locate/traverse 规划 → 逐跳绑定 → 生成 |

分题型 F1（单轮 → 多跳）：materials 0.598→0.631；condition 0.203→**0.476**；legal_basis 0.245→0.305；multi_hop_dept 0.290→0.387；multi_hop_material 0.385→0.407；department 0.270→0.230；process 0.246→0.262。

要点：
- **多跳模式总体 +6.6 个 F1 点**，对条件类（+27 点）和法条类（+6 点）提升最大——多跳规划能主动走到 ServiceCondition/LegalCitation 路径。
- EM=0 是判分器定义所致（归一化后全等）；模型答案普遍带解释与来源引用，F1 为有效指标。改进方向：生成端按题型输出纯清单，或判分端加宽松匹配档。
- 零拒答（56/56 全部作答）——检索为空时的硬拒答仍未实现（qa/ask.py:48 已知缺口）。

## 四、已知限制（对比数字时必须考虑）

1. GovernmentService 缺 part-00002（-136,420）、ServiceChannel 缺 part-00005（-193,314）——节点片补灌中，不影响本基线（抽样池来自已入图节点）。
2. Chunk 未导入（阶段二）——hasChunk 上下文缺失，condition/长文本问答靠 ServiceCondition 承载。
3. 全图约 100 万向量属性中仅评测实体（1,207 条）为真实嵌入——检索召回上限受回填范围约束；全量回填脚本已就绪（scripts/backfill_vectors.py，可过夜跑）。
4. 题集为模板法生成，问题表述单一（"X 需要提交哪些申请材料？"句式），真实用户问法的泛化能力未测。

## 五、复现

```bash
# 1. 题集与向量回填
python scripts/build_testset_multihop.py --out data/pilot/testset_multihop.csv \
  --ids-out kg/import/checkpoints/testset_service_ids.txt --per-type 8 --seed 42 \
  --types material,department,legal,condition,process,multi_hop_dept,multi_hop_material
python scripts/backfill_vectors.py --service-ids kg/import/checkpoints/testset_service_ids.txt --max-chars 1800
# 2. 预测（两模式）
KG_SCHEMA=zwdmxgj python scripts/run_baseline.py --testset data/pilot/testset_multihop.csv --out <pred_single.jsonl>
KG_SCHEMA=zwdmxgj python scripts/run_baseline.py --testset data/pilot/testset_multihop.csv --out <pred_multihop.jsonl> --multihop
# 3. 判分
python scripts/score_testset.py --testset data/pilot/testset_multihop.csv --pred <pred>.jsonl
```

预测产物：`data/pilot/testset_multihop_pred_single.jsonl` / `..._multihop.jsonl`（data/ 不入库，可由上述命令重建）。
