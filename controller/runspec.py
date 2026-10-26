"""Run specs: one OpenEvolve run assembled from named, swappable parts (docs/run-specs.md).

A run spec, configs/runs/<name>.yaml, names one choice for each lever:
    seed       the initial program, and the text the evaluator checks candidates against
    model      configs/models/<name>.env, with the shared server pin configs/llm/vllm.env
    prompt     configs/prompts/<name>/: prompt.yaml, system_message.txt, OpenEvolve template overrides
    search     configs/search/<name>.yaml: OpenEvolve's database and evolution settings
    evaluator  a registered policy (evaluator/policies) on an experiment manifest, or a raw
               OpenEvolve evaluator file (openevolve_file), with its CPUs and executor
A profile can also be named by a path (any value with a "/").

This module checks that the parts exist and fit together, derives the settings
that depend on them (evaluator.timeout from the policy's own limits, the code
length cap from the manifest, the CPU budget), and writes what OpenEvolve and
the evaluator read:
    <run>/resolved/config.yaml   the OpenEvolve config
    <run>/resolved/prompt/       a snapshot of the prompt profile's templates
    <run>/run.json               the parts, every input's sha256, commits, and each job that ran it

    python -m controller.runspec --check SPEC [--cpus N] [--shell]    validate; --shell prints job variables
    python -m controller.runspec --resolve SPEC --run-dir D --api-base URL [--cpus N]
    python -m controller.runspec --describe RUN_DIR
    python -m controller.runspec --selftest
SPEC is a name under configs/runs/ or a path. Resolving an existing run
(a resume) refuses if any input changed, and otherwise records the new job.
"""

import argparse
import copy
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import openevolve
import yaml
from openevolve.config import Config, load_config

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from controller.oe_config import summarize, unknown_keys  # noqa: E402
from evaluator import results  # noqa: E402
from evaluator.manifest import Manifest  # noqa: E402
from evaluator.policies import REGISTRY, InfraError, policy_class  # noqa: E402

CONFIGS = ROOT / "configs"
VLLM_ENV = CONFIGS / "llm" / "vllm.env"
ADAPTER = "evaluator/openevolve_adapter.py"
SPEC_VERSION = 1
RUN_JSON_VERSION = 1

SPEC_KEYS = {"spec_version": int, "description": str, "seed": str, "model": str, "prompt": str, "search": str,
             "evaluator": dict}
SPEC_OPTIONAL = {"language": str, "overrides": dict, "iterations": int, "campaign": dict}
# A staged campaign (controller/campaign.py, M5): GPU jobs generate, CPU arrays evaluate.
CAMPAIGN_KEYS = {"generations": int, "candidates_per_generation": int}
CAMPAIGN_OPTIONAL = {"max_concurrent_tasks": int, "max_task_retries": int}
CAMPAIGN_DEFAULTS = {"max_concurrent_tasks": 40, "max_task_retries": 2}
NODE_CPUS = 56                         # HPC3's CPU nodes (docs/sjsu-hpc-workflow.md); a candidate's task fits one
EVALUATOR_KEYS = {"policy": str, "experiment": str, "openevolve_file": str, "executor": str,
                  "parallel_candidates": int, "cpus_per_candidate": int, "timeout_s": int, "openevolve": dict}
PROFILE_KEYS = {"prompt": {"description": str, "requires": dict, "openevolve": dict},
                "search": {"description": str, "openevolve": dict}}
MODEL_KEYS = ("LLM_SERVER", "MODEL_ID", "MODEL_REVISION", "MODEL_DIR", "CONTEXT_TOKENS", "VLLM_SERVE_ARGS",
              "LLM_TEMPERATURE", "LLM_MAX_TOKENS", "LLM_TIMEOUT_S")
VLLM_KEYS = ("VLLM_IMAGE", "VLLM_SIF", "VLLM_SIF_SHA256", "SERVED_MODEL_NAME")
SERVERS = ("vllm", "fake")
EXECUTORS = ("local",)                 # M5 adds slurm-array
REQUIRES = ("champsim_commit", "module_name")
LANGUAGES = {".h": "cpp", ".hpp": "cpp", ".cc": "cpp", ".cpp": "cpp", ".py": "python"}
TEMPLATE_NAMES = {p.stem for p in (Path(openevolve.__file__).parent / "prompts" / "defaults").glob("*.txt")}
PROMPT_EXTRA_FILES = {"prompt.yaml", "fragments.json"}
# Settings this module derives or owns; a search profile setting them would be overwritten.
SEARCH_OWNED = ("llm", "prompt", "evaluator", "max_iterations", "language", "log_level", "log_dir")
PROMPT_OWNED = ("template_dir", "system_message")
RESERVED_CPUS = 2                      # OpenEvolve's controller and the LLM server
DEFAULT_JOB_CPUS = 16                  # slurm/openevolve.sbatch's request
TIMEOUT_MARGIN_S = 60
CODE_LENGTH_MARGIN = 8192              # above the policy's own size limit, so the evaluator reports oversize itself
MAX_RETRIES = 1                        # OpenEvolve retries only when evaluate() raises, i.e. infra_error


class SpecError(ValueError):
    pass


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rel(path):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def spec_path(ref):
    p = Path(ref)
    return p if p.suffix in (".yaml", ".yml") else CONFIGS / "runs" / f"{ref}.yaml"


def component_path(kind, ref):
    """A profile by name (configs/<kind>/<name>[.ext]) or by path (any value with a '/')."""
    if "/" in ref:
        p = Path(ref)
        return p if p.is_absolute() else ROOT / p
    return {"model": CONFIGS / "models" / f"{ref}.env", "prompt": CONFIGS / "prompts" / ref,
            "search": CONFIGS / "search" / f"{ref}.yaml"}[kind]


def load_yaml(path, what):
    try:
        data = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise SpecError(f"{what} {rel(path)}: {e}")
    if not isinstance(data, dict):
        raise SpecError(f"{what} {rel(path)} must be a mapping")
    return data


def schema_problems(data, required, optional, where):
    found = []
    for key, kind in {**required, **optional}.items():
        if key not in data:
            if key in required:
                found.append(f"{where}: missing {key}")
        elif not isinstance(data[key], kind) or (kind is int and isinstance(data[key], bool)):
            found.append(f"{where}: {key} must be {kind.__name__}")
    extra = set(data) - set(required) - set(optional)
    if extra:
        found.append(f"{where}: unknown key(s) {', '.join(sorted(extra))}")
    return found


def read_env(paths, keys):
    """Source shell files in order, as the jobs do, and return {key: value} for the keys that are set."""
    script = ('set -eu; n="$1"; shift; for ((i = 0; i < n; i++)); do source "$1"; shift; done; '
              'for v in "$@"; do if [[ -n "${!v+set}" ]]; then printf "%s\\0%s\\0" "$v" "${!v}"; fi; done')
    out = subprocess.run(["bash", "-c", script, "_", str(len(paths)), *map(str, paths), *keys],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SpecError(f"sourcing {', '.join(rel(p) for p in paths)} failed: {out.stderr.strip()}")
    parts = out.stdout.split("\0")[:-1]
    return dict(zip(parts[0::2], parts[1::2]))


def set_dotted(config, dotted, value):
    node = config
    *parents, last = dotted.split(".")
    for key in parents:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise SpecError(f"override {dotted}: {key} is not a section")
    node[last] = value


def deep_merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def config_digest(config):
    """sha256 of the resolved config, without the values that differ per job or run directory."""
    stable = copy.deepcopy(config)
    stable.get("llm", {}).pop("api_base", None)
    stable.get("prompt", {}).pop("template_dir", None)
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


# --- resolution -------------------------------------------------------------

def resolve(ref, *, cpus, api_base=None, template_dir="<run>/resolved/prompt"):
    """Validate a spec and build everything a run needs. Returns a dict; raises SpecError listing every problem."""
    path = spec_path(ref)
    if not path.is_file():
        raise SpecError(f"no run spec {rel(path)} (names are files in configs/runs/)")
    spec = load_yaml(path, "run spec")
    problems = schema_problems(spec, SPEC_KEYS, SPEC_OPTIONAL, "spec")
    if problems:
        raise SpecError("; ".join(problems))
    if spec["spec_version"] != SPEC_VERSION:
        raise SpecError(f"spec: spec_version {spec['spec_version']} is not supported (this resolver reads {SPEC_VERSION})")
    campaign = None
    if "campaign" in spec:
        problems += schema_problems(spec["campaign"], CAMPAIGN_KEYS, CAMPAIGN_OPTIONAL, "campaign")
        if problems:
            raise SpecError("; ".join(problems))
        campaign = {**CAMPAIGN_DEFAULTS, **spec["campaign"]}
        if min(campaign["generations"], campaign["candidates_per_generation"], campaign["max_concurrent_tasks"]) < 1 \
                or campaign["max_task_retries"] < 0:
            problems.append("campaign: generations, candidates_per_generation and max_concurrent_tasks must be at "
                            "least 1, and max_task_retries at least 0")
        total = campaign["generations"] * campaign["candidates_per_generation"]
        if spec.get("iterations", total) != total:
            problems.append(f"spec: iterations {spec['iterations']} must equal generations x candidates_per_generation "
                            f"({total}) in a campaign, or be left out")
        iterations = total
    elif "iterations" not in spec:
        raise SpecError("spec: missing iterations (only a campaign derives it)")
    else:
        iterations = spec["iterations"]
    if iterations < 1:
        problems.append("spec: iterations must be at least 1")
    inputs = {rel(path): sha256_file(path)}

    def track(p):
        inputs[rel(p)] = sha256_file(p)

    # Seed: OpenEvolve's initial program and the evaluator's reference text.
    seed = ROOT / spec["seed"]
    language = spec.get("language") or LANGUAGES.get(seed.suffix)
    if not seed.is_file():
        problems.append(f"seed: no file {spec['seed']}")
    else:
        track(seed)
    if not language:
        problems.append(f"seed: can't tell the language of {spec['seed']}; set `language:` in the spec")

    # Model profile, after the shared server pin.
    model_file = component_path("model", spec["model"])
    model = {}
    if not model_file.is_file():
        problems.append(f"model: no profile {rel(model_file)}")
    else:
        model = read_env([VLLM_ENV, model_file], VLLM_KEYS + MODEL_KEYS)
        track(VLLM_ENV), track(model_file)
        missing = [k for k in VLLM_KEYS + MODEL_KEYS if k not in model]
        if missing:
            problems.append(f"model: {rel(model_file)} doesn't set {', '.join(missing)}")
        else:
            try:
                context, max_tokens = int(model["CONTEXT_TOKENS"]), int(model["LLM_MAX_TOKENS"])
                model.update(CONTEXT_TOKENS=context, LLM_MAX_TOKENS=max_tokens,
                             LLM_TEMPERATURE=float(model["LLM_TEMPERATURE"]), LLM_TIMEOUT_S=int(model["LLM_TIMEOUT_S"]))
            except ValueError as e:
                problems.append(f"model: {rel(model_file)}: {e}")
            else:
                if max_tokens >= context:
                    problems.append(f"model: LLM_MAX_TOKENS {max_tokens} leaves no room for the prompt in "
                                    f"CONTEXT_TOKENS {context}")
            if model["LLM_SERVER"] not in SERVERS:
                problems.append(f"model: LLM_SERVER must be one of {', '.join(SERVERS)}")
            elif model["LLM_SERVER"] == "vllm":
                assets = Path(os.environ.get("AFAR_ASSETS", ""))
                weights = assets / "models" / model["MODEL_DIR"] / ".complete"
                sif = assets / "containers" / model["VLLM_SIF"]
                for need in (weights, sif):
                    if not os.environ.get("AFAR_ASSETS") or not need.exists():
                        problems.append(f"model: {need} is missing; run `make llm-fetch PROFILE={spec['model']}` on "
                                        "coe-hpc1 (and slurm/build_sif.sbatch for the container)")

    # Prompt profile: a directory of OpenEvolve templates plus prompt.yaml.
    prompt_dir = component_path("prompt", spec["prompt"])
    prompt = {"requires": {}, "openevolve": {}}
    template_files = []
    if not (prompt_dir / "prompt.yaml").is_file():
        problems.append(f"prompt: no profile {rel(prompt_dir)}/prompt.yaml (OpenEvolve would silently use its "
                        "default prompt)")
    else:
        prompt = load_yaml(prompt_dir / "prompt.yaml", "prompt profile")
        problems += schema_problems(prompt, PROFILE_KEYS["prompt"], {}, f"prompt {rel(prompt_dir)}/prompt.yaml")
        for f in sorted(prompt_dir.iterdir()):
            track(f)
            if f.name in PROMPT_EXTRA_FILES:
                continue
            if f.suffix != ".txt" or f.stem not in TEMPLATE_NAMES:
                problems.append(f"prompt: {rel(f)} is not an OpenEvolve template name (known: "
                                f"{', '.join(sorted(TEMPLATE_NAMES))}), so OpenEvolve would ignore it")
            else:
                template_files.append(f)
        owned = [k for k in PROMPT_OWNED if k in prompt.get("openevolve", {})]
        if owned:
            problems.append(f"prompt: the resolver sets {', '.join(owned)} from the profile's files; remove it")
        unknown_req = set(prompt.get("requires", {})) - set(REQUIRES)
        if unknown_req:
            problems.append(f"prompt: unknown requires key(s) {', '.join(sorted(unknown_req))} (known: {', '.join(REQUIRES)})")

    # Search profile.
    search_file = component_path("search", spec["search"])
    search = {"openevolve": {}}
    if not search_file.is_file():
        problems.append(f"search: no profile {rel(search_file)}")
    else:
        search = load_yaml(search_file, "search profile")
        track(search_file)
        problems += schema_problems(search, PROFILE_KEYS["search"], {}, f"search {rel(search_file)}")
        owned = [k for k in SEARCH_OWNED if k in search.get("openevolve", {})]
        if owned:
            problems.append(f"search: {', '.join(owned)} belong to the run spec or another profile, not the search profile")
        if campaign and search.get("openevolve", {}).get("diff_based_evolution", True):
            problems.append("search: a campaign generates full rewrites only; set diff_based_evolution: false")

    # Evaluator: a registered policy on a manifest, or a raw OpenEvolve evaluator file.
    ev = spec["evaluator"]
    problems += schema_problems(ev, {}, EVALUATOR_KEYS, "evaluator")
    parallel, per = ev.get("parallel_candidates", 1), ev.get("cpus_per_candidate", 1)
    executor = ev.get("executor", "local")
    if executor not in EXECUTORS:
        problems.append(f"evaluator: executor {executor!r} is not available (known: {', '.join(EXECUTORS)})")
    if parallel < 1 or per < 1:
        problems.append("evaluator: parallel_candidates and cpus_per_candidate must be at least 1")
    elif campaign:
        # Each candidate is its own array task on a CPU node, so only the node size limits it.
        if per > NODE_CPUS:
            problems.append(f"evaluator: cpus_per_candidate {per} exceeds a CPU node's {NODE_CPUS}")
        if "openevolve_file" in ev:
            problems.append("evaluator: a campaign evaluates with a registered policy, not openevolve_file")
    elif parallel * per + RESERVED_CPUS > cpus:
        problems.append(f"evaluator: {parallel} candidates x {per} CPUs + {RESERVED_CPUS} for OpenEvolve and the server "
                        f"needs {parallel * per + RESERVED_CPUS} CPUs; the job has {cpus}")
    evaluator_cfg, evaluator_info, max_code_length, manifest = {}, {}, None, None
    evaluator_file = ROOT / ADAPTER
    if ("policy" in ev) == ("openevolve_file" in ev):
        problems.append("evaluator: set exactly one of policy (a registered evaluator policy) and openevolve_file")
    elif "policy" in ev:
        for key in ("timeout_s",):
            if key in ev:
                problems.append(f"evaluator: {key} is derived from the policy and the manifest; remove it")
        try:
            cls = policy_class(ev["policy"])
        except ValueError as e:
            problems.append(f"evaluator: {e}")
            cls = None
        if "experiment" not in ev:
            problems.append("evaluator: a policy needs an experiment (a manifest id in configs/experiments/)")
        else:
            manifest_file = CONFIGS / "experiments" / f"{ev['experiment']}.json"
            try:
                manifest = Manifest(manifest_file)
            except (OSError, ValueError) as e:
                problems.append(f"evaluator: experiment {ev['experiment']}: {e}")
            else:
                track(manifest_file)
                champsim = manifest["champsim"]
                have = {"champsim_commit": champsim["commit"], "module_name": champsim["module_name"]}
                for key, want in prompt.get("requires", {}).items():
                    if key in have and have[key] != want:
                        problems.append(f"prompt: requires {key} {want!r}, but {ev['experiment']} has {have[key]!r}")
                if cls is not None:
                    if cls.needs_simulation:
                        problems += [f"evaluator: {p}" for p in manifest.check_traces()]
                    if cls.needs_baselines:
                        from evaluator import evaluate_candidate as ec
                        try:
                            ec.load_baselines(manifest, ec.results_root() / "baselines" / manifest.experiment_id)
                        except InfraError as e:
                            problems.append(f"evaluator: {e}")
                    limit = cls().time_limit_s(manifest, per)
                    evaluator_cfg = {"timeout": limit + TIMEOUT_MARGIN_S, "max_retries": MAX_RETRIES,
                                     "cascade_evaluation": False, "parallel_evaluations": parallel}
                    max_code_length = manifest["limits"]["max_source_bytes"] + CODE_LENGTH_MARGIN
                evaluator_info = {"policy": ev["policy"], "experiment": manifest.experiment_id,
                                  "manifest": rel(manifest_file), "manifest_sha256": manifest.sha256}
        evaluator_file = ROOT / ADAPTER
    else:
        for key in ("experiment",):
            if key in ev:
                problems.append(f"evaluator: {key} applies only to a policy, not openevolve_file")
        evaluator_file = ROOT / ev["openevolve_file"]
        if not evaluator_file.is_file():
            problems.append(f"evaluator: no file {ev['openevolve_file']}")
        else:
            track(evaluator_file)
        if "timeout_s" not in ev:
            problems.append("evaluator: openevolve_file needs timeout_s (OpenEvolve's evaluator.timeout)")
        if prompt.get("requires"):
            problems.append("prompt: requires a ChampSim manifest, but the evaluator is a raw OpenEvolve file")
        evaluator_cfg = {"timeout": ev.get("timeout_s"), "parallel_evaluations": parallel}
        evaluator_info = {"openevolve_file": ev["openevolve_file"]}
    evaluator_cfg.update(ev.get("openevolve", {}))
    evaluator_info.update(executor=executor, parallel_candidates=parallel, cpus_per_candidate=per,
                          file=rel(evaluator_file))

    if problems:
        raise SpecError("; ".join(problems))

    # Compose the OpenEvolve config: search profile, then derived and owned settings, then overrides.
    config = deep_merge({}, search["openevolve"])
    config.update(max_iterations=iterations, language=language)
    if max_code_length is not None:
        config["max_code_length"] = max_code_length
    config["llm"] = {
        "api_base": api_base or "http://127.0.0.1:0/v1",       # a literal: OpenEvolve doesn't expand ${VAR} here
        "api_key": "${AFAR_LLM_API_KEY}",                       # expanded by OpenEvolve; random per job (llm_serve.sh)
        "models": [{"name": model["SERVED_MODEL_NAME"], "weight": 1.0}],
        "temperature": model["LLM_TEMPERATURE"], "max_tokens": model["LLM_MAX_TOKENS"],
        "timeout": model["LLM_TIMEOUT_S"],
    }
    config["prompt"] = deep_merge({"template_dir": str(template_dir)}, prompt["openevolve"])
    system_file = next((f for f in template_files if f.stem == "system_message"), None)
    if system_file:
        # The text itself, not the template name: OpenEvolve also copies prompt.system_message
        # into each model's settings, where a name would reach the LLM as the word "system_message".
        config["prompt"]["system_message"] = system_file.read_text()
    config["evaluator"] = evaluator_cfg
    for dotted, value in (spec.get("overrides") or {}).items():
        set_dotted(config, dotted, value)
    unknown = list(unknown_keys(Config, config))
    if unknown:
        raise SpecError(f"unknown OpenEvolve key(s), which OpenEvolve would silently ignore: {', '.join(unknown)}")

    return {
        "spec_file": path, "spec": spec, "config": config, "inputs": inputs, "template_files": template_files,
        "prompt_dir": prompt_dir, "seed": seed, "language": language, "evaluator_file": evaluator_file,
        "evaluator": evaluator_info, "manifest": manifest, "campaign": campaign, "iterations": iterations,
        "model": {"profile": spec["model"], "server": model["LLM_SERVER"], "model_id": model["MODEL_ID"],
                  "revision": model["MODEL_REVISION"], "served_name": model["SERVED_MODEL_NAME"],
                  "context_tokens": model["CONTEXT_TOKENS"], "vllm_image": model["VLLM_IMAGE"],
                  "vllm_sif_sha256": model["VLLM_SIF_SHA256"], "file": rel(model_file)},
    }


def write(resolution, resolved_dir):
    """Write config.yaml and the prompt snapshot; load the config with OpenEvolve's loader as a last check."""
    resolved_dir = Path(resolved_dir)
    if resolved_dir.exists():
        shutil.rmtree(resolved_dir)
    (resolved_dir / "prompt").mkdir(parents=True)
    for f in resolution["template_files"]:
        shutil.copy2(f, resolved_dir / "prompt" / f.name)
    fragments = resolution["prompt_dir"] / "fragments.json"
    if fragments.is_file():
        shutil.copy2(fragments, resolved_dir / "prompt" / fragments.name)
    config_path = resolved_dir / "config.yaml"
    header = (f"# Resolved by controller/runspec.py from {rel(resolution['spec_file'])} at {results.now()}.\n"
              "# Generated: edit the spec or its profiles, not this file.\n")
    config_path.write_text(header + yaml.safe_dump(resolution["config"], sort_keys=False, width=120))
    key_was = os.environ.get("AFAR_LLM_API_KEY")
    os.environ.setdefault("AFAR_LLM_API_KEY", "validation-only")
    try:
        loaded = load_config(str(config_path))
    except Exception as e:  # any loader failure is a spec problem
        raise SpecError(f"OpenEvolve's loader rejects the resolved config: {type(e).__name__}: {e}")
    finally:
        if key_was is None:
            os.environ.pop("AFAR_LLM_API_KEY", None)
    return config_path, loaded


def run_record(resolution, run_dir, api_base):
    return {
        "run_json_version": RUN_JSON_VERSION,
        "run_id": Path(run_dir).name,
        "spec": {"name": resolution["spec_file"].stem, "file": rel(resolution["spec_file"]),
                 "description": resolution["spec"]["description"], "data": resolution["spec"]},
        "seed": {"path": rel(resolution["seed"]), "sha256": sha256_file(resolution["seed"])},
        "language": resolution["language"],
        "model": resolution["model"],
        "prompt": {"profile": resolution["spec"]["prompt"], "dir": rel(resolution["prompt_dir"]),
                   "templates": [f.name for f in resolution["template_files"]]},
        "search": {"profile": resolution["spec"]["search"]},
        "evaluator": resolution["evaluator"],
        "campaign": resolution["campaign"],
        "inputs": resolution["inputs"],
        "resolved_config_sha256": config_digest(resolution["config"]),
        "project_commit": results.project_commit(),
        "champsim_commit": results.champsim_commit(),
        "jobs": [],
    }


def resolve_run(ref, run_dir, api_base, cpus):
    """--resolve: write <run>/resolved/ and create or extend <run>/run.json."""
    run_dir = Path(run_dir)
    resolution = resolve(ref, cpus=cpus, api_base=api_base, template_dir=(run_dir / "resolved" / "prompt").resolve())
    record = run_record(resolution, run_dir, api_base)
    run_json = run_dir / "run.json"
    if run_json.exists():
        old = json.loads(run_json.read_text())
        changed = sorted(k for k in set(old["inputs"]) | set(record["inputs"])
                         if old["inputs"].get(k) != record["inputs"].get(k))
        if changed or old["resolved_config_sha256"] != record["resolved_config_sha256"]:
            raise SpecError(f"{run_dir} was started with different inputs ({', '.join(changed) or 'resolved config'}); "
                            "a resumed run must use exactly the same parts, so start a new run instead")
        record["jobs"] = old["jobs"]
        record["project_commit"], record["champsim_commit"] = old["project_commit"], old["champsim_commit"]
    config_path, loaded = write(resolution, run_dir / "resolved")
    record["jobs"].append({"job_id": os.environ.get("SLURM_JOB_ID"), "node": socket.gethostname(),
                           "resolved_at": results.now(), "api_base": api_base,
                           "project_commit": results.project_commit()})
    results.write_atomic(run_json, record)
    return resolution, record, loaded


def describe_resolution(resolution, loaded):
    ev, m = resolution["evaluator"], resolution["model"]
    which = (f"policy {ev['policy']} on {ev['experiment']}" if "policy" in ev else f"OpenEvolve file {ev['openevolve_file']}")
    lines = [f"spec      {rel(resolution['spec_file'])}: {resolution['spec']['description']}",
             f"seed      {rel(resolution['seed'])} ({resolution['language']})",
             f"model     {m['profile']}: {m['model_id']}@{m['revision'][:12]} via {m['server']}, "
             f"context {m['context_tokens']}",
             f"prompt    {resolution['spec']['prompt']}: {', '.join(f.name for f in resolution['template_files']) or 'defaults'}",
             f"search    {resolution['spec']['search']}",
             f"evaluator {which}, executor {ev['executor']}, {ev['parallel_candidates']} x {ev['cpus_per_candidate']} CPUs"]
    return "\n".join(lines) + "\n" + summarize(loaded)


def shell_vars(resolution):
    ev = resolution["evaluator"]
    values = {"AFAR_RUN_SEED": rel(resolution["seed"]), "AFAR_RUN_MODEL_PROFILE": resolution["model"]["file"],
              "AFAR_RUN_EVALUATOR_FILE": ev["file"], "AFAR_RUN_POLICY": ev.get("policy", ""),
              "AFAR_RUN_EXPERIMENT": ev.get("manifest", ""), "AFAR_RUN_CPUS_PER_CANDIDATE": str(ev["cpus_per_candidate"])}
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items())


def check(ref, cpus, shell=False):
    with tempfile.TemporaryDirectory(prefix="afar-runspec-") as tmp:
        resolution = resolve(ref, cpus=cpus, template_dir=Path(tmp, "prompt"))
        _, loaded = write(resolution, tmp)
        out = sys.stderr if shell else sys.stdout
        print(describe_resolution(resolution, loaded), file=out)
        print(f"ok: {rel(resolution['spec_file'])} resolves (config {config_digest(resolution['config'])[:12]})", file=out)
    if shell:
        print(shell_vars(resolution), end="")
    return 0


def describe_run(run_dir):
    record = json.loads((Path(run_dir) / "run.json").read_text())
    ev, m = record["evaluator"], record["model"]
    print(f"run {record['run_id']}: {record['spec']['file']} ({record['spec']['description']})")
    print(f"  seed {record['seed']['path']} {record['seed']['sha256'][:12]}; model {m['profile']} "
          f"({m['model_id']}@{m['revision'][:12]}); prompt {record['prompt']['profile']}; search {record['search']['profile']}")
    print(f"  evaluator {ev.get('policy') or ev.get('openevolve_file')}"
          f"{' on ' + ev['experiment'] if 'experiment' in ev else ''}, {ev['parallel_candidates']} x "
          f"{ev['cpus_per_candidate']} CPUs; config {record['resolved_config_sha256'][:12]}; "
          f"commit {record['project_commit']}")
    for job in record["jobs"]:
        print(f"  job {job['job_id']} on {job['node']} at {job['resolved_at']}")
    return 0


# --- selftest ---------------------------------------------------------------

def selftest():
    """Resolution of the repo's specs, and each validation rule on a broken variant."""
    failures = 0

    def expect(condition, message):
        if not condition:
            raise SpecError(message)

    def case(name, fn, needle=None):
        nonlocal failures
        try:
            detail = fn()
            ok = needle is None
            detail = detail if isinstance(detail, str) else "resolves"
        except SpecError as e:
            ok, detail = needle is not None and needle in str(e), str(e)
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<40} {detail[:150]}")

    saved = {k: os.environ.get(k) for k in ("AFAR_ASSETS", "AFAR_RESULTS")}
    with tempfile.TemporaryDirectory(prefix="afar-runspec-selftest-") as tmp:
        tmp = Path(tmp)
        # Stand-in assets, so the test doesn't depend on what this host has downloaded.
        assets = tmp / "assets"
        env = read_env([VLLM_ENV, CONFIGS / "models" / "qwen2.5-coder-7b-v1.env"], ("MODEL_DIR", "VLLM_SIF"))
        (assets / "models" / env["MODEL_DIR"]).mkdir(parents=True)
        (assets / "models" / env["MODEL_DIR"] / ".complete").write_text("selftest\n")
        (assets / "containers").mkdir()
        (assets / "containers" / env["VLLM_SIF"]).write_text("selftest\n")
        os.environ["AFAR_ASSETS"], os.environ["AFAR_RESULTS"] = str(assets), str(tmp / "results")

        def variant(name, edit, base="champsim-compile-v1"):
            data = load_yaml(spec_path(base), "run spec")
            edit(data)
            p = tmp / f"{name}.yaml"
            p.write_text(yaml.safe_dump(data))
            return p

        def profile(kind, name, files):
            d = tmp / kind / name
            d.mkdir(parents=True)
            for fname, text in files.items():
                (d / fname).write_text(text)
            return d

        def resolved(ref, cpus=DEFAULT_JOB_CPUS):
            def go():
                r = resolve(ref, cpus=cpus)
                write(r, tmp / f"out-{Path(str(ref)).stem}")
                return r
            return go

        def compile_spec():
            c = resolve("champsim-compile-v1", cpus=DEFAULT_JOB_CPUS)["config"]
            expect(c["evaluator"]["timeout"] == 120 and c["max_code_length"] == 40960 and c["language"] == "cpp"
                   and c["llm"]["models"] == [{"name": "afar-llm", "weight": 1.0}], f"derived values wrong: {c}")
        case("champsim-compile-v1 resolves", compile_spec)
        case("toy-v1 resolves", resolved("toy-v1"))
        cpp_prompt = (CONFIGS / "prompts" / "champsim-rewrite-v1" / "prompt.yaml").read_text()
        case("unknown spec key", resolved(variant("k", lambda d: d.update(seeed="x"))), "unknown key(s) seeed")
        case("missing prompt profile", resolved(variant("p", lambda d: d.update(prompt="nope-v1"))), "no profile")
        bad = profile("prompts", "typo", {"prompt.yaml": cpp_prompt, "sytem_message.txt": "x"})
        case("misspelled template file", resolved(variant("t", lambda d: d.update(prompt=str(bad)))),
             "not an OpenEvolve template name")
        owned = profile("prompts", "owned", {"prompt.yaml": cpp_prompt.replace("openevolve:\n", "openevolve:\n  template_dir: x\n")})
        case("prompt sets template_dir", resolved(variant("o", lambda d: d.update(prompt=str(owned)))), "the resolver sets")
        req = profile("prompts", "req", {"prompt.yaml": cpp_prompt.replace("module_name: evolved_repl", "module_name: other")})
        case("prompt requires another module", resolved(variant("r", lambda d: d.update(prompt=str(req)))),
             "requires module_name")
        search = (CONFIGS / "search" / "champsim-rewrite-v1.yaml").read_text().replace("population_size", "populaton_size")
        (tmp / "typo-search.yaml").write_text(search)
        case("unknown OpenEvolve key in search", resolved(variant("s", lambda d: d.update(search=str(tmp / "typo-search.yaml")))),
             "database.populaton_size")
        model = (CONFIGS / "models" / "fake-v1.env").read_text()
        (tmp / "nocontext.env").write_text(model.replace("CONTEXT_TOKENS=16384\n", ""))
        case("model profile missing a key", resolved(variant("m", lambda d: d.update(model=str(tmp / "nocontext.env")))),
             "doesn't set CONTEXT_TOKENS")
        (tmp / "small.env").write_text(model.replace("CONTEXT_TOKENS=16384", "CONTEXT_TOKENS=4096"))
        case("max_tokens fills the context", resolved(variant("c", lambda d: d.update(model=str(tmp / "small.env")))),
             "leaves no room")
        os.environ["AFAR_ASSETS"] = str(tmp / "empty")
        case("model weights not downloaded", resolved("champsim-compile-v1"), "make llm-fetch")
        case("fake model needs no assets", resolved(variant("f", lambda d: d.update(model="fake-v1"))))
        os.environ["AFAR_ASSETS"] = str(assets)
        case("unregistered policy", resolved(variant("u", lambda d: d["evaluator"].update(policy="nope"))),
             "unknown evaluator policy")
        case("CPU budget exceeded", resolved(variant("b", lambda d: d["evaluator"].update(parallel_candidates=4,
                                                                                            cpus_per_candidate=4))),
             "needs 18 CPUs; the job has 16")
        case("policy and openevolve_file", resolved(variant("e", lambda d: d["evaluator"].update(
            openevolve_file="examples/function_minimization/evaluator.py"))), "exactly one of")
        case("unknown executor", resolved(variant("x", lambda d: d["evaluator"].update(executor="cloud"))),
             "executor 'cloud'")
        case("baselines missing", resolved(variant("bl", lambda d: d["evaluator"].update(policy="champsim_ipc"))),
             "baselines unusable")
        over = variant("ov", lambda d: d.update(overrides={"database.num_islands": 2}))
        case("override applied", lambda: expect(resolve(over, cpus=16)["config"]["database"]["num_islands"] == 2,
                                                "override not applied"))

        # Campaigns (M5): iterations derive from the generations; the evaluation's CPUs come from array tasks.
        camp = {"generations": 3, "candidates_per_generation": 4}
        def campaign_spec():
            r = resolve(variant("cp", lambda d: (d.pop("iterations"), d.update(campaign=camp))), cpus=4)
            expect(r["iterations"] == 12 and r["config"]["max_iterations"] == 12
                   and r["campaign"]["max_task_retries"] == 2, f"campaign not derived: {r['campaign']}")
        case("campaign derives iterations", campaign_spec)
        case("campaign iterations mismatch", resolved(variant("cm", lambda d: d.update(campaign=camp))),
             "must equal generations x candidates_per_generation")
        case("campaign task larger than a node", resolved(variant("cn", lambda d: (d.pop("iterations"), d.update(
            campaign=camp), d["evaluator"].update(cpus_per_candidate=64)))), "exceeds a CPU node's 56")
        case("campaign with an OpenEvolve file", resolved(variant("co", lambda d: (d.pop("iterations"), d.update(
            campaign=camp)), base="toy-v1")), "not openevolve_file")
        diff = (CONFIGS / "search" / "champsim-rewrite-v1.yaml").read_text().replace(
            "diff_based_evolution: false", "diff_based_evolution: true")
        (tmp / "diff-search.yaml").write_text(diff)
        case("campaign in diff mode", resolved(variant("cd", lambda d: (d.pop("iterations"), d.update(
            campaign=camp, search=str(tmp / "diff-search.yaml"))))), "full rewrites only")

        # Derived timeout for champsim_ipc comes from the policy's own limits.
        m = Manifest(CONFIGS / "experiments" / "crc2-evolve-v1.json")
        limit = policy_class("champsim_ipc")().time_limit_s(m, 7)
        case("champsim_ipc time limit, 14 traces on 7 CPUs", lambda: expect(limit == 60 + 600 + 2 * 7200, f"got {limit}"))

        # Resume: the same inputs append a job; a changed input is refused.
        run_dir = tmp / "runs" / "toy-v1-1"
        resolve_run("toy-v1", run_dir, "http://127.0.0.1:20001/v1", 16)
        _, record, _ = resolve_run("toy-v1", run_dir, "http://127.0.0.1:20002/v1", 16)
        case("resume appends a job", lambda: expect(len(record["jobs"]) == 2 and
             (run_dir / "resolved" / "prompt" / "system_message.txt").exists(), "jobs not appended"))
        changed = variant("toy-changed", lambda d: d.update(iterations=11), base="toy-v1")
        case("resume with changed inputs", lambda: resolve_run(changed, run_dir, "http://x/v1", 16) and None,
             "different inputs")
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    print("runspec selftest passed" if failures == 0 else f"runspec selftest FAILED: {failures} case(s)")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--check", metavar="SPEC")
    which.add_argument("--resolve", metavar="SPEC")
    which.add_argument("--describe", metavar="RUN_DIR")
    which.add_argument("--selftest", action="store_true")
    parser.add_argument("--cpus", type=int, default=int(os.environ.get("SLURM_CPUS_PER_TASK", DEFAULT_JOB_CPUS)),
                        help=f"CPUs the job has (default $SLURM_CPUS_PER_TASK, else {DEFAULT_JOB_CPUS})")
    parser.add_argument("--shell", action="store_true", help="with --check: print shell variables for the job")
    parser.add_argument("--run-dir")
    parser.add_argument("--api-base")
    args = parser.parse_args()
    try:
        if args.selftest:
            return selftest()
        if args.describe:
            return describe_run(args.describe)
        if args.check:
            return check(args.check, args.cpus, args.shell)
        if not (args.run_dir and args.api_base):
            parser.error("--resolve needs --run-dir and --api-base")
        resolution, record, loaded = resolve_run(args.resolve, args.run_dir, args.api_base, args.cpus)
        print(describe_resolution(resolution, loaded))
        print(f"-> {Path(args.run_dir) / 'resolved' / 'config.yaml'}; run.json lists {len(record['jobs'])} job(s)")
        return 0
    except SpecError as e:
        print("spec problem(s):\n  " + str(e).replace("; ", "\n  "), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
