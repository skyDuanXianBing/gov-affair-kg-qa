# pilot 骨架层对账报告

- Neo4j 库: zwdmxgj（docker release-openspg-neo4j）
- 节点期望 = 源表去重主键数；边期望 = 源表 distinct (start,end) 对数（SPG (s,p,o) UPSERT 去重）

## 一、节点对账

| 实体 | Neo4j 实际 | 期望 | 差异 | 说明 |
|---|---:|---:|---:|---|
| ServiceDomain | 1 | 1 | +0 | routing_metadata/service_domains.csv |
| CategoryScheme | 1 | 1 | +0 | routing_metadata/category_schemes.csv |
| ServiceCategory | 50 | 50 | +0 | routing_metadata/service_categories.csv |
| KnowledgeModel | 50 | 50 | +0 | routing_metadata/knowledge_models.csv |
| Department | 25,855 | 25,855 | +0 | data\pilot\departments.csv distinct department_id |
| Material | 30,111 | 30,111 | +0 | build/shared_ids stats.material_shared_nodes |
| LegalBasis | 2,758 | 2,758 | +0 | build/shared_ids stats.legal_bases |
| LegalCitation | 9,394 | 9,394 | +0 | build/shared_ids stats.legal_citations |
| ServiceCondition | 481,500 | 481,500 | +0 | data\pilot\conditions.csv distinct condition_id |
| ProcessStep | 1,556,046 | 1,556,046 | +0 | data\pilot\process_steps.csv distinct process_step_id |
| ServiceResult | 316,827 | 316,827 | +0 | data\pilot\results.csv distinct result_id |
| FAQ | 126,849 | 126,849 | +0 | data\pilot\faqs.csv distinct faq_id |
| ServiceChannel | 1,593,314 | 1,593,314 | +0 | data\pilot\service_channels.csv distinct channel_id |
| Fee | 470,559 | 470,559 | +0 | data\pilot\fees.csv distinct fee_id |
| GovernmentService | 481,501 | 481,501 | +0 | data\pilot\services.csv distinct service_id |

## 二、边对账

| 关系 | Neo4j 实际 | 期望 distinct(s,o) | 源行数 | 去重率 | 说明 |
|---|---:|---:|---:|---:|---|
| partOf | 9,394 | 9,394 | 9,394 | 0.00% | build\shared_ids\pilot\part_of.csv |
| handledBy | 481,501 | 481,501 | 481,501 | 0.00% | data\pilot\service_handled_by.csv |
| collaboratesWith | 43,633 | 43,633 | 43,700 | 0.15% | data\pilot\service_collaborates_with.csv |
| requiresMaterial | 2,153,859 | 2,153,859 | 2,153,892 | 0.00% | build\shared_ids\pilot\service_requires_material_out.csv |
| hasCondition | 481,500 | 481,500 | 481,500 | 0.00% | data\pilot\service_has_condition.csv |
| hasProcessStep | 1,556,046 | 1,556,046 | 1,556,046 | 0.00% | data\pilot\service_has_process_step.csv |
| nextStep | 1,274,564 | 1,274,564 | 1,274,564 | 0.00% | data\pilot\process_step_next.csv |
| producesResult | 316,829 | 316,829 | 316,829 | 0.00% | data\pilot\service_produces_result.csv |
| citesLegal | 6,305,650 | 6,305,650 | 6,305,710 | 0.00% | build\shared_ids\pilot\service_based_on_out.csv |
| hasFaq | 126,849 | 126,849 | 126,849 | 0.00% | data\pilot\service_has_faq.csv |
| hasChannel | 1,593,314 | 1,593,314 | 1,593,314 | 0.00% | data\pilot\service_has_channel.csv |
| hasFee | 470,559 | 470,559 | 470,559 | 0.00% | data\pilot\service_has_fee.csv |
| belongsToDomain | 481,601 | 481,501 | 481,501 | 0.00% | kg\import\routing_metadata\service_belongs_to_domain.csv ⚠️ |
| classifiedAs | 481,501 | 481,501 | 481,501 | 0.00% | kg\import\routing_metadata\service_classified_as.csv |
| usesModel | 481,501 | 481,501 | 481,501 | 0.00% | kg\import\routing_metadata\service_uses_model.csv |

## 三、多跳链抽查（5 条）

查询：事项→handledBy→部门，事项→requiresMaterial→材料，事项→citesLegal→条款→partOf→法规文件

```cypher
MATCH (s:`ZwdmxGJ.GovernmentService`)-[:handledBy]->(d:`ZwdmxGJ.Department`)
MATCH (s)-[:requiresMaterial]->(m:`ZwdmxGJ.Material`)
MATCH (s)-[:citesLegal]->(lc:`ZwdmxGJ.LegalCitation`)-[:partOf]->(lb:`ZwdmxGJ.LegalBasis`)
RETURN s.serviceId AS service, d.name AS dept, m.name AS material, lc.name AS citation, lb.name AS law
LIMIT 5
```

| # | serviceId | 部门 | 材料 | 引用条款 | 法规文件 |
|---|---|---|---|---|---|
| 1 | \"09edf6cc5c4db5e2b49ad653fd195ce0\ | \"广州市卫生健康委员会\ | \"放射卫生技术服务机构资质注销申请表\ | \"关于印发放射卫生技术服务机构管理办法的通知 第四条第一款\ | \"关于印发放射卫生技术服务机构管理办法的通知\ |
| 2 | \"09edf6cc5c4db5e2b49ad653fd195ce0\ | \"广州市卫生健康委员会\ | \"原《放射卫生技术服务机构证书》正、副本原件。\ | \"关于印发放射卫生技术服务机构管理办法的通知 第四条第一款\ | \"关于印发放射卫生技术服务机构管理办法的通知\ |
| 3 | \"a3c18c6fa5dd007e61edbfa2f5defa9e\ | \"广州市卫生健康委员会\ | \"放射卫生技术服务机构资质审定申请表（延续）\ | \"关于印发放射卫生技术服务机构管理办法的通知 第四条第一款\ | \"关于印发放射卫生技术服务机构管理办法的通知\ |
| 4 | \"a3c18c6fa5dd007e61edbfa2f5defa9e\ | \"广州市卫生健康委员会\ | \"仪器设备清单；\ | \"关于印发放射卫生技术服务机构管理办法的通知 第四条第一款\ | \"关于印发放射卫生技术服务机构管理办法的通知\ |
| 5 | \"a3c18c6fa5dd007e61edbfa2f5defa9e\ | \"广州市卫生健康委员会\ | \"专业技术人员情况一览表；\ | \"关于印发放射卫生技术服务机构管理办法的通知 第四条第一款\ | \"关于印发放射卫生技术服务机构管理办法的通知\ |

## 四、结论

- 节点差异实体数: 0
- 边差异关系数: 1

