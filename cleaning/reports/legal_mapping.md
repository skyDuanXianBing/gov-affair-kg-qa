# 法人服务 AUDIT_* → gdzwfw-large-human-readable-v1 字段映射表

- 运行ID: materialize-legal-2026-08
- 依据: `cleaning/reports/legal_field_survey.md` 勘察结论
- 通则: 代码值一律用同名 `*_TEXT` 伴随字段转中文;无 TEXT 伴随的保留原值;找不到来源的 v1 字段填空字符串 `""` 或空列表 `[]`,并在"无来源字段清单"中注明。

## 事项

| v1 字段 | 来源 | 处理 |
|---|---|---|
| 事项类型 | AUDIT_ITEM.TASK_TYPE_TEXT | 直接取值 |
| 名称 | AUDIT_ITEM.CATANAME | 空则 reject |
| 官方列表出现次数 | 派生统计 | ITEM_ID 在 49 个文件中的总行数(跨主题),最小为 1 |
| 实施主体 | AUDIT_ITEM.DEPT_NAME | |
| 服务对象 | AUDIT_ITEM.SERVE_TYPE_TEXT | 逗号分隔原文保留 |
| 状态 | AUDIT_ITEM.TASK_STATE_TEXT | |
| 编码 | AUDIT_ITEM.ITEM_ID,缺失按 TASK_CODE→ROWGUID→NATION_TASK_CODE 兜底 | 全局唯一键;全空则 reject;全局去重保留首次 |
| 行使层级 | AUDIT_ITEM.USE_LEVEL_TEXT | |
| 详情返回编码 | 顶层 errCode | int→str;**成功行无 errCode 键→""**(errCode 仅出现在被拒的"未找到匹配的信息"行) |
| 主题分类 | 文件名去掉 .jsonl | 法人服务特有;去重时以首次出现的文件为准 |

## 办理

| v1 字段 | 来源 | 处理 |
|---|---|---|
| 办理地址 | AUDIT_ITEM.XHTSDZ,空则 AUDIT_CATALOG_LOBBY[0].ADDRESS | |
| 办理方式 | AUDIT_ITEM.HANDLE_TYPE_TEXT | 按英文逗号 split → list;空则 [] |
| 办理深度 | AUDIT_ITEM.WBSD_LEVEL_TEXT | |
| 办理环节 | AUDIT_ITEM_FLOWSHEET[] | {办理人员:TRANSACTOR, 办理时限:TRANSACT_TIME_LIMIT, 办理结果:TRANSACT_RESULT, 审查标准:CHECK_STANDARD, 步骤:STEP_TEXT, 环节:UNTI_LINK_TEXT} |
| 可网上办理 | AUDIT_ITEM.ONLINECHECK_TEXT | |
| 咨询电话 | AUDIT_ITEM.LINK_TEL | |
| 承诺办结时限 | AUDIT_ITEM.PROMISE_DAY + PROMISE_TYPE_TEXT | 拼接如 "6个工作日";PROMISE_DAY 空则 "" |
| 承诺时限说明 | AUDIT_ITEM.CRBJSXSM | |
| 投诉电话 | AUDIT_ITEM.TSTEL | |
| 是否收费 | AUDIT_ITEM.IS_FEE_TEXT | |
| 法定办结时限 | AUDIT_ITEM.ANTICIPATE_DAY + ANTICIPATE_TYPE_TEXT | 拼接如 "45个工作日";空则 "" |
| 法定时限说明 | AUDIT_ITEM.FDBLSXSM | |
| 窗口办理流程 | AUDIT_ITEM.CKBLLC | 长文本 |
| 网上办理流程 | AUDIT_ITEM.WSBLLC | 长文本 |
| 网上办理限制说明 | AUDIT_ITEM_EXTEND.UNONLINEREASON,空则 AUDIT_ITEM.UNONLINEREASONOTHER | |
| 跨域通办 | AUDIT_ITEM.SCOPES[] | {覆盖地区: DIVISIONS[].DIVISION_NAME 去重后"、"连接, 通办形式: SCOPESHAPE_TEXT, 通办范围: SCOPERANGE_TEXT} |

## 办理结果(list)

来源 AUDIT_ITEM_RESULT[]:

| v1 字段 | 来源 |
|---|---|
| 名称 | NAME |
| 类型 | RESULT_TYPE_TEXT,空则 SUBJECT_RESULT_TYPE_TEXT |
| 说明 | RESUL_EXPLAIN |
| 有效期说明 | CARD_VALIDDATE |
| 公开附件 | RESULTATTACHLIST[] → {名称:ATTACHNAME, 公开链接:FILEPATH} |

## 常见问答(list)

来源 AUDIT_QA[]: {问题:QUESTION, 答复:ANSWER}

## 申请

| v1 字段 | 来源 |
|---|---|
| 受理条件 | AUDIT_ITEM.ACCEPT_CONDITION |
| 材料[] | AUDIT_MATERIAL[]: {序号:ORDERNUM, 名称:MATERIAL_NAME, 份数:PAGE_NUM, 页数:**无来源→""**, 提交形式:ZZHDZB_TEXT, 材料类型:MATERIAL_TYPE_TEXT, 材料来源:SOURCE_TYPE_TEXT, 是否必要:IS_NEED_TEXT, 说明:MATERIAL_DESC 空则 FILL_EXPLIAN, 公开附件:FORM_GUID[]+EXAMPLE_GUID[] 合并 → {名称:ATTACHNAME, 公开链接:FILEPATH}} |

注: PAGE_NUM 为原件份数(官网"份数"列),PAGE_COPYNUM 为复印件份数,v1 无对应字段不取;"页数"在原始数据中无对应字段(PAGE_FORMAT 是纸张规格 A4),置 ""。

## 法律依据(list)

来源 **AUDIT_ITEM.LAW[]**(非 SERVICE_DEPT_LAW,该表恒空):

| v1 字段 | 来源 |
|---|---|
| 名称 | LAWNAME |
| 文号 | ACCORDINGNUMBER |
| 条款 | TERMSNUMBER |
| 内容 | TERMSCONTENT |

## 来源

| v1 字段 | 取值 |
|---|---|
| 官方详情JSON | ""(不内嵌原始行,避免输出翻倍;原始行可由 SHA 校验回溯) |
| 官方详情JSON_SHA256 | 对原始行(strip 后 UTF-8 字节)的 sha256 hex |
| 结构化记录_SHA256 | 记录构造完成后,对本字段置 "" 的完整记录做 `json.dumps(ensure_ascii=False, sort_keys=True)` UTF-8 sha256,再回填 |
| 详情页URL | ""(**无来源**:TRANSACT_WEB_URL/LOGIN_URL 为在线申办地址,非详情页) |
| 运行ID | "materialize-legal-2026-08" |
| 采集序号 | 文件内 1-based 原始行号(含坏行) |

## 清洗与拒绝规则(v2 修订,质量审计修复后)

1. JSON 解析失败(如 HTML 污染行)→ rejects,reason=`bad_json`
2. 无 AUDIT_ITEM 且非 v2 套餐服务(errCode 行"未找到匹配的信息")→ rejects,reason=`no_audit_item`
3. **编码兜底**: ITEM_ID 缺失时按 `TASK_CODE → ROWGUID → NATION_TASK_CODE` 兜底;编码或名称仍为空才进 rejects,reason=`missing_code_or_name`
4. 全局按编码去重保留首次(编码命名空间 = ITEM_ID ∪ 兜底键 ∪ v2 id)→ 重复行计数(跨主题重复单独统计),不进 rejects
5. 字符串规范化(按序执行):
   a. 含 `&` 或 `<` 时:`html.unescape()` 迭代解码至稳定(至多 3 轮,覆盖 `&amp;amp;` 双重编码)→ `<br>` 系列标签转 `\n` → 白名单 HTML 标签(p/div/span/a/b/strong/em/i/u/ul/ol/li/table/thead/tbody/tfoot/tr/td/th/font/h1-h6/hr/img/sub/sup/section/article/center)删除(只删标签不动文本)
   b. `\r\n` 与孤立 `\r` 统一为 `\n`
   c. `\t` 转单个空格
   d. 删除 `\x7f`、其余 C0 控制字符(除 `\n`)与 C1 控制字符(U+0080–U+009F)
   e. 去首尾空白
6. 输出 `ensure_ascii=False`,每行一个 JSON,键序固定为 schema 声明顺序

## v2 套餐服务映射(special_item_type="TC",小写键 schema,共 160 条/28 文件)

识别条件: 无 AUDIT_ITEM 且含 `catalog_name`(或 special_item_type="TC")。物化到同一 v1 输出,去重/计数流程一致。

| v1 字段 | v2 来源 |
|---|---|
| 事项.事项类型 | special_item_type_text(取值"套餐服务") |
| 事项.名称 | catalog_name(空则 subject_name) |
| 事项.实施主体 | impl_org_name |
| 事项.服务对象 | service_object_text |
| 事项.状态 | status_text |
| 事项.编码 | **id** |
| 事项.行使层级 | authority_level_text |
| 事项.详情返回编码 | ""(v2 无 errCode 键) |
| 办理.办理地址 | window_list[0].address |
| 办理.办理方式 | application_channel_text 按逗号 split |
| 办理.办理深度 | online_apply_depth_text(v2 无 I~IV 级,为能力描述原文) |
| 办理.办理环节 | [](**无来源**) |
| 办理.可网上办理 | **派生**: application_channel_text 含"网上办理"→"是",否则"否";该字段空→"" |
| 办理.咨询电话 / 投诉电话 | consult_phone / complaint_phone |
| 办理.承诺/法定办结时限 | promise_time+promise_time_unit_text / legal_time+legal_time_unit_text(拼接同 v1) |
| 办理.承诺/法定时限说明 | promise_time_note / legal_time_note |
| 办理.是否收费 | is_charge_text |
| 办理.窗口/网上办理流程 | window_flow / online_flow |
| 办理.网上办理限制说明 | ""(**无来源**) |
| 办理.跨域通办 | application_scope_v2_list[] → {覆盖地区: division_list[].division_name 去重"、"连接, 通办形式: application_shape_text, 通办范围: application_scope_v2_text} |
| 办理结果 | result_sample_list[] → {名称: name, 类型: subject_result_type_text, 说明: "", 有效期说明: "", 公开附件: sample_att_list[]} |
| 常见问答 | question_list[] → {问题: question, 答复: answer}(实测全空) |
| 申请.受理条件 | conditions |
| 申请.材料 | submit_material_list[] → {序号: sort_order, 名称: document_name, 份数: origin_count, 页数: "", 提交形式: document_media_text, 材料类型: material_type_text, 材料来源: document_source_text, 是否必要: document_need_type_text, 说明: note, 公开附件: blank_att_list+sample_att_list} |
| 法律依据 | clause_list[] → {名称: legal_name, 文号: reference_number, 条款: clause_number, 内容: content} |
| 附件(v2 通用) | {名称: name 空则 original_name, 公开链接: **waiwang_url**(外网;neiwang_url 为内网地址不取)} |

## 无来源字段清单(置 "" / [])

- 办理结果.说明、有效期说明 —— 有来源但填充率低(RESUL_EXPLAIN 0.33 / CARD_VALIDDATE 0.35),非"无来源"
- **申请.材料.页数** —— 无来源
- **来源.详情页URL** —— 无来源
- **来源.官方详情JSON** —— 按任务约定填 ""
