#!/usr/bin/env bash
# Download the pinned LLM assets (configs/llm/serve.env) into $AFAR_ASSETS.
#
# Usage: make llm-fetch [LLM_PARTS="sandbox model"]
#        (coe-hpc1 only: needs internet and the apptainer module)
#
# Safe to re-run: finished assets are skipped. Each asset is written under a
# temporary name first, so an interrupted download never looks complete.
set -euo pipefail
cd "$(dirname "$0")/.."
source env/site.sh
source configs/llm/serve.env

containers="${AFAR_ASSETS}/containers"
models="${AFAR_ASSETS}/models"
mkdir -p "$containers" "$models" "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR" "$HF_HOME"

# Container, step 1 of 2: unpack the digest-pinned image into a sandbox
# directory. This skips coe-hpc1's mksquashfs, which deadlocked packing this
# image into a .sif; slurm/build_sif.sbatch does the packing on HPC3 instead.
# The timeout is a watchdog: a hung step fails instead of running for hours.
fetch_sandbox() {
    local sif="${containers}/${VLLM_SIF}" sandbox="${containers}/${VLLM_SANDBOX}"
    if [[ -f "$sif" ]]; then
        echo "container: ${sif} (present)"
        return
    fi
    if [[ -f "${sandbox}.complete" ]]; then
        echo "sandbox: ${sandbox} (present)"
    else
        echo "sandbox: unpacking ${VLLM_IMAGE}"
        rm -rf "${sandbox}.partial"
        timeout 3h nice -n 19 apptainer build --sandbox --fix-perms "${sandbox}.partial" "$VLLM_IMAGE"
        mv "${sandbox}.partial" "$sandbox"
        echo "$VLLM_IMAGE" > "${sandbox}.complete"
    fi
    echo "sandbox ready; next, on g17: sbatch slurm/build_sif.sbatch"
}

# Model: weights at the pinned revision into their own directory.
fetch_model() {
    local model_dir="${models}/${MODEL_DIR}"
    if [[ -f "${model_dir}/.complete" ]]; then
        echo "model: ${model_dir} (present)"
    else
        echo "model: downloading ${MODEL_ID}@${MODEL_REVISION}"
        .venv/controller/bin/hf download "$MODEL_ID" --revision "$MODEL_REVISION" --local-dir "$model_dir"
        echo "${MODEL_ID}@${MODEL_REVISION}" > "${model_dir}/.complete"
    fi
    du -sh "$model_dir"
}

for part in "${@:-sandbox model}"; do
    for p in $part; do
        case "$p" in
            sandbox) fetch_sandbox ;;
            model)   fetch_model ;;
            *) echo "llm_fetch.sh: unknown part '$p' (use: sandbox, model)" >&2; exit 2 ;;
        esac
    done
done
