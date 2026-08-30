# 政务大模型项目 Mac → Windows 迁移报告（2026-08-30 夜）

目标：Mac（colima）上的 OpenSPG 灌图迁移到 Windows（mywin-zihang，Docker Desktop），
在 Windows 上全新起栈、全新灌图，无人值守继续跑；Mac 本地仓库与正在运行的导入监督器不动。

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
