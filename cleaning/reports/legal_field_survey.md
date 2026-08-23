# 法人服务数据集字段勘察报告

- 运行ID: materialize-legal-2026-08
- 勘察日期: 2026-08-09
- 数据源: `data/法人服务/*.jsonl`(49 个主题文件,共约 27GB / 60.5 万行)
- 勘察脚本: `cleaning/survey_legal.py`,原始统计: `cleaning/reports/legal_survey_raw.json`
- 抽样: 交通运输 / 设立变更 / 其他 / 食品药品 / 司法公证 各 300 行(共 1500 行)+ 交通运输、设立变更各 2000 行专项分析

## 1. 顶层键(表)清单与出现率

每行 JSON 的顶层键全集(23 个,1500 行抽样):

| 表 | 类型 | 非空出现率 | 说明 |
|---|---|---|---|
| AUDIT_ITEM | dict | 1495/1500 | 事项主表(138 字段) |
| AUDIT_ITEM_EXTEND | dict | 1495/1500 | 事项扩展表(189 字段) |
| AUDIT_SPECIAL_TIME | list | 1495/1500 | 特别程序时限(多数元素字段为空) |
| AUDIT_CATALOG_LOBBY | list | 1203/1500 | 办理大厅/窗口(ADDRESS/TIME/TEL/NAME) |
| AUDIT_MATERIAL | list | 1172/1500 | 申请材料(36 字段) |
| AUDIT_ITEM_FLOWSHEET | list | 1131/1500 | 办理环节(12 字段) |
| AUDIT_ITEM_RESULT | list | 1077/1500 | 办理结果(24 字段) |
| OTHER_GUIDES | list/null | 683/1500 | 其他办事指南(YW_CODE/YW_NAME) |
| AUDIT_ITEM_COUNTRY_CODE | list | 635/1500 | 国家事项编码对照 |
| AUDIT_QA | list | 586/1500 | 常见问答(QUESTION/ANSWER) |
| AUDIT_OTHER_DEPT | list | 160/1500 | 联审部门(OTHERDEPTNAME/STEP) |
| ZHONGJIE_SERVICE | list/null | 105/1500 | 中介服务 |
| AUDIT_CHARGE | list | 30/1500 | 收费明细(极少) |
| AUDIT_ZCHJSXZ | list | 6/1500 | 政策梳理(极少) |
| SERVICE_DEPT_LAW | list | **0(恒空)** | 与描述不符:法律依据不在此表 |
| HANDLE_GUID / AUDIT_ITEM_CONDITION / AUDIT_MATERIAL_CONDITION / AUDIT_SPTL | list | **0(恒空)** | 键存在但始终为空 |
| IS_JOINT | str | 恒空 | |
| errCode / msg / result | 标量 | 100% | 接口返回码 |

**重要偏差**:任务描述中的 `SERVICE_DEPT_LAW`(法律依据表)在本数据集中键存在但**始终为空列表**。法律依据的真实来源是 **`AUDIT_ITEM.LAW`**(非空率 0.939),其元素为 dict,含 `LAWNAME`(法律名称)、`ACCORDINGNUMBER`(文号)、`TERMSNUMBER`(条款)、`TERMSCONTENT`(内容)、`LAWURL`、`BFJG`(颁布机关)、`SSRQ`(实施日期)。

## 2. AUDIT_ITEM 关键字段(138 字段中的有效字段)

| 字段 | 非空率 | 语义 |
|---|---|---|
| ITEM_ID | 1.0 | 事项实施清单唯一 ID(32位hex) |
| CATANAME | 1.0 | 事项名称 |
| TASK_NAME | 1.0 | 主项名称(与 CATANAME 多数相同) |
| TASK_CODE | 1.0 | 事项基本码(多办理项共享) |
| TONGYI_CODE | 1.0 | **部门统一社会信用代码**(非事项码!) |
| CATALOG_CODE / NATION_CATALOG_CODE / NATION_TASK_CODE | 1.0 | 目录编码 |
| TASK_TYPE / TASK_TYPE_TEXT | 1.0 | 事项类型(01行政许可/05行政给付/07行政确认/08行政奖励/09行政裁决/20其他行政权力/21公共服务) |
| SERVE_TYPE / SERVE_TYPE_TEXT | 1.0 | 服务对象(逗号分隔多值:自然人,企业法人,…) |
| DEPT_CODE / DEPT_NAME | 1.0 | 实施主体 |
| USE_LEVEL / USE_LEVEL_TEXT | 1.0 | 行使层级(2省级/3市级/4县级) |
| TASK_STATE / TASK_STATE_TEXT | 1.0 | 状态(样本内全为"在用") |
| HANDLE_TYPE / HANDLE_TYPE_TEXT | 0.998 | 办理方式(逗号分隔:窗口办理,网上办理,快递申请) |
| WBSD_LEVEL / WBSD_LEVEL_TEXT | 0.926 | 网上办理深度(I~IV级) |
| ONLINECHECK / ONLINECHECK_TEXT | 1.0 | 可网上办理(0否/1是) |
| PROMISE_DAY + PROMISE_TYPE_TEXT | 0.999 / 1.0 | 承诺办结时限(数字+工作日/自然日) |
| ANTICIPATE_DAY + ANTICIPATE_TYPE_TEXT | 0.90 / 0.93 | 法定办结时限 |
| CRBJSXSM | 0.995 | 承诺时限说明(整句) |
| FDBLSXSM | 0.922 | 法定时限说明(整句) |
| IS_FEE / IS_FEE_TEXT | 1.0 | 是否收费 |
| LINK_TEL | 1.0 | 咨询电话 |
| TSTEL | 0.926 | 投诉电话(HFXS=投诉答复说明,0.287) |
| XHTSDZ | 0.443 | 线下办理地址(简) |
| CKBLLC / WSBLLC | 0.56 / 0.96 | 窗口/网上办理流程(长文本) |
| ACCEPT_CONDITION | 1.0 | 受理条件(纯文本) |
| LAW | 0.939 | **法律依据 list**(见 §1 偏差说明) |
| SCOPES | 0.79 | **跨域通办 list**(SCOPERANGE_TEXT=通办范围/SCOPESHAPE_TEXT=通办形式/DIVISIONS[].DIVISION_NAME=覆盖地区) |
| UNONLINEREASON / UNONLINEREASONOTHER | 0.072 | 网上办理限制原因 |
| TRANSACT_WEB_URL / LOGIN_URL | 0.96 / 0.72 | 在线申办地址(**非详情页 URL**) |
| YEAR / PUBLISHDATE / VERSION_DATE | 1.0 | 年份(2026)/发布日期 |
| PROJECT_TYPE_TEXT | 1.0 | 即办件/承诺件 |

非空率 <5% 而未被采用的字段:CHARGETYPE(0.02)、AGENTS、TRANSACT_APP_URL(0.08)、MOBILE_APPLT_WEBSITE(0)等。

## 3. 子表字段与代码对照(均已验证 *_TEXT 伴随字段取值)

- **AUDIT_MATERIAL**: ORDERNUM(序号)、MATERIAL_NAME、MATERIAL_TYPE(_TEXT: 1证件证书证明/2申请表格文书/3其他)、SOURCE_TYPE(_TEXT: 10申请人自备/20政府部门核发/99其他)、IS_NEED(_TEXT: 1必要/2非必要/3容缺后补)、ZZHDZB(_TEXT: 1纸质/2电子化/3纸质/电子化,即提交形式)、PAGE_NUM("1",原件份数)、PAGE_COPYNUM(复印件份数)、PAGE_FORMAT(A4)、MATERIAL_DESC(0.13)/FILL_EXPLIAN(0.59,填报须知)、FORM_GUID/EXAMPLE_GUID(附件 list:{ATTACHNAME, FILEPATH},FILEPATH 为 static.gdzwfw.gov.cn 公开链接)
- **AUDIT_ITEM_FLOWSHEET**: TRANSACTOR(办理人员)、TRANSACT_TIME_LIMIT(办理时限)、TRANSACT_RESULT(办理结果)、CHECK_STANDARD(审查标准)、STEP→STEP_TEXT(acceptance受理/investigate审查/decide决定/accreditation制证/delivery送达/addressee收件)、UNTI_LINK→UNTI_LINK_TEXT(applitAndAccept申请与受理/reviewAndDecision审查与决定/ConfermentAndService颁证与送达)
- **AUDIT_ITEM_RESULT**: NAME、RESULT_TYPE_TEXT(APPROVAL批文批复/CERTIFICATE证件执照/PROOF证明文件/AUTHENTICATION_REPORT鉴定报告/WORK_RESULT其他文件)、SUBJECT_RESULT_TYPE_TEXT(10证照/20批文/30其他)、RESUL_EXPLAIN(0.33,说明)、CARD_VALIDDATE(0.35,有效期)、RESULTATTACHLIST/RESULT_BLANK/RESULT_SAMPLE(附件 list:{ATTACHNAME, FILEPATH})
- **AUDIT_QA**: QUESTION / ANSWER(直接对应)
- **AUDIT_CHARGE**(仅 30/1500): FEE_NAME/FEE_STAND/FEE_TYPE_TEXT 等;v1 schema 无收费明细字段,仅取 IS_FEE_TEXT
- **AUDIT_CATALOG_LOBBY**: NAME(窗口名)/ADDRESS/TIME(办公时间)/TEL;XHTSDZ 为空时用作办理地址后备
- **AUDIT_SPECIAL_TIME**: 元素多为空(SHIXIAN/SXDW_TEXT 等,非空率<0.26),v1 无对应字段,不采用

## 4. 唯一键分析(交通运输+设立变更前 2000 行)

| 候选键 | 唯一值数 | 重复键数 | 结论 |
|---|---|---|---|
| ITEM_ID | 1915 | 11 | **稳定唯一键**(事项实施清单粒度;重复=同一事项跨主题/跨年出现) |
| TASK_CODE | 742 | 244 | 事项基本码,一个码对应多个办理项,不唯一 |
| TONGYI_CODE | 46 | 34 | 部门信用代码,完全不可用 |
| (CATANAME, DEPT_CODE) | 742 | 244 | 与 TASK_CODE 同粒度 |

→ **编码字段取 ITEM_ID**;去重按 ITEM_ID 全局(跨文件)保留首次;"官方列表出现次数"取 ITEM_ID 在全数据集中的出现次数(≥1)。

## 5. 数据异常(勘察发现)

1. **HTML 污染行**: 交通运输.jsonl 第 593 行起出现采集时接口返回的 HTML 错误页(`<!DOCTYPE html>` 等,连续多行),JSON 解析失败 → 进 rejects(reason=bad_json)。
2. **空结果行**: errCode=0 但 msg="未找到匹配的信息"、无 AUDIT_ITEM(1500 行抽样中 5 行)→ rejects(reason=no_audit_item)。
3. **SERVICE_DEPT_LAW / HANDLE_GUID / AUDIT_ITEM_CONDITION / AUDIT_MATERIAL_CONDITION / AUDIT_SPTL 恒为空列表**,与任务描述不符,法律依据改从 AUDIT_ITEM.LAW 取。
4. **TONGYI_CODE 命名误导**: 值是部门统一社会信用代码而非事项统一编码。
5. 同一 ITEM_ID 会出现在多个主题文件(跨主题重复),去重时主题分类以**首次出现**的文件为准。
