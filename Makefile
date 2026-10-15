# AFAR_HPC: reproducible OpenEvolve + ChampSim environment.
#
#     make setup                  # first-time setup (about 15 min)
#     make smoke TRACE=<path>     # optional seconds-long ChampSim run
#     source env/activate.sh      # modules + controller venv in your shell
#
# Run `make help` for every target.

SHELL := /bin/bash
.SHELLFLAGS := -e -o pipefail -c
.DEFAULT_GOAL := help

JOBS  ?= 2
TRACE ?=

CHAMPSIM_DIR    := external/ChampSim
BASELINE_CONFIG := configs/champsim/baseline.json
BASELINE_DIR    := build/baseline
VENV_DIR        := .venv
ROLES           := controller evaluator

# Every recipe line that needs the toolchain starts with this.
MODULES := source env/modules.sh &&

# Wheels only: CentOS 7 (glibc 2.17) cannot build or use the newest numpy etc.
PIP_FLAGS := --quiet --disable-pip-version-check --only-binary=:all:

.PHONY: help setup submodules venvs vcpkg champsim check smoke env-record \
        lock modules-save clean distclean

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

setup: submodules venvs vcpkg champsim check ## Full first-time setup

submodules: ## Fetch ChampSim (pinned) and its vcpkg
	$(MODULES) git submodule update --init --recursive

venvs: $(foreach r,$(ROLES),$(VENV_DIR)/$(r)/.installed) ## Create both venvs from the lock files

$(VENV_DIR)/%/.installed: env/%-requirements.txt
	$(MODULES) python3 -m venv --clear $(VENV_DIR)/$*
	$(MODULES) $(VENV_DIR)/$*/bin/python -m pip install $(PIP_FLAGS) -r $<
	touch $@

vcpkg: ## Install ChampSim's C++ dependencies (vcpkg manifest mode)
	$(MODULES) cd $(CHAMPSIM_DIR) && { [ -x vcpkg/vcpkg ] || ./vcpkg/bootstrap-vcpkg.sh -disableMetrics; }
	$(MODULES) cd $(CHAMPSIM_DIR) && ./vcpkg/vcpkg install --clean-after-build

champsim: venvs ## Build the baseline ChampSim binary into build/baseline/
	$(MODULES) cd $(CHAMPSIM_DIR) && $(CURDIR)/$(VENV_DIR)/evaluator/bin/python config.sh \
	    --bindir $(CURDIR)/$(BASELINE_DIR) $(CURDIR)/$(BASELINE_CONFIG)
	$(MODULES) $(MAKE) -C $(CHAMPSIM_DIR) -j$(JOBS)

check: ## Verify modules, pinned ChampSim, venvs vs locks, OpenEvolve, baseline binary
	$(MODULES) env/check.sh

smoke: ## Run 10K+10K instructions of TRACE=<path> through the baseline binary
	@[ -n "$(TRACE)" ] || { echo "usage: make smoke TRACE=/path/to/trace.champsimtrace.xz" >&2; exit 2; }
	mkdir -p build
	$(MODULES) $(BASELINE_DIR)/champsim --warmup-instructions 10000 --simulation-instructions 10000 \
	    --json build/smoke.json "$(TRACE)" > build/smoke.log
	$(MODULES) $(VENV_DIR)/evaluator/bin/python -c 'import json; roi = json.load(open("build/smoke.json"))[-1]["roi"]["cores"][0]; \
	    print("smoke ok:", roi["instructions"], "instructions,", roi["cycles"], "cycles")'

env-record: ## Write env/modules.lock.txt and env/versions.txt
	$(MODULES) module -t list > env/modules.lock.txt 2>&1
	$(MODULES) env/record-versions.sh > env/versions.txt

lock: ## Re-resolve env/*-requirements.in into the pinned *.txt lock files
	$(MODULES) for role in $(ROLES); do PIP_FLAGS="$(PIP_FLAGS)" env/lock.sh $$role; done

modules-save: ## Also save the module set as a personal Lmod collection (afar-hpc)
	$(MODULES) module save afar-hpc

clean: ## Remove ChampSim build outputs and build/
	-$(MODULES) $(MAKE) -C $(CHAMPSIM_DIR) clean
	rm -rf build

distclean: clean ## Also remove the venvs and vcpkg_installed/
	rm -rf $(VENV_DIR) $(CHAMPSIM_DIR)/vcpkg_installed
