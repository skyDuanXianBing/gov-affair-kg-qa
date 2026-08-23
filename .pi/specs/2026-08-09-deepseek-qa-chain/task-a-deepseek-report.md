# 任务 A 报告 — GovAffair DeepSeek chat 端点接线

> 执行：impl_green_coder · 2026-08-09 19:0x
> 依据：`.pi/specs/2026-08-09-deepseek-qa-chain/需求规格说明书.md` §4 任务 A（A1–A4）；`HANDOFF.md` §三待办1、§五

## 结论

✅ 任务 A 完成：A1–A4 全部通过。`chat_llm` 已从 mock_llm 切换为真实 DeepSeek API（`deepseek-v4-flash`），`knext project update` 成功，KAG maas 客户端冒烟返回正常中文。

## 关键波折：两个 key 的问题（已解决）

- 首次直连 `/models` 返回 **HTTP 401**：`{"error":{"message":"Authentication Fails, Your api key: ****df14 is invalid",...}}`。
- 根因（主代理定位）：`~/.zprofile:6` 是**旧 key**（末4位 df14，服务端已判无效）；`~/.zshrc:44` 是用户 2026-08-09 18:37 更换的**新 key**（末4位 fd71）。`zsh -lc` 非交互不加载 `.zshrc`，故初次取到旧 key。
- 处置：改用 `export deepseekapi=$(sed -n 's/^export deepseekapi="\(.*\)"/\1/p' ~/.zshrc | tail -1)` 取新 key（核验末4位 fd71）后继续。**遗留**：`~/.zprofile:6` 的旧 key 未动（主代理指示由 Lead 与用户处理），交互式 zsh 若 `.zprofile` 后加载可能覆盖 `.zshrc`，建议用户尽快同步。

## 各步证据

### 步骤 1 — /models（A1 ✅）

```
$ export deepseekapi=$(sed -n 's/^export deepseekapi="\(.*\)"/\1/p' ~/.zshrc | tail -1)
$ env -u https_proxy -u http_proxy -u all_proxy curl -sS https://api.deepseek.com/models \
    -H "Authorization: Bearer ${deepseekapi}"
HTTP 200
{"object":"list","data":[
  {"id":"deepseek-v4-flash","object":"model","owned_by":"deepseek"},
  {"id":"deepseek-v4-pro","object":"model","owned_by":"deepseek"}]}
```

**所选 model id：`deepseek-v4-flash`**。理由：HANDOFF §三待办1 与规格书 §2.1 均要求优先 flash 变体；`/models` 实际返回仅 `deepseek-v4-flash` 与 `deepseek-v4-pro` 两个 id，flash 为其中低成本/低时延变体，适合冒烟与问答链路验证，真实计费更省。

### 步骤 2 — 最小 /chat/completions 验证 key 可用

```
$ curl -sS https://api.deepseek.com/chat/completions -H "Authorization: Bearer ${deepseekapi}" \
    -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"用一句中文回答：办理居住证一般需要什么材料？"}],"max_tokens":50}'
HTTP 200
content: "一般需要本人身份证、近期照片、居住地址证明"
usage: prompt_tokens=96, completion_tokens=50, total=146（finish_reason=length，按预期被 max_tokens 截断）
```

key 真实可用，计费消耗 146 tokens。

### 步骤 3 — 编辑 kag_config.yaml（A2 ✅）

改动仅限文件头注释与 `chat_llm` 段（openie_llm / vectorizer / project / chain_vectorizer 均未动）：

```yaml
# 头部注释（替换原"key 无效"过时描述）：
# GovAffair 项目配置（v0.2 更新）
# vectorizer: ollama bge-m3 真实端点（本机 11434，容器经 en0 IP 可达）
# chat_llm: 2026-08-09 接入真实 DeepSeek API（model: deepseek-v4-flash，flash 变体，
#   直连 api.deepseek.com 不走代理；key 取自环境变量 deepseekapi，末4位 fd71，来源 ~/.zshrc:44）
# openie_llm: 保持 mock 不动（结构化建图路线，builder 不做 LLM 抽取）

chat_llm:
  type: maas
  base_url: https://api.deepseek.com
  api_key: ****fd71        # yaml 内为明文 key（规格书 A2 允许），报告中脱敏
  model: deepseek-v4-flash
  enable_check: true       # 由 false 调为 true：真实端点应通过连通性校验（规格书 A3 建议）
```

key 通过 bash 从 `~/.zshrc` 注入，未在工具调用/报告中出现明文。

### 步骤 4 — knext project update（A3 ✅）

```
$ cd kg/pilot/GovAffair
$ env -u https_proxy -u http_proxy -u all_proxy ../../venv/bin/knext project update --proj_path ./kag_config.yaml
2026-08-09 18:59:33 - INFO - knext.command.sub_command.project - project id: 1
Project [GovAffair] with namespace [GovAffair] was successfully updated from [./kag_config.yaml].
```

注意：本仓库 knext 版本参数为 `--proj_path`（任务单中的 `--config_path` 不存在，已按 `knext project update --help` 实测调整）。`enable_check: true` 下 update 直接通过，说明 DeepSeek 端点连通性校验成功。

### 步骤 5 — 冒烟（A4 ✅）

新增脚本 `kg/pilot/smoke_deepseek_chat.py`（加载 `GovAffair/kag_config.yaml` 的 chat_llm 配置，经 `kag.interface.common.llm_client.LLMClient.from_config` 构造 maas 客户端，清除代理 env 后发一次真实 completion）：

```
$ cd kg/pilot && ../venv/bin/python smoke_deepseek_chat.py
[smoke] chat_llm type=maas base_url=https://api.deepseek.com model=deepseek-v4-flash
[smoke] Q: 用一句中文回答：办理居住证一般需要什么材料？
[smoke] A: 办理居住证一般需要本人身份证、居住证明（如租房合同或房产证）、就业或就读证明，以及近期照片等材料。
[smoke] PASS
```

返回正常中文，确认 KAG 侧 chat_llm 端到端真实可用（非 mock）。

## 计费消耗

共 3 次真实调用（/models 不计费另计）：最小 completion 146 tokens + enable_check 1 次 + 冒烟 1 次，均在 max_tokens≤50 限制内，符合"冒烟次数个位数"约束。

## Changed files

| 文件 | 改动 |
|---|---|
| `kg/pilot/GovAffair/kag_config.yaml` | 仅文件头注释 + `chat_llm` 段（type/base_url/api_key/model/enable_check），其余段未动 |
| `kg/pilot/smoke_deepseek_chat.py` | 新增冒烟脚本（按任务允许范围留在 `kg/pilot/`） |

## 范围遵守

- 未动 openie_llm / vectorizer / project / chain_vectorizer 段；未动 docker/Neo4j/MySQL 数据；未动 `data/`；未安装依赖；未执行任何 git 提交/暂存（工作区无 staged 文件）。
- 全程未在报告/注释中写入完整 key（仅末4位）。

## 遗留风险

1. **`~/.zprofile:6` 旧 key（末4位 df14）残留**：交互 shell 若后加载 `.zprofile` 会拿到无效 key，后续依赖环境变量 `deepseekapi` 的流程可能踩坑。建议用户/Lead 同步两处（本任务按主代理指示未修改该文件）。
2. **yaml 内明文 key**：规格书 A2 允许，但若 `kag_config.yaml` 未来入 git 需先做脱敏/外置处理。
3. **enable_check=true**：意味着 knext/kag 初始化时会对 DeepSeek 发起校验调用（小额计费）；如需零校验可改回 false。
4. 冒烟脚本 `smoke_deepseek_chat.py` 含明文读取 yaml 的逻辑，无独立风险，可留作回归用。

## 建议 QA 重点

- 复跑 `../venv/bin/python smoke_deepseek_chat.py` 确认可重复。
- 任务 B（solver 链路）启动前确认其读取的是同一 `kag_config.yaml`，并留意 openie_llm 仍为 mock（若 solver 组件依赖 openie_llm，按规格书 §2.2 须停下回报）。
