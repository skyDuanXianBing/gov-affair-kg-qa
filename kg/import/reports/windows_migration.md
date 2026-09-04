# 政务大模型项目 Mac → Windows 迁移报告（2026-08-30 夜首迁移 + 2026-08-31 晚续做轮）

目标：Mac（colima）上的 OpenSPG 灌图迁移到 Windows（mywin-zihang，Docker Desktop），
在 Windows 上全新起栈、全新灌图，无人值守继续跑；Mac 本地仓库与正在运行的导入监督器不动。

> **2026-08-31 晚续做轮见文末 §10-§17**：发现并修复 v1 监督器超时漏片缺陷（checkpoint 热修复 +
> supervise_win2 断点续跑监督器）、Neo4j 并发写卡死事件与看门狗、B5/cleaned/unified 补传、
> 验收对账数据。§1-§9 为首夜记录，保留原貌。

## 1. 迁移目标与布局

- Windows 目标目录：`E:\graduate\gov-affair-kg-qa\repo\`（全新目录，只新建不删除任何 E 盘既有文件）。
- Mac 源仓库：`/Volumes/f/AllMyData/MyUnderGraduate/政务大模型`（未改动、未移动）。
- 不迁移：`kg/deploy/volumes/`（54G docker 卷，Windows 全新起栈）、`kg/venv`、`kg/_ref`、
  `kg/import/shards/`（可由源数据重建）、`cleaning/trial`、`kg/pilot/{csv,csv_v2,pilot_10000.jsonl}`、
  `kg/pilot/GovAffair/builder/ckpt`。

## 2. 传输桶与校验结果（gzip -1 流式 tar over ssh，多流并行）

| 桶 | 内容 | 字节（Mac 实测） | 耗时 | 校验 |
|---|---|---|---|---|
| B1 | 仓库本体（含 .git、kg/import 全部、routing_metadata 82M、schemas、论文 PDF、.env） | 141,649,718（424 文件） | ~4 min | 文件数+字节数精确一致；Windows `git log` 正常 |
| B2 | `build/shared_ids/{pilot,personal}` | 4,314,398,456（14 文件） | pilot 385s ∥ personal 453s | 14 文件字节数精确一致；stats.json 关键数 30,111/9,394/2,758 在 |
| B3 | `data/pilot` 灌图必需 18 个 CSV（manifest 36 任务实际引用的全部 pilot 源表 + testset） | 1,819,066,578 | 288s | 18 文件字节数精确一致 |
| B4 | `data/personal` 全目录 | 26,023,767,386（28 文件，24.24 GiB） | ~70 min（19G documents 单流 + 其余并行） | 28 文件字节数精确一致 |
| B5 | `data/pilot` 大文件（documents 9.6G / documents_chunks 17.4G / legal_bases 5G / materials / 原始边表 / manifests） | ~33.4 GiB | 后台补传 | 见 §8 遗留 |

吞吐实测：单流 ~1.6-4 MB/s；2-3 流并行聚合 ~10 MB/s（WiFi 瓶颈）；CSV gzip 压缩比 ~5-10x。

**B3 说明**：manifest 的 36 个任务只引用 data/pilot 中 ~1.8G 的 17 张表
（documents/documents_chunks/legal_bases 原始表/原始 materials/原始边表不参与 pilot 灌图），
故先传必需子集让灌图当晚上线，大文件随后台补传。

## 3. Windows 侧适配（全部只改 Windows 副本，Mac 仓库不动）

1. **docker-compose**（`repo\kg\deploy\docker-compose.yml`）：
   - 去掉 `/etc/localtime` 挂载（Linux-only，Windows 会起不来）；
   - server 服务加 `extra_hosts: ["host.docker.internal:host-gateway"]`
     （该 Docker 28.5.1 未自动注入 host.docker.internal，容器访问不到宿主机 mock 嵌入服务）；
   - 端口 8887/7474/7687/9000/9001/13306 实测全空闲，沿用原映射，未改。
2. **docker 凭据**：ssh 非交互会话无法访问 Windows 凭据管理器（docker-credential-desktop 报
   "A specified logon session does not exist"），用独立 `--config E:\graduate\gov-affair-kg-qa\docker_cfg`
   绕过，不动用户级 docker 配置。
3. **kg_user_model 模型注册**：全新 MySQL 没有 mock-emb 的用户模型注册，直接插入 Mac 同款行
   （instance_id `39bff17a37cb4337a0d2527e5a86b1f6`，base_url 改 `http://host.docker.internal:18999/v1`）。
   否则创建项目时 `setLocalVectorizerPlatform` NPE（modelId 解析不到）。
4. **登录密码**：种子密码为默认 `openspg@kag`，按 kg/import/README §1 记录的方法
   `dw_access_key = sha256Hex(sha256Hex(明文+"OPENSPG") + salt)` 重置回 `openspg@kag2026`（盐 Ktu4O）。
5. **publish_schema.py（Windows 副本）**：
   - 模板项目兜底：全新库无 GovAffair 模板项目，回退读 `checkpoints/mac_p2_config_reference.json`
     （迁移前从 Mac MySQL 导出的项目 2 完整 config，随 B1 传过去）；
   - URL 重写：192.168.x 网段 mock/ollama 地址 → `http://host.docker.internal:18999`；
   - 项目 id 参数化：Mac 上 ZwdmxGJ=项目 2，Windows 全新库自增为 **项目 1**，corrected_config
     改为以实际 id 写 config（README 记录的"project 段 id 陷阱"）。
6. **run_import.py（Windows 副本）**：`fcntl` 文件锁替换为 `msvcrt.locking`（Unix-only 模块）。
7. **manifest（Windows 副本）**：`project_id` 2 → 1。
8. **checkpoint**：全新 `checkpoints/state_win.json` 从空开始（Mac 的 state.json 仅作参考随 B1 传过去，
   Windows 全新栈不认 Mac 的 job）。
9. **边表预去重**（`kg/import/dedup_edges.py`，Windows 副本）：
   - `service_based_on_out.csv`：6,305,710 → 6,305,650（按 (service_id, legal_citation_id) 去重，删 60 行）
   - `service_requires_material_out.csv`：2,153,892 → 2,153,859（按 (service_id, material_id) 去重，删 33 行）
   - 原文件保留为 `*.full.csv`，去重报告 `build/shared_ids/pilot/dedup_report.json`。
   （SPG (s,p,o) UPSERT 语义下重复行不改变最终边数，去重只为省 Builder 吞吐。）

## 4. Windows OpenSPG 栈状态

- 4 容器全部 Up：release-openspg-{server,mysql,neo4j,minio}（compose 项目 `openspg`，
  数据卷 `repo\kg\deploy\volumes\`，全新）。server -Xmx8192m，宿主 Docker 32G 内存/20 核。
- 踩坑记录：server 首启比 MySQL 初始化快，JDBC 拒连自杀；MySQL 就绪后 `docker restart release-openspg-server` 即恢复（重启策略 always，以后宿主机重启理论上也会自愈，若再遇到拒连重启一次 server 容器即可）。
- 健康：http://127.0.0.1:8887 → 200；登录 openspg / openspg@kag2026 通过。
- Neo4j：`neo4j://localhost:7687`，密码 `neo4j@openspg`，库 `zwdmxgj`。

## 5. Schema 与灌图启动

- `publish_schema.py`：创建项目 ZwdmxGJ（id=1），schema 一次提交成功，
  **17 实体 / 29 关系全部发布**，核对报告 `repo\kg\import\reports\schema_types.md`（与 Mac 一致，
  Double→Float 3 处适配）。
- 监督器 `kg/import/supervise_win.py`（supervise_v5.sh 的 Python 移植）：
  - mock_llm_threaded.py 18999 已由 schtasks `GovKGMockLLM`（onstart，SYSTEM）常驻；
  - 主监督器由 schtasks `GovKGImportSupervisor`（SYSTEM，断 ssh 存活）拉起；
  - 阶段：路由 10 表 → 共享 4 表 → 弱实体 3 道 → services 单道 → 关系 5 道 → service_routing 2 道 → verify_graph；
  - 日志：`repo\kg\import\checkpoints\win_supervisor.log` + 每道 `win_*.log`。
- 起飞验证（2026-08-31 00:30 复核，全部通过）：
  - service_domains：Builder FINISH，Neo4j `ZwdmxGJ.ServiceDomain` = 1（期望 1）✓
  - departments：25,855 行 FINISH（880s），Neo4j `ZwdmxGJ.Department` = 25,855（期望 25,855）✓
  - materials：30,111 行 FINISH（1070s），Neo4j `ZwdmxGJ.Material` = 30,111（期望 30,111）✓
  - 路由层 10 表全部 FINISH；关系抽查 belongsToDomain=100 / belongsToScheme=50 / parentCategory=49
    与源表一致（50 分类 + 50 模型、50 分类挂 1 scheme、49 非根分类）。
  - 导入已确认进入无人值守状态（监督器 schtasks SYSTEM 运行，断 ssh 不死）。
- Windows 电源：**已把 AC 睡眠设为从不**（原 1 小时会睡，会中断灌图）：
  `powercfg /change standby-timeout-ac 0`；还原用 `powercfg /change standby-timeout-ac 3600`。
- 事故记录（无数据损失）：documents/documents_chunks 首轮实际已传完（dir 显示全尺寸+原始 mtime），
  但 PowerShell 递归求和读数滞后误判为"部分文件"，手工 del 后由 Mac 原件重传。
  教训：对账以 `dir`/逐文件字节数为准，不信任大目录递归求和的即时读数。

## 6. 与 Mac 的差异清单（后续在 Windows 上工作要知道的）

| 项 | Mac | Windows |
|---|---|---|
| 项目 id | 2 | **1**（manifest 已改，publish_schema 已参数化） |
| 嵌入服务地址 | 192.168.31.80:18999（config 里残留旧 IP 也无碍） | host.docker.internal:18999（compose extra_hosts 注入） |
| 磁盘路径 | /Volumes/f/AllMyData/MyUnderGraduate/政务大模型 | E:\graduate\gov-affair-kg-qa\repo |
| checkpoint | checkpoints/state.json（Mac 任务，勿混用） | checkpoints/state_win.json |
| 论文目录名 | 论文/xxx: yyy/（含冒号） | 论文/xxx_ yyy/（NTFS 不允许冒号，tar 自动改 _，内容一致） |
| supervisor | supervise_v5.sh + bash | supervise_win.py + schtasks |

## 7. 明早用户清单

1. Windows 不休眠已由迁移会话设置好（AC 从不睡眠）；若想还原见 §5 末尾命令。
2. 看进度：
   - `type E:\graduate\gov-affair-kg-qa\repo\kg\import\checkpoints\win_supervisor.log`（阶段推进）
   - 各泳道 `win_*.log`；Neo4j 计数（例）：
     `docker exec release-openspg-neo4j cypher-shell -u neo4j -p neo4j@openspg -d zwdmxgj "MATCH (n) RETURN labels(n)[0], count(*);"`
3. 全部跑完后监督器会自动跑 verify_graph.py 出 `reports/pilot_reconciliation.md`。
4. Mac 明早可直接关机；Mac 侧监督器与栈不用管（迁移期间未动）。

## 8. 遗留与风险

- B5（pilot 大文件 ~33.4G：documents/documents_chunks/legal_bases 原始表等）+ `data/cleaned`、
  `data/unified`（各 29G）传输中，不影响灌图（36 任务不引用它们）；Mac 关机即止，缺了随时可补传。
- services/conditions 等长表按 Mac 经验单片 350K 行安全；Windows 内存更大，若仍遇 Builder OOM，
  降低监督器弱实体/关系阶段并行道数（supervise_win.py stage() 的 lanes 列表）。
- Mac MySQL 里 kg_reason_task 等历史运行记录未迁移（仅迁移 kg_user_model 模型注册与项目 config）。
- schtasks 两个任务（GovKGMockLLM onstart / GovKGImportSupervisor once）创建为 SYSTEM；
  不需要时 `schtasks /delete /tn GovKGMockLLM /f` 等。
- vectorizer.modelId 沿用 Mac 的 instance_id（39bff17a…），kg_user_model 行已复制，语义等价。
- 长连接劣化观察：>10min 的 ssh 流偶发吞吐塌缩（19KB/s 级），杀掉重连即恢复；长传建议加
  `-o ServerAliveInterval=30` 并分文件分桶。

## 9. 本次传输/起栈命令速查（在 Windows 补传时照抄）

```bash
# Mac 侧压缩流传输（示例：data/cleaned）
cd "/Volumes/f/AllMyData/MyUnderGraduate/政务大模型" \
  && COPYFILE_DISABLE=1 tar --disable-copyfile -cf - data/cleaned | gzip -1 \
  | ssh mywin-zihang "tar xzf - -C E:\\graduate\\gov-affair-kg-qa\\repo"
# 注意：bsdtar --exclude './build' 会连带排除嵌套的 kg/build（无通配符的目录名按basename匹配），
# 用 --null -T 文件清单代替 --exclude。
```

---

## 10. 续做轮：v1 监督器超时漏片缺陷（2026-08-31 晚发现）

v1 监督器（supervise_win.py）的 run_import `--timeout 28800`（8h）小于 Windows 实测
单片处理时长（最长 10.5h）。首夜至 08-31 白天的实际后果：

- 弱实体 4 表（process_steps/results/service_channels/fees）的**片 1** 等待方超时退出、
  checkpoint 停在 SUBMITTED，但 **Builder Job 在服务端实际全部 FINISH**（Job 17/18/20/21
  分别于 09:43/12:09/18:36/20:43 完成，数据已写入 Neo4j）；
- v1 阶段严格顺序、超时不回头重试 → **各表剩余分片（process_steps 4 片、
  service_channels 4 片、fees 1 片、services 1 片）永远不会再被提交**；
- 更危险的是：services 片 1（Job 23）也会在 02:05 超时，v1 将带着不完整的
  GovernmentService 实体进入关系表阶段，关系导入会大面积悬空。

## 11. 修复：checkpoint 热修复 + supervise_win2 断点续跑监督器

新增 `kg/import/repair_state_win.py`（Windows 副本）：

- 扫 checkpoint 中非 SUCCESS 且已提交 job 的分片，查 `/public/v1/builder/job/get`：
  FINISH → 标 SUCCESS（带真实 Builder 耗时）；FAILURE → 删条目待重跑；
  RUNNING → 跳过或 `--wait` 轮询到终态（上限 26h，防重启后死 job 卡住）；
- 写入走与 run_import 相同的 `.lock` 文件锁 + 锁内读-改-写，与运行中的导入进程互斥安全
  （run_import 的 save_state 本身是"状态只升不降"合并写，不会被回退）。
- 21:50 实际执行：修复 4 分片（process_steps#17 / service_channels#18 / results#20 /
  fees#21 → SUCCESS），services#23 正确跳过（仍 RUNNING）。

新增 `kg/import/supervise_win2.py` 替代 v1，schtasks 换为 `GovKGImportSupervisor2`
（SYSTEM，Once + AtStartup 双触发器，重启 Windows 自动续跑）：

- `--timeout 86400`（24h）；
- 启动即 repair（无等待）；阶段 3 后 `repair --wait` 等 RUNNING job 终态再续跑 services，
  避免 SUBMITTED 分片被重复提交（run_import 只跳过 SUCCESS）；
- 阶段 5/6 后各有 sweep 兜底轮（repair --wait + 重跑仍有非 SUCCESS 分片的表）；
- 弱实体分道重平衡：process_steps（4 片）/ service_channels（4 片）/ fees+已完成表（1 片）。

20:54 v2 起飞验证：路由/共享表全 SUCCESS 秒过自检；三道弱实体片 1 全部 SHA256 命中
checkpoint 跳过；片 2 已提交（Job 24/25/26）；加上 Job 23 共 4 个 Builder Job 并发。

## 12. 事故：Neo4j 并发写卡死与恢复（2026-08-31 20:54-21:21）

3 个新 Job（24/25/26）启动瞬间，Neo4j 数十个来自 server 的写事务集体卡在 **Closing**
状态 20 分钟零推进；txlog 20:54 后无写入；server CPU 0.06%（处理线程全部阻塞等写事务）；
直连查询报 `Database 'zwdmxgj' not up to the requested version`。判定为
Docker Desktop（WSL2 + gRPC-FUSE 卷）IO 层卡死而非锁竞争（无 Running 事务、全 Closing，
Neo4j CPU 空转 36%）。

处置：`docker restart release-openspg-neo4j`（compose restart:always，事务原子回滚）。
重启后完全恢复：server driver 自动重连、GovernmentService 导入进度无缝继续（重启前
122,185 节点继续增长）、事务流 ~36 tx/s、直连查询恢复。**重启 Neo4j 对在途 Builder Job
无致命影响（实证）**。

防复发：新增 `kg/import/watchdog_win.py` + schtasks `GovKGWatchdog`（SYSTEM，onstart）：
每 5 分钟查 SHOW TRANSACTIONS，zwdmxgj 存在 Closing >15 分钟（正常 <2s）且确有导入在途
时自动 restart neo4j，重启后冷却 5 分钟，单日上限 6 次。日志
`checkpoints/win_watchdog.log`。

## 13. 验收数据（2026-08-31 21:35 采集）

**Neo4j 实体对账（已完成表，与源数据精确一致）：**

| 实体 | Neo4j | 期望（源数据） | 结果 |
|---|---|---|---|
| Department | 25,855 | departments.csv 25,855 | ✓ |
| Material | 30,111 | stats.json material_shared_nodes 30,111 | ✓ |
| LegalBasis | 2,758 | stats.json legal_bases 2,758 | ✓ |
| LegalCitation | 9,394 | stats.json legal_citations 9,394 | ✓ |
| FAQ | 126,849 | faqs.csv 126,849 | ✓ |
| ServiceCondition | 481,500 | conditions 3 片全部 | ✓ |
| ServiceResult | 316,827 | results.csv 316,827 | ✓ |
| ProcessStep | 322,491 | 片 1（322,491），片 2 在途 | 进行中 |
| ServiceChannel | 350,000 | 片 1（350,000），片 2 在途 | 进行中 |
| Fee | 350,000 | 片 1（350,000），片 2 在途 | 进行中 |
| GovernmentService | 122,185+ | 片 1 350,000 在途（~35%） | 进行中 |
| 路由层 5 类 | 1/1/50/50/1 | domain/scheme/category/model | ✓ |

stats.json 三关键数（30111 / 9394 / 2758）✓；关键文件 9/9 存在 ✓；
`python -m unittest tests.test_build_shared_ids tests.test_score_testset` → **Ran 25 tests OK** ✓。
关系链路抽查（handledBy/requiresMaterial/basedOn）待阶段 5 关系表导入完成后
由 verify_graph.py 全量对账（监督器阶段 7 自动执行）。

**去重（首夜已完成，本轮复核 dedup_report.json）：**
service_based_on_out 6,305,710 → 6,305,650（-60）；service_requires_material_out
2,153,892 → 2,153,859（-33）；原始文件保留为 `*.full.csv`，产物在
`build/shared_ids/pilot/dedup/`。

## 14. B5 + cleaned + unified 补传（2026-08-31 20:57 起，3 桶并行 gzip -1 流式）

对照 Mac 全清单精确 diff：缺 34 文件共 60.16 GiB（pilot 10 个 24.4G：documents_chunks
17.4G/legal_bases 4.95G/service_requires_material 762M/service_based_on 560M/materials
280M/manifests 79M；unified 3 个 20.6G；cleaned 24 个 ~24G），已传 71 文件字节数全部
一致。三桶并行 gzip -1 流式传输，逐桶日志 `/tmp/gov_transfer/transfer_*.log`，脚本可重跑续传。

**结果（23:55 终验）**：**全部 34 个缺失文件传输完成，data/{pilot,cleaned,unified}
共 105 文件 91.78 GiB 数量与字节数 100% 一致**；关键文件 SHA256 抽查
（services.csv / legal_bases.csv）两端逐一吻合。`data/pilot` 29 文件全齐（灌图全部
数据源在位）。cleaned 桶传输流曾遭 SSH 长连接劣化卡死一次（首夜已知问题，杀流重连
即恢复）。E 盘剩余 ~543 GB（首迁移前 635 GB）。

## 15. 当前无人值守布局（schtasks，全部 SYSTEM）

| 任务 | 作用 | 触发 |
|---|---|---|
| GovKGMockLLM | mock 嵌入服务 18999 | onstart 常驻 |
| GovKGImportSupervisor2 | supervise_win2 断点续跑监督器（v1 已删） | Once + AtStartup(+2min) |
| GovKGWatchdog | Neo4j Closing 卡死自动重启 | Once + AtStartup(+3min)，5min 轮询 |

断 SSH 全部存活；Windows 重启后三个任务按触发器自动恢复；AC 睡眠已设从不（首夜）。

## 16. 明早用户清单（更新版）

1. 看监督器进度：`type E:\graduate\gov-affair-kg-qa\repo\kg\import\checkpoints\win_supervisor.log`
   （阶段推进）、各道 `win_*.log`、看门狗 `win_watchdog.log`（若夜间重启过 neo4j 会有记录）。
2. Builder Job：`http://127.0.0.1:8887`（openspg / openspg@kag2026）或
   `curl http://127.0.0.1:8887/public/v1/builder/job/get?id=N`。
3. Neo4j 计数：`docker exec release-openspg-neo4j cypher-shell -u neo4j -p neo4j@openspg -d zwdmxgj "MATCH (n) UNWIND [l IN labels(n) WHERE l STARTS WITH 'ZwdmxGJ'] AS l RETURN l, count(*);"`
4. 剩余量预估（按当前 ~36 tx/s 物理吞吐）：弱实体剩 9 片 + services 1 片（每片 350K 行
   约需 8-11h，3 道并行）→ 预计 1-2 天进入关系表阶段；关系表 ~35 片（大表
   service_based_on 6.3M 行 18 片）还需更久。全部自动推进，无需人工干预。
5. ~~补传~~ 已全部完成（§14 终验通过）；E 盘剩余 524 GB。
6. 全部跑完后监督器自动执行 verify_graph.py 出对账报告。

## 17. 失败 / 阻塞 / 遗留（累计）

- ~~v1 监督器超时漏片~~（已修复，§11）；~~Neo4j 20:54 卡死~~（已恢复 + 看门狗，§12）。
- Windows 单片导入时长约为 Mac 的 3-17 倍（Docker Desktop 跨界文件系统 IO 所致，
  E 盘本身为 NVMe）。已排除 GPU/内存因素；如需根治需把 volumes 迁到 WSL2 原生 ext4
  （涉及重建容器卷，本次未做）。
- 4 Job 并发写曾触发 IO 卡死一次；看门狗兜底后继续用 3-4 并发（吞吐收益 vs 卡死风险
  已平衡，单并发更稳但更慢）。
- B5/cleaned/unified 补传进行中（§14）；不影响灌图（36 任务不引用它们）。
- Mac 侧原文件、原监督器、原栈全程未动；数据已全量迁移并校验，**Mac 现在可以关机**，
  后续一切在 Windows 上自动推进。
- schtasks 三个任务均为 SYSTEM；不需要时 `schtasks /delete /tn GovKGWatchdog /f` 等。
