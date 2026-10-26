# AFAR_HPC: reproducible OpenEvolve + ChampSim environment.
#
# Build and test on coe-hpc1 (internet, build modules); submit jobs from g17
# (COE-HPC3). Everything a job needs is built here onto shared storage.
#
#     make setup                  # first-time setup on coe-hpc1 (a few minutes)
#     make smoke TRACE=<path>     # optional seconds-long ChampSim run
#     source env/activate.sh      # site paths + controller venv (either cluster)
#
# Run `make help` for every target.

SHELL := /bin/bash
.SHELLFLAGS := -e -o pipefail -c
.DEFAULT_GOAL := help

include env/python.pin

JOBS  ?= 2
TRACE ?=
# OpenEvolve runs: output directory, optional resume checkpoint and iteration count.
OUT        ?= runs/toy-manual
CHECKPOINT ?=
ITERATIONS ?=
# make llm-fetch: which pinned LLM assets to download, and for which model profile.
LLM_PARTS  ?= sandbox model
PROFILE    ?= qwen2.5-coder-7b-v1
EXP        ?=
# make oe-check: one run spec (a name in configs/runs/); empty checks them all.
SPEC       ?=
# Staged campaigns (M5): C= is a campaign id or directory; EXCLUDE= keeps generation jobs off nodes.
C          ?=
NAME       ?=
EXCLUDE    ?= cs002

CHAMPSIM_DIR    := external/ChampSim
BASELINE_CONFIG := configs/champsim/baseline.json
BASELINE_DIR    := build/baseline
VENV_DIR        := .venv
ROLES           := controller evaluator

PYTHON_DIR := .python
PYTHON     := $(CURDIR)/$(PYTHON_DIR)/bin/python3
PBS_ASSET  := cpython-$(PYTHON_VERSION)+$(PBS_RELEASE)-x86_64-unknown-linux-gnu-install_only.tar.gz
PBS_URL    := https://github.com/astral-sh/python-build-standalone/releases/download/$(PBS_RELEASE)/$(subst +,%2B,$(PBS_ASSET))

# Build and download steps (coe-hpc1 only): compiler, git, Apptainer modules.
BUILD_ENV := source env/modules.sh &&
# Run-time steps (either cluster): shared paths only, no modules.
RUN_ENV := source env/site.sh &&
# A clean environment like a batch job's, for proving run-time steps need no modules.
# AFAR_ASSETS/AFAR_RESULTS pass through when set, as sbatch passes them to jobs.
CLEAN_ENV := env -i HOME="$$HOME" USER="$$USER" PATH=/usr/bin:/bin \
    $${AFAR_ASSETS:+AFAR_ASSETS=$$AFAR_ASSETS} $${AFAR_RESULTS:+AFAR_RESULTS=$$AFAR_RESULTS}

# Wheels only, built for old glibc, so venvs made here also run on COE-HPC3.
# No cache: pip's default ~/.cache/pip grows on the nearly full /home.
PIP_FLAGS := --quiet --disable-pip-version-check --only-binary=:all: --no-cache-dir
# Bundle libstdc++/libgcc so the binary needs only glibc on COE-HPC3.
CHAMPSIM_LDFLAGS := -static-libstdc++ -static-libgcc

.PHONY: help setup submodules python venvs vcpkg champsim check check-runtime smoke \
        env-record lock clean distclean oe-check oe-check-config oe-toy-manual oe-summary oe-compile-selftest \
        evaluator-selftest evaluate-selftest baseline-local llm-fetch traces-fetch \
        campaign-start campaign-status campaign-stop campaign-resume campaign-selftest

help: ## Show this help
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

setup: submodules python venvs vcpkg champsim check ## Full first-time setup (coe-hpc1)

submodules: ## Fetch ChampSim (pinned) and its vcpkg
	$(BUILD_ENV) git submodule update --init --recursive

python: $(PYTHON_DIR)/.installed ## Download the pinned portable CPython into .python/

$(PYTHON_DIR)/.installed: env/python.pin
	rm -rf $(PYTHON_DIR) && mkdir -p $(PYTHON_DIR)
	curl -fsSL --retry 3 -o $(PYTHON_DIR)/python.tar.gz "$(PBS_URL)"
	echo "$(PBS_SHA256)  $(PYTHON_DIR)/python.tar.gz" | sha256sum -c -
	tar -xzf $(PYTHON_DIR)/python.tar.gz -C $(PYTHON_DIR) --strip-components=1
	rm $(PYTHON_DIR)/python.tar.gz
	$(PYTHON) --version
	touch $@

venvs: $(foreach r,$(ROLES),$(VENV_DIR)/$(r)/.installed) ## Create both venvs from the lock files

$(VENV_DIR)/%/.installed: env/%-requirements.txt $(PYTHON_DIR)/.installed
	$(PYTHON) -m venv --clear $(VENV_DIR)/$*
	$(VENV_DIR)/$*/bin/python -m pip install $(PIP_FLAGS) -r $<
	touch $@

vcpkg: ## Install ChampSim's C++ dependencies (vcpkg manifest mode)
	$(BUILD_ENV) cd $(CHAMPSIM_DIR) && { [ -x vcpkg/vcpkg ] || ./vcpkg/bootstrap-vcpkg.sh -disableMetrics; }
	$(BUILD_ENV) cd $(CHAMPSIM_DIR) && ./vcpkg/vcpkg install --clean-after-build

# ChampSim's own Makefile also calls `python3`; point it at the evaluator venv.
champsim: venvs ## Build the baseline ChampSim binary into build/baseline/
	$(BUILD_ENV) export PATH="$(CURDIR)/$(VENV_DIR)/evaluator/bin:$$PATH" && cd $(CHAMPSIM_DIR) && \
	    python config.sh --bindir $(CURDIR)/$(BASELINE_DIR) $(CURDIR)/$(BASELINE_CONFIG)
	$(BUILD_ENV) export PATH="$(CURDIR)/$(VENV_DIR)/evaluator/bin:$$PATH" && \
	    $(MAKE) -C $(CHAMPSIM_DIR) -j$(JOBS) LDFLAGS="$(CHAMPSIM_LDFLAGS)"

check: ## Build checks, then run-time checks in a clean, job-like environment
	$(BUILD_ENV) env/check.sh --build
	$(CLEAN_ENV) bash env/check.sh --runtime

check-runtime: ## Run-time checks only, in a clean environment (also run on g17 or in jobs)
	$(CLEAN_ENV) bash env/check.sh --runtime

smoke: ## Run 10K+10K instructions of TRACE=<path> through the baseline binary
	@[ -n "$(TRACE)" ] || { echo "usage: make smoke TRACE=/path/to/trace.champsimtrace.xz" >&2; exit 2; }
	mkdir -p build
	$(RUN_ENV) $(BASELINE_DIR)/champsim --warmup-instructions 10000 --simulation-instructions 10000 \
	    --json build/smoke.json "$(TRACE)" > build/smoke.log
	$(RUN_ENV) $(VENV_DIR)/evaluator/bin/python -c 'import json; roi = json.load(open("build/smoke.json"))[-1]["roi"]["cores"][0]; \
	    print("smoke ok:", roi["instructions"], "instructions,", roi["cycles"], "cycles")'

# Configs reference ${AFAR_LLM_API_KEY}, which GPU jobs set; any value lets them load here.
oe-check: venvs ## Validate run specs and every profile they name (SPEC=<name>; default all of configs/runs/)
	$(RUN_ENV) $(VENV_DIR)/controller/bin/python -m controller.runspec --selftest
	$(RUN_ENV) for s in $(or $(SPEC),$(basename $(notdir $(wildcard configs/runs/*.yaml)))); do \
	    $(VENV_DIR)/controller/bin/python -m controller.runspec --check $$s || exit 1; done
	$(MAKE) --no-print-directory oe-check-config

oe-check-config: venvs ## Load the hand-written configs/openevolve/*.yaml with OpenEvolve's loader; flag unknown keys
	$(RUN_ENV) AFAR_LLM_API_KEY=validation-only $(VENV_DIR)/controller/bin/python scripts/oe_check_config.py configs/openevolve/*.yaml

# OpenEvolve's own example with llm.manual_mode: each prompt is written to
# $(OUT)/manual_tasks_queue/<id>.json and the run waits for <id>.answer.json.
oe-toy-manual: venvs ## Toy OpenEvolve run with no LLM (OUT=, CHECKPOINT=, ITERATIONS=)
	$(RUN_ENV) $(VENV_DIR)/controller/bin/openevolve-run \
	    examples/function_minimization/initial_program.py examples/function_minimization/evaluator.py \
	    --config configs/openevolve/toy-manual.yaml --output $(OUT) \
	    $(if $(CHECKPOINT),--checkpoint $(CHECKPOINT)) $(if $(ITERATIONS),--iterations $(ITERATIONS))

oe-summary: venvs ## Summarize an OpenEvolve run, one row per iteration (RUN=runs/toy-<jobid>)
	$(VENV_DIR)/controller/bin/python scripts/oe_run_summary.py $(RUN)

oe-compile-selftest: venvs ## Check the R7 compile evaluator on variants of the seed (after make champsim)
	$(BUILD_ENV) $(VENV_DIR)/controller/bin/python evaluator/compile_check.py --selftest

evaluator-selftest: venvs ## Self-test results, manifests, static checks, fitness and calibration rules (seconds)
	$(RUN_ENV) $(VENV_DIR)/evaluator/bin/python -m evaluator.results --selftest
	$(RUN_ENV) $(VENV_DIR)/evaluator/bin/python -m evaluator.manifest --selftest
	$(RUN_ENV) $(VENV_DIR)/evaluator/bin/python -m evaluator.static_checks --selftest
	$(RUN_ENV) $(VENV_DIR)/evaluator/bin/python -m evaluator.fitness --selftest
	$(RUN_ENV) $(VENV_DIR)/evaluator/bin/python -m evaluator.calibrate --selftest

evaluate-selftest: venvs ## Every evaluation status on built and run variants of the seed (a few minutes; Spack gcc 12 here)
	$(BUILD_ENV) $(RUN_ENV) AFAR_CXX=$$(command -v g++) \
	    $(VENV_DIR)/evaluator/bin/python -m evaluator.evaluate_candidate --selftest --jobs $(JOBS)

baseline-local: venvs ## LRU baseline for EXP=<manifest id> on this host; seconds-long manifests only (smoke-tiny-v1)
	@[ -n "$(EXP)" ] || { echo "usage: make baseline-local EXP=smoke-tiny-v1" >&2; exit 2; }
	$(RUN_ENV) $(VENV_DIR)/evaluator/bin/python -m evaluator.run_baseline --manifest configs/experiments/$(EXP).json

campaign-start: venvs ## Start a staged campaign from a run spec with a campaign: section (SPEC=, NAME=, EXCLUDE=; on g17)
	@[ -n "$(SPEC)" ] || { echo "usage: make campaign-start SPEC=m5-dev-v1 [NAME=<id>]" >&2; exit 2; }
	$(RUN_ENV) $(VENV_DIR)/controller/bin/python -m controller.campaign start $(SPEC) $(if $(NAME),--name $(NAME)) --exclude "$(EXCLUDE)"

campaign-status: venvs ## A campaign's state, generations and best fitness (C=<id>)
	$(RUN_ENV) $(VENV_DIR)/controller/bin/python -m controller.campaign status $(C)

campaign-stop: venvs ## Halt a campaign after its current stage (C=<id>)
	$(RUN_ENV) $(VENV_DIR)/controller/bin/python -m controller.campaign stop $(C)

campaign-resume: venvs ## Resubmit a campaign's current stage, after a halt or a cancelled job (C=<id>; on g17)
	$(RUN_ENV) $(VENV_DIR)/controller/bin/python -m controller.campaign resume $(C)

campaign-selftest: venvs ## The staged pipeline end to end on this host: fake LLM, local executor, injected faults (~2 min)
	$(RUN_ENV) $(VENV_DIR)/controller/bin/python -m controller.oe_adapter --selftest
	$(BUILD_ENV) $(RUN_ENV) AFAR_CXX=$$(command -v g++) $(VENV_DIR)/controller/bin/python -m controller.campaign --selftest

llm-fetch: venvs ## Download a model profile's weights and unpack the vLLM sandbox into AFAR_ASSETS (PROFILE=, LLM_PARTS=; needs internet)
	$(BUILD_ENV) PROFILE=$(PROFILE) scripts/llm_fetch.sh $(LLM_PARTS)

traces-fetch: venvs ## Download the 19 CRC-2 traces ArchAgent used into AFAR_ASSETS and pin them (~28 GB zip; needs internet)
	$(RUN_ENV) nice -n 19 $(VENV_DIR)/evaluator/bin/python -I scripts/traces_fetch_crc2.py

env-record: ## Write env/modules.lock.txt and env/versions.txt
	$(BUILD_ENV) module -t list > env/modules.lock.txt 2>&1
	$(BUILD_ENV) env/record-versions.sh > env/versions.txt

lock: python ## Re-resolve env/*-requirements.in into the pinned *.txt lock files (needs internet)
	for role in $(ROLES); do PYTHON="$(PYTHON)" PIP_FLAGS="$(PIP_FLAGS)" env/lock.sh $$role; done

clean: ## Remove ChampSim build outputs and build/
	-$(BUILD_ENV) $(MAKE) -C $(CHAMPSIM_DIR) clean
	rm -rf build

distclean: clean ## Also remove .python/, the venvs and vcpkg_installed/
	rm -rf $(PYTHON_DIR) $(VENV_DIR) $(CHAMPSIM_DIR)/vcpkg_installed
