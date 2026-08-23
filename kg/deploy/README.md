# OpenSPG 全家桶部署（colima + docker compose）

> 部署日期：2026-08-09 ｜ 宿主机：macOS (Apple M1 Max, 32GB) ｜ **架构路线：原生 arm64（aarch64），未使用 Rosetta/amd64**

## 1. 环境清单

| 组件 | 版本 | 安装方式 |
|---|---|---|
| colima | 0.10.3 | `brew install colima` |
| docker CLI | 29.7.2（server 29.5.2） | `brew install docker` |
| docker-compose | 5.4.0 | `brew install docker-compose` |
| VM | Ubuntu 24.04.4 LTS, aarch64, vz + virtiofs | `colima start` |

colima VM 配置：`--arch aarch64 --vm-type vz --cpu 6 --memory 14 --disk 100 --mount /Volumes/f:w`

- docker context 已自动切到 `colima`（socket：`~/.colima/default/docker.sock`）
- **VM 内未配置代理**：四个镜像全部托管在阿里云杭州 registry（`spg-registry.cn-hangzhou.cr.aliyuncs.com/spg/*`），实测直连 0.25s 可达，拉取无需代理
- brew/colima 自身的下载（GitHub releases）走宿主机代理 `http://127.0.0.1:7890`；注意**不要** export `all_proxy=socks5://...` 再跑 `colima start`（socks5 下载大文件会 unexpected EOF 中断），只设 `http_proxy`/`https_proxy` 即可
- brew 装的 docker-compose 插件未被 docker CLI 自动识别，已建软链：`~/.docker/cli-plugins/docker-compose -> /opt/homebrew/bin/docker-compose`

## 2. 镜像与架构验证

四个镜像均提供 **linux/arm64 + linux/amd64 双 manifest**（部署前经 registry API 确认），容器原生运行，无 `exec format error`：

| 镜像 | 大小 |
|---|---|
| openspg-server:latest | 4.31GB（内置 miniconda + `kag` python 环境） |
| openspg-neo4j:latest | 1.01GB |
| openspg-mysql:latest | 538MB |
| openspg-minio:latest | 233MB |

## 3. 相对官方 compose 的本地修改

官方源：<https://github.com/OpenSPG/openspg/blob/master/dev/release/docker-compose.yml>

1. **mysql 宿主机端口 3306 → 13306**：本机 3306 被 podman 的 `mysql-container`（酒店项目，运行中）占用。容器间通信不受影响（仍走 `mysql:3306` 服务名），仅宿主机客户端连接改用 13306。
2. **数据卷全部落盘到 `./volumes/`**（官方只映射了 neo4j logs 且指向 `$HOME/dozerdb/logs`）：
   - `./volumes/mysql/data` → `/var/lib/mysql`
   - `./volumes/neo4j/data` → `/data`，`./volumes/neo4j/logs` → `/logs`
   - `./volumes/minio/data` → `/data`
   - `./volumes/server/logs` → `/home/admin/logs`（server 日志实际走 stdout，用 `docker logs` 看，此目录为空属正常）
3. JVM heap 保持官方值（server `-Xmx8192m`、neo4j heap max 4G），14G VM 实测空闲充裕（稳定运行合计约 3.2G RSS）。

## 4. 常用操作

```bash
cd kg/deploy
docker compose up -d        # 启动
docker compose ps           # 状态
docker compose down         # 停止（数据保留在 volumes/）
docker logs -f release-openspg-server   # 看 server 日志
colima stop                 # 不用时停 VM 释放内存
```

宿主机重启后：`colima start`（参数已记忆，直接 start 即可）→ `docker compose up -d`。

## 5. 服务端点与初始账号（健康检查全部通过，2026-08-09）

| 服务 | 地址 | 账号 | 验证结果 |
|---|---|---|---|
| OpenSPG 产品模式 UI | <http://127.0.0.1:8887> | `openspg` / `openspg@kag` | HTTP 200，日志 `OpenSPG Application Started!!!` |
| neo4j browser | <http://127.0.0.1:7474> | `neo4j` / `neo4j@openspg` | discovery JSON 正常，cypher `RETURN 1` 通过 |
| neo4j bolt | `bolt://127.0.0.1:7687` | 同上 | TCP 连通，server 端 graphStoreUrl 已连上 |
| mysql | `127.0.0.1:13306` | `root` / `openspg`（库 `openspg`） | `SHOW DATABASES` 含 openspg |
| minio API | <http://127.0.0.1:9000> | `minio` / `minio@openspg` | `/minio/health/live` 200 |
| minio console | <http://127.0.0.1:9001> | 同上 | HTTP 200 |

## 6. 已知事项

- podman machine（`podman-machine-default`，跑 mysql:3306 + redis:6379）未做任何改动，与 colima 并存。
- server 镜像内置 KAG python 环境（`/home/admin/miniconda3`，env 名 `kag`），KAG 开发者模式可另用 pip 安装 `kag` 包连本服务的 8887。
- 项目路径含中文（`政务大模型/kg/deploy`），virtiofs 挂载与 compose 均正常工作，未发现路径问题。

## 7. 试点 LLM mock 端点（2026-08-09 试点阶段临时方案）

- `mock_llm.py`：最小 OpenAI 兼容 mock（`/v1/chat/completions` + `/v1/embeddings`，固定响应，embedding 1024 维零向量），监听 `0.0.0.0:18999`（容器与宿主机统一经 `http://192.168.31.80:18999` 访问，en0 IP 随网络变化需同步改 kag 配置），nohup 常驻，日志 `mock_llm.log`。
- 用途：**仅用于通过 `knext project create/update` 的连通性校验**（该命令会实际调用 chat_llm 与 vectorizer，见 `kg/design/kag_notes.md` §4）。试点为纯结构建图，不消费 LLM。
- **正式问答/抽取前必须在 `kag_config.yaml` 把 `chat_llm`/`openie_llm`/`vectorizer` 换成真实模型端点**，否则一切 LLM 相关能力返回的都是 mock 垃圾响应。
- 重启：`cd kg/deploy && nohup python3 mock_llm.py 18999 > mock_llm_stdout.log 2>&1 &`

## 8. UI 密码变更（2026-08-09）

首次登录 8887 强制重置密码，已改为 `openspg` / `openspg@kag2026`（初始密码 openspg@kag 作废）。
