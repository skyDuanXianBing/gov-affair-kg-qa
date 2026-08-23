#!/usr/bin/env bash
# 按“pilot 全量 -> personal 全量”的顺序执行 OpenSPG CSV 分片导入。
# 默认只做 dry-run；必须显式传入 --execute 才会上传并提交 Builder Job。

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly DEFAULT_RUN_ID="full-import"
readonly PILOT_MANIFEST="schema/openspg_import_manifest.json"
readonly PILOT_SHARD_MANIFEST="build/openspg-shards-16mib-model-v2/pilot/shard_manifest.json"
readonly PERSONAL_MANIFEST="schema/openspg_personal_import_manifest.json"
readonly PERSONAL_SHARD_MANIFEST="build/openspg-shards-16mib-model-v2/personal/shard_manifest.json"
readonly EXPECTED_TARGET_BYTES=16777216

if [[ -n "${PYTHON_BIN:-}" ]]; then
    PYTHON_BIN="${PYTHON_BIN}"
elif [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
else
    PYTHON_BIN="python3"
fi
RUN_ID="${OPENSPG_IMPORT_RUN_ID:-${DEFAULT_RUN_ID}}"
POLL_INTERVAL="${OPENSPG_POLL_INTERVAL:-5}"
JOB_TIMEOUT="${OPENSPG_JOB_TIMEOUT:-14400}"
UPLOAD_TIMEOUT="${OPENSPG_UPLOAD_TIMEOUT:-3600}"
RETRIES="${OPENSPG_IMPORT_RETRIES:-3}"
RETRY_BACKOFF="${OPENSPG_RETRY_BACKOFF:-2}"
EXECUTE=false
NO_PROGRESS=false
DELETE_SUCCESSFUL_SHARDS=false
VALIDATE_ONLY=false
CURRENT_DATASET="初始化"

usage() {
    cat <<'USAGE'
用法：
  ./scripts/run_full_openspg_import.sh [选项]

默认行为：
  对 pilot 和 personal 两套 16 MiB 全量分片执行 dry-run，不上传、不提交 Builder Job。

选项：
  --execute                    真实上传并提交，逐分片等待 Builder Job 成功
  --run-id ID                  状态和报告目录名称，默认 full-import
  --no-progress                关闭 tqdm 进度条
  --delete-successful-shards   成功后删除本地分片，必须和 --execute 一起使用
  --validate-only              只检查虚拟环境和 16 MiB 分片，不读取或上传 CSV
  -h, --help                   显示帮助

必需环境变量：
  OPENSPG_BASE_URL             OpenSPG HTTP API 地址

可选环境变量：
  OPENSPG_TOKEN                OpenSPG Token
  OPENSPG_PROJECT_ID           覆盖 manifest 中的项目 ID
  OPENSPG_NAMESPACE            覆盖 manifest 中的 namespace
  OPENSPG_IMPORT_RUN_ID        等价于 --run-id
  OPENSPG_POLL_INTERVAL        Builder 状态轮询间隔，默认 5 秒
  OPENSPG_JOB_TIMEOUT          单个 Builder Job 超时，默认 14400 秒
  OPENSPG_UPLOAD_TIMEOUT       单个分片上传超时，默认 3600 秒
  OPENSPG_IMPORT_RETRIES       失败重试次数，默认 3
  OPENSPG_RETRY_BACKOFF        重试退避秒数，默认 2
  PYTHON_BIN                   覆盖 Python 命令；默认优先使用项目 .venv/bin/python

示例：
  # 安全预检
  export OPENSPG_BASE_URL='你的 OpenSPG API 地址'
  ./scripts/run_full_openspg_import.sh

  # 正式导入
  ./scripts/run_full_openspg_import.sh --execute

  # 使用指定 run-id，便于中断后原命令续传
  ./scripts/run_full_openspg_import.sh --execute --run-id full-import-16mib-20260817
USAGE
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

validate_shard_manifest() {
    local manifest_path="$1"
    local source_manifest_path="$2"
    "${PYTHON_BIN}" - "${manifest_path}" "${source_manifest_path}" "${EXPECTED_TARGET_BYTES}" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
source_manifest_path = Path(sys.argv[2])
expected_target_bytes = int(sys.argv[3])
data = json.loads(manifest_path.read_text(encoding="utf-8"))
source_data = json.loads(source_manifest_path.read_text(encoding="utf-8"))
actual_target_bytes = int(data.get("target_bytes") or 0)
if int(source_data.get("version") or 0) != 3:
    raise SystemExit(f"ERROR: {source_manifest_path} 不是模型 v3 manifest")
if actual_target_bytes != expected_target_bytes:
    raise SystemExit(
        f"ERROR: {manifest_path} target_bytes={actual_target_bytes}，"
        f"预期为 {expected_target_bytes}（16 MiB）"
    )

expected_job_keys = [str(job["key"]) for job in source_data.get("jobs", [])]
actual_job_keys = [str(job["key"]) for job in data.get("jobs", [])]
if actual_job_keys != expected_job_keys:
    missing_job_keys = [key for key in expected_job_keys if key not in actual_job_keys]
    unexpected_job_keys = [key for key in actual_job_keys if key not in expected_job_keys]
    raise SystemExit(
        f"ERROR: {manifest_path} 任务清单不完整；"
        f"expected={len(expected_job_keys)} actual={len(actual_job_keys)} "
        f"missing={missing_job_keys} unexpected={unexpected_job_keys}"
    )

missing_files: list[str] = []
size_mismatches: list[str] = []
part_count = 0
row_count = 0
for job in data.get("jobs", []):
    for part in job.get("parts", []):
        part_count += 1
        row_count += int(part.get("row_count") or 0)
        part_path = Path(part["part_file"])
        if not part_path.is_file():
            missing_files.append(str(part_path))
            continue
        expected_size = int(part.get("bytes") or 0)
        if part_path.stat().st_size != expected_size:
            size_mismatches.append(str(part_path))

if missing_files:
    raise SystemExit(f"ERROR: {manifest_path} 缺少 {len(missing_files)} 个分片文件")
if size_mismatches:
    raise SystemExit(f"ERROR: {manifest_path} 有 {len(size_mismatches)} 个分片大小不匹配")
print(
    f"分片检查通过：{manifest_path}，jobs={len(data.get('jobs', []))}，"
    f"parts={part_count}，rows={row_count}，target=16 MiB"
)
PY
}

on_error() {
    local exit_code=$?
    printf '\n导入在数据集 %s 阶段失败，退出码：%s\n' "${CURRENT_DATASET}" "${exit_code}" >&2
    printf '修复问题后使用相同 run-id 重跑即可断点续传：%s\n' "${RUN_ID}" >&2
    exit "${exit_code}"
}

trap on_error ERR

while (($# > 0)); do
    case "$1" in
        --execute)
            EXECUTE=true
            shift
            ;;
        --run-id)
            (($# >= 2)) || fail "--run-id 缺少参数"
            RUN_ID="$2"
            shift 2
            ;;
        --no-progress)
            NO_PROGRESS=true
            shift
            ;;
        --delete-successful-shards)
            DELETE_SUCCESSFUL_SHARDS=true
            shift
            ;;
        --validate-only)
            VALIDATE_ONLY=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "未知参数：$1；使用 --help 查看用法"
            ;;
    esac
done

[[ "${RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]] || fail "run-id 只能包含字母、数字、点、下划线和连字符"
[[ -n "${OPENSPG_BASE_URL:-}" ]] || fail "请先设置 OPENSPG_BASE_URL"
if [[ "${PYTHON_BIN}" == */* ]]; then
    [[ -x "${PYTHON_BIN}" ]] || fail "Python 不存在或不可执行：${PYTHON_BIN}"
else
    command -v "${PYTHON_BIN}" >/dev/null 2>&1 || fail "找不到 Python 命令：${PYTHON_BIN}"
fi

if [[ "${DELETE_SUCCESSFUL_SHARDS}" == true && "${EXECUTE}" != true ]]; then
    fail "--delete-successful-shards 必须和 --execute 一起使用"
fi

cd "${ROOT_DIR}"

for required_file in \
    "requirements-import.txt" \
    "scripts/import_openspg_csvs.py" \
    "${PILOT_MANIFEST}" \
    "${PILOT_SHARD_MANIFEST}" \
    "${PERSONAL_MANIFEST}" \
    "${PERSONAL_SHARD_MANIFEST}"; do
    [[ -f "${required_file}" ]] || fail "缺少文件：${required_file}"
done

if [[ "${NO_PROGRESS}" != true ]]; then
    "${PYTHON_BIN}" -c 'import tqdm' >/dev/null 2>&1 || fail \
        "缺少 tqdm，请先执行：${PYTHON_BIN} -m pip install -r requirements-import.txt"
fi

validate_shard_manifest "${PILOT_SHARD_MANIFEST}" "${PILOT_MANIFEST}"
validate_shard_manifest "${PERSONAL_SHARD_MANIFEST}" "${PERSONAL_MANIFEST}"

if [[ "${VALIDATE_ONLY}" == true ]]; then
    printf '虚拟环境和两套 16 MiB 分片检查全部通过。\n'
    exit 0
fi

readonly RUN_ROOT="build/import-runs/${RUN_ID}"
mkdir -p "${RUN_ROOT}"

COMMON_ARGS=(
    --all
    --mode clone
    --poll-interval "${POLL_INTERVAL}"
    --timeout "${JOB_TIMEOUT}"
    --upload-timeout "${UPLOAD_TIMEOUT}"
    --retries "${RETRIES}"
    --retry-backoff "${RETRY_BACKOFF}"
)

if [[ "${EXECUTE}" == true ]]; then
    COMMON_ARGS+=(--execute --wait)
fi
if [[ "${NO_PROGRESS}" == true ]]; then
    COMMON_ARGS+=(--no-progress)
fi
if [[ "${DELETE_SUCCESSFUL_SHARDS}" == true ]]; then
    COMMON_ARGS+=(--delete-successful-shards)
fi

run_dataset() {
    local dataset_name="$1"
    local manifest_path="$2"
    local shard_manifest_path="$3"
    local state_file="${RUN_ROOT}/${dataset_name}-state.json"
    local preview_dir="${RUN_ROOT}/${dataset_name}"

    CURRENT_DATASET="${dataset_name}"
    printf '\n========== 开始处理 %s 全量数据 ==========\n' "${dataset_name}"
    printf 'manifest: %s\n' "${manifest_path}"
    printf 'shards:   %s\n' "${shard_manifest_path}"
    printf 'state:    %s\n' "${state_file}"
    printf 'report:   %s/import-report.json\n\n' "${preview_dir}"

    "${PYTHON_BIN}" scripts/import_openspg_csvs.py \
        --manifest "${manifest_path}" \
        --shard-manifest "${shard_manifest_path}" \
        --state-file "${state_file}" \
        --preview-dir "${preview_dir}" \
        --name-suffix="-${dataset_name}-${RUN_ID}" \
        "${COMMON_ARGS[@]}"

    printf '========== %s 处理完成 ==========\n' "${dataset_name}"
}

printf 'OpenSPG CSV 全量导入（16 MiB 分片）\n'
printf '运行模式：%s\n' "$([[ "${EXECUTE}" == true ]] && printf 'EXECUTE' || printf 'DRY_RUN')"
printf 'Python：%s\n' "${PYTHON_BIN}"
printf 'run-id：%s\n' "${RUN_ID}"
printf '输出目录：%s\n' "${RUN_ROOT}"
printf '数据顺序：pilot -> personal\n'

run_dataset "pilot" "${PILOT_MANIFEST}" "${PILOT_SHARD_MANIFEST}"
run_dataset "personal" "${PERSONAL_MANIFEST}" "${PERSONAL_SHARD_MANIFEST}"

CURRENT_DATASET="完成"
printf '\n========== pilot 与 personal 两套全量数据处理完成 ==========\n'
printf '报告目录：%s\n' "${RUN_ROOT}"
if [[ "${EXECUTE}" != true ]]; then
    printf '本次为 dry-run，未上传文件、未提交 Builder Job。\n'
    printf '确认报告后，执行：./scripts/run_full_openspg_import.sh --execute --run-id %s\n' "${RUN_ID}"
fi
