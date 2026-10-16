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
CLEAN_ENV := env -i HOME="$$HOME" USER="$$USER" PATH=/usr/bin:/bin

# Wheels only, built for old glibc, so venvs made here also run on COE-HPC3.
PIP_FLAGS := --quiet --disable-pip-version-check --only-binary=:all:
# Bundle libstdc++/libgcc so the binary needs only glibc on COE-HPC3.
CHAMPSIM_LDFLAGS := -static-libstdc++ -static-libgcc

.PHONY: help setup submodules python venvs vcpkg champsim check check-runtime smoke \
        env-record lock clean distclean oe-check-config oe-toy-manual

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

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

oe-check-config: venvs ## Load configs/openevolve/*.yaml with OpenEvolve's loader; flag unknown keys
	$(RUN_ENV) $(VENV_DIR)/controller/bin/python scripts/oe_check_config.py configs/openevolve/*.yaml

# OpenEvolve's own example with llm.manual_mode: each prompt is written to
# $(OUT)/manual_tasks_queue/<id>.json and the run waits for <id>.answer.json.
oe-toy-manual: venvs ## Toy OpenEvolve run with no LLM (OUT=, CHECKPOINT=, ITERATIONS=)
	$(RUN_ENV) $(VENV_DIR)/controller/bin/openevolve-run \
	    examples/function_minimization/initial_program.py examples/function_minimization/evaluator.py \
	    --config configs/openevolve/toy-manual.yaml --output $(OUT) \
	    $(if $(CHECKPOINT),--checkpoint $(CHECKPOINT)) $(if $(ITERATIONS),--iterations $(ITERATIONS))

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
