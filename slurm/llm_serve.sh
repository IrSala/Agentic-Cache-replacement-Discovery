# Run the pinned vLLM server as a sidecar inside a GPU job.
#
# Source after env/site.sh, configs/llm/vllm.env and a model profile
# (configs/models/<name>.env), from the repo root:
#     source slurm/llm_serve.sh
#     llm_start
#     llm_wait_ready 900
#     ... clients use $AFAR_LLM_API_BASE with the key in $AFAR_LLM_API_KEY ...
# The server is stopped when the job script exits (trap on EXIT and TERM).
#
# The server listens on 127.0.0.1 only, on a free port derived from the job ID,
# and requires a random per-job API key. The key travels through the
# environment (APPTAINERENV_VLLM_API_KEY), never on a command line, because
# command lines are visible to every user on a shared node. vLLM's key covers
# the /v1 endpoints only (vLLM security docs), so the server also lives only as
# long as the job.
#
# The fake-v1 profile (LLM_SERVER=fake), or AFAR_LLM_FAKE=1 with any profile,
# starts scripts/fake_llm_server.py instead of vLLM, so a job script can be
# tested on coe-hpc1 without a GPU. AFAR_LLM_FAKE_FAIL=1 makes
# that stand-in die at start-up; AFAR_LLM_FAKE_DELAY=<s> makes it load slowly;
# AFAR_LLM_FAKE_REPLY_DIR=<dir> cycles through that directory's files (staged
# campaign tests); AFAR_LLM_FAKE_REPLY=<file> makes it answer with that file's text, e.g. a
# reply recorded from a real run.

_llm_py=".venv/controller/bin/python"

llm_is_fake() {
    [[ "${AFAR_LLM_FAKE:-0}" == 1 || "${LLM_SERVER:-vllm}" == fake ]]
}

# Number GPUs by PCI bus inside the container, as nvidia-smi and Slurm do
# (CUDA's default order is fastest-first), so logs and CUDA agree on "GPU 0".
export APPTAINERENV_CUDA_DEVICE_ORDER=PCI_BUS_ID

# Check the allocated GPU before starting the server: log its health and run the
# CUDA memory query vLLM starts with. Fails in seconds on an unusable GPU, e.g.
# cs002 GPU 0, which had uncorrectable ECC errors (docs/llm-serving.md, R5).
llm_gpu_preflight() {
    if llm_is_fake; then
        return 0
    fi
    local gpu="${CUDA_VISIBLE_DEVICES:?no GPU allocated to this job}"
    local sif="${AFAR_ASSETS}/containers/${VLLM_SIF}"
    echo "gpu preflight: node=$(hostname -s) CUDA_VISIBLE_DEVICES=${gpu}"
    nvidia-smi --query-gpu=index,uuid,pci.bus_id --format=csv,noheader -i "$gpu"
    nvidia-smi -q -i "$gpu" | grep -E 'Compute Mode|Volatile|Uncorr|Remap|Pending|Failure|Recovery' || true
    echo "gpu preflight: compute processes on this node's GPUs:"
    nvidia-smi --query-compute-apps=gpu_bus_id,pid,used_memory --format=csv
    if ! apptainer exec --nv "$sif" python3 -c '
import torch
free, total = torch.cuda.mem_get_info()
props = torch.cuda.get_device_properties(0)
uuid = getattr(props, "uuid", "unknown")
print(f"gpu preflight: CUDA ok on {props.name} (uuid GPU-{uuid}): {free / 2**30:.1f} of {total / 2**30:.1f} GiB free")
'; then
        echo "gpu preflight: FAILED: GPU ${gpu} on $(hostname -s) is unusable (error above)." >&2
        echo "  Resubmit avoiding this node:  sbatch --exclude=$(hostname -s) <job script>" >&2
        echo "  and report the GPU to HPC support (see docs/llm-serving.md, R5)." >&2
        return 1
    fi
}

# Print a free 127.0.0.1 port near 20000 + job ID.
llm_pick_port() {
    local base=$(( 20000 + ${SLURM_JOB_ID:?} % 20000 )) p
    for p in $(seq "$base" $(( base + 19 ))); do
        if "$_llm_py" -c 'import socket, sys; s = socket.socket(); s.bind(("127.0.0.1", int(sys.argv[1]))); s.close()' "$p" 2>/dev/null; then
            echo "$p"
            return 0
        fi
    done
    echo "llm_pick_port: no free port in ${base}-$(( base + 19 ))" >&2
    return 1
}

# Start the server in the background; export AFAR_LLM_PORT, _API_BASE, _API_KEY.
llm_start() {
    AFAR_LLM_PORT="$(llm_pick_port)" || return 1
    AFAR_LLM_API_KEY="$("$_llm_py" -c 'import secrets; print(secrets.token_hex(16))')"
    AFAR_LLM_API_BASE="http://127.0.0.1:${AFAR_LLM_PORT}/v1"
    AFAR_LLM_LOG="logs/vllm-${SLURM_JOB_ID}.log"
    export AFAR_LLM_PORT AFAR_LLM_API_KEY AFAR_LLM_API_BASE

    # Node-local caches, private to this user (other users share the node).
    local cache="/tmp/${USER}/afar-llm"
    mkdir -p "$cache"
    chmod 700 "/tmp/${USER}" "$cache"

    if llm_is_fake; then
        VLLM_API_KEY="$AFAR_LLM_API_KEY" "$_llm_py" scripts/fake_llm_server.py \
            --port "$AFAR_LLM_PORT" --served-model-name "$SERVED_MODEL_NAME" \
            ${AFAR_LLM_FAKE_FAIL:+--exit-immediately} --startup-delay "${AFAR_LLM_FAKE_DELAY:-0}" \
            ${AFAR_LLM_FAKE_REPLY:+--reply-file "$AFAR_LLM_FAKE_REPLY"} \
            ${AFAR_LLM_FAKE_REPLY_DIR:+--reply-dir "$AFAR_LLM_FAKE_REPLY_DIR"} \
            > "$AFAR_LLM_LOG" 2>&1 &
    else
        local sif="${AFAR_ASSETS}/containers/${VLLM_SIF}"
        local model="${AFAR_ASSETS}/models/${MODEL_DIR}"
        export APPTAINERENV_VLLM_API_KEY="$AFAR_LLM_API_KEY"
        export APPTAINERENV_HF_HUB_OFFLINE=1
        export APPTAINERENV_VLLM_NO_USAGE_STATS=1
        export APPTAINERENV_DO_NOT_TRACK=1
        export APPTAINERENV_VLLM_CACHE_ROOT="${cache}/vllm"
        export APPTAINERENV_XDG_CACHE_HOME="${cache}/xdg"
        export APPTAINERENV_HF_HOME="${cache}/hf"
        # VLLM_SERVE_ARGS holds several flags, so it is intentionally unquoted.
        apptainer exec --nv --bind "${model}:/model:ro" "$sif" \
            vllm serve /model --served-model-name "$SERVED_MODEL_NAME" \
            --host 127.0.0.1 --port "$AFAR_LLM_PORT" ${VLLM_SERVE_ARGS} > "$AFAR_LLM_LOG" 2>&1 &
    fi
    AFAR_LLM_PID=$!
    trap llm_stop EXIT
    trap 'echo "llm: received SIGTERM"; exit 143' TERM
    echo "llm: started pid=${AFAR_LLM_PID} api_base=${AFAR_LLM_API_BASE} log=${AFAR_LLM_LOG}"
}

# Wait until /health answers; fail early if the server dies. $1 = limit in seconds.
llm_wait_ready() {
    local limit="${1:-900}" start=$SECONDS
    until curl -sf "http://127.0.0.1:${AFAR_LLM_PORT}/health" > /dev/null; do
        if ! kill -0 "$AFAR_LLM_PID" 2>/dev/null; then
            echo "llm: server exited during start-up; last 50 log lines:" >&2
            tail -50 "$AFAR_LLM_LOG" >&2
            return 1
        fi
        if (( SECONDS - start >= limit )); then
            echo "llm: not healthy after ${limit}s; last 50 log lines:" >&2
            tail -50 "$AFAR_LLM_LOG" >&2
            return 1
        fi
        sleep 5
    done
    AFAR_LLM_READY_SECONDS=$(( SECONDS - start ))
    echo "llm: healthy after ${AFAR_LLM_READY_SECONDS}s"
}

# Stop the server: SIGTERM, up to 30 s to exit, then SIGKILL.
llm_stop() {
    if [[ -z "${AFAR_LLM_PID:-}" ]] || ! kill -0 "$AFAR_LLM_PID" 2>/dev/null; then
        return 0
    fi
    echo "llm: stopping server (pid ${AFAR_LLM_PID})"
    kill -TERM "$AFAR_LLM_PID" 2>/dev/null || true
    local i
    for i in $(seq 30); do
        if ! kill -0 "$AFAR_LLM_PID" 2>/dev/null; then
            echo "llm: stopped"
            return 0
        fi
        sleep 1
    done
    kill -KILL "$AFAR_LLM_PID" 2>/dev/null || true
    echo "llm: killed after 30s"
}
