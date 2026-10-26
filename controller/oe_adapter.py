"""The one module that drives OpenEvolve 0.4.0's internals, for staged campaigns (M5).

openevolve-run does one iteration at a time in one process: sample a parent, ask
the LLM, evaluate the child, add it. A campaign (controller/campaign.py) splits
that loop across jobs, so it needs the pieces separately. They are all here,
mirroring OpenEvolve's own code, so an OpenEvolve upgrade touches one file:

    load_config        the resolved config, with this job's server address
    new_database, load_database, save_checkpoint   standard OpenEvolve checkpoints
    sample             a parent and inspirations from one island (process_parallel._submit_iteration)
    build_prompt       as process_parallel._run_iteration_worker
    generate           all prompts at once; one LLMEnsemble per request, so token usage stays per request
    parse_rewrite      a full rewrite from the reply, as the worker
    add_program        database.add plus artifacts, prompt log and island bookkeeping (the controller loop)

Two deliberate differences from openevolve-run:
  * enforce_evolve_blocks is not applied: in 0.4.0 it never reaches the workers
    either, and the evaluator rejects edits outside the block (contract §1).
  * the database's sampling seed is the configured one plus the generation, so
    each generation samples differently but reproducibly.

    python -m controller.oe_adapter --selftest     a small generational loop with a stand-in LLM
"""

import asyncio
import json
import os
import random
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import openevolve
from openevolve.config import load_config as _load_config
from openevolve.database import Program, ProgramDatabase
from openevolve.llm.ensemble import LLMEnsemble
from openevolve.prompt.sampler import PromptSampler
from openevolve.utils.code_utils import parse_full_rewrite
from openevolve.utils.metrics_utils import safe_numeric_average

OPENEVOLVE_VERSION = "0.4.0"
PROMPT_TEMPLATE_KEY = "full_rewrite_user"


def check_version():
    if openevolve.__version__ != OPENEVOLVE_VERSION:
        raise RuntimeError(f"controller/oe_adapter.py mirrors OpenEvolve {OPENEVOLVE_VERSION}'s internals, but "
                           f"{openevolve.__version__} is installed; re-check it (--selftest) before using it")


def load_config(path, api_base=None):
    """The resolved OpenEvolve config; with api_base, every model talks to this job's server."""
    check_version()
    os.environ.setdefault("AFAR_LLM_API_KEY", "no-server-in-this-stage")
    config = _load_config(str(path))
    if api_base:
        config.llm.api_base = api_base
        for model in config.llm.models + config.llm.evaluator_models:
            model.api_base = api_base
    return config


def _seed(config, generation):
    if config.database.random_seed is not None:
        random.seed(config.database.random_seed + generation)


def new_database(config, generation=0):
    db = ProgramDatabase(config.database)
    _seed(config, generation)
    return db


def load_database(config, checkpoint, generation):
    db = ProgramDatabase(config.database)
    db.load(str(checkpoint))
    _seed(config, generation)
    return db


def save_checkpoint(db, checkpoints_dir, iteration, suffix):
    """checkpoints_dir/checkpoint_<iteration>, as OpenEvolve writes it, renamed into place when complete."""
    checkpoints_dir = Path(checkpoints_dir)
    final = checkpoints_dir / f"checkpoint_{iteration}"
    partial = checkpoints_dir / f".checkpoint_{iteration}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    db.save(str(partial), iteration)
    best = db.get(db.best_program_id) if db.best_program_id else db.get_best_program()
    if best:
        (partial / f"best_program{suffix}").write_text(best.code)
        (partial / "best_program_info.json").write_text(json.dumps({
            "id": best.id, "generation": best.generation, "iteration": best.iteration_found,
            "current_iteration": iteration, "metrics": best.metrics, "language": best.language,
            "timestamp": best.timestamp, "saved_at": time.time()}, indent=2))
    shutil.rmtree(final, ignore_errors=True)
    os.replace(partial, final)
    return final


def num_islands(db):
    return len(db.islands)


def sample(db, config, island):
    return db.sample_from_island(island_id=island, num_inspirations=config.prompt.num_diverse_programs)


def _score(program):
    return program.metrics.get("combined_score", safe_numeric_average(program.metrics))


def build_prompt(sampler, db, config, parent, inspirations, iteration):
    """The prompt OpenEvolve's worker would build for this parent (process_parallel._run_iteration_worker)."""
    parent_island = parent.metadata.get("island", db.current_island)
    island_programs = sorted((db.programs[pid] for pid in db.islands[parent_island] if pid in db.programs),
                             key=_score, reverse=True)
    top = config.prompt.num_top_programs
    prompt = sampler.build_prompt(
        current_program=parent.code, parent_program=parent.code, program_metrics=parent.metrics,
        previous_programs=[p.to_dict() for p in island_programs[:top]],
        top_programs=[p.to_dict() for p in island_programs[:top + config.prompt.num_diverse_programs]],
        inspirations=[p.to_dict() for p in inspirations], language=config.language,
        evolution_round=iteration, diff_based_evolution=config.diff_based_evolution,
        program_artifacts=db.get_artifacts(parent.id) or None,
        feature_dimensions=db.config.feature_dimensions, current_changes_description=None)
    return prompt, parent_island


def new_sampler(config):
    return PromptSampler(config.prompt)


async def _ask(models, prompt, client=None):
    started = time.monotonic()
    try:
        if client is not None:
            text, usage = await client(prompt)
        else:
            ensemble = LLMEnsemble(models)
            text = await ensemble.generate_with_context(
                system_message=prompt["system"], messages=[{"role": "user", "content": prompt["user"]}])
            usage = getattr(ensemble, "last_usage", None)
        return {"response": text, "token_usage": usage, "seconds": round(time.monotonic() - started, 2), "error": None}
    except Exception as e:  # one failed request rejects one candidate, not the generation
        return {"response": None, "token_usage": None, "seconds": round(time.monotonic() - started, 2),
                "error": f"LLM generation failed: {type(e).__name__}: {e}"}


def generate(config, prompts, client=None):
    """Send every prompt at once (vLLM batches them); one result dict per prompt, in order."""
    async def run():
        return await asyncio.gather(*(_ask(config.llm.models, p, client) for p in prompts))
    return asyncio.run(run())


def parse_rewrite(response, language):
    return parse_full_rewrite(response, language) if response else None


def add_program(db, config, *, code, parent_id, parent_generation, parent_metrics, parent_island, target_island,
                iteration, metrics, artifacts=None, prompt=None, response=None, token_usage=None, seed=False):
    """Add one evaluated program, as OpenEvolve's controller loop does; its id, or None if the database refused it."""
    program = Program(
        id=str(uuid.uuid4()), code=code, language=config.language,
        parent_id=None if seed else parent_id, generation=0 if seed else parent_generation + 1,
        metrics=metrics, iteration_found=iteration,
        metadata={} if seed else {"changes": "Full rewrite", "parent_metrics": parent_metrics,
                                  "island": parent_island, "token_usage": token_usage})
    admitted = db.add(program, iteration=iteration, target_island=target_island)
    if admitted is None:
        return None
    if artifacts:
        db.store_artifacts(program.id, artifacts)
    if prompt:
        db.log_prompt(program_id=program.id, template_key=PROMPT_TEMPLATE_KEY, prompt=dict(prompt),
                      responses=[response] if response else [], token_usage=token_usage)
    if not seed:
        db.increment_island_generation(island_idx=parent_island)
        if db.should_migrate():
            db.migrate_programs()
    return program.id


# --- selftest ---------------------------------------------------------------

def selftest():
    """Two generations through every function above, with a stand-in LLM; then OpenEvolve reloads the checkpoint."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from controller import runspec

    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<44} {detail}")

    seed = (runspec.ROOT / "seeds" / "evolved_repl.h").read_text()
    variant = seed.replace("std::min_element(begin, end)", "std::max_element(begin, end)")

    async def stand_in(prompt):          # replies with the MRU variant, or the seed itself for an MRU parent
        code = seed if "max_element" in prompt["user"].split("# Current Program")[-1] else variant
        return f"```cpp\n{code}```", {"prompt_tokens": len(prompt["user"]) // 4, "completion_tokens": 900}

    with tempfile.TemporaryDirectory(prefix="afar-oe-adapter-") as tmp:
        tmp = Path(tmp)
        r = runspec.resolve("champsim-compile-v1", cpus=16, template_dir=tmp / "resolved" / "prompt")
        config_path, _ = runspec.write(r, tmp / "resolved")
        config = load_config(config_path, api_base="http://127.0.0.1:9/v1")
        check("version pinned", openevolve.__version__ == OPENEVOLVE_VERSION, openevolve.__version__)
        check("api_base reaches every model", all(m.api_base == "http://127.0.0.1:9/v1" for m in config.llm.models))

        db = new_database(config)
        sid = add_program(db, config, code=seed, parent_id=None, parent_generation=0, parent_metrics={},
                          parent_island=0, target_island=0, iteration=0, metrics={"combined_score": 1.0}, seed=True)
        ckpt = save_checkpoint(db, tmp / "checkpoints", 0, ".h")
        check("seed checkpoint", (ckpt / "metadata.json").exists() and (ckpt / "best_program.h").exists())

        iteration = 0
        for generation in (1, 2):
            db = load_database(config, ckpt, generation)
            sampler = new_sampler(config)
            picks = [sample(db, config, i % num_islands(db)) + (i % num_islands(db),) for i in range(3)]
            prompts = [build_prompt(sampler, db, config, parent, insp, iteration + 1 + i)
                       for i, (parent, insp, _) in enumerate(picks)]
            replies = generate(config, [p for p, _ in prompts], client=stand_in)
            check(f"generation {generation}: 3 prompts, 3 replies",
                  len(replies) == 3 and all(x["response"] for x in replies),
                  f"system message {len(prompts[0][0]['system'])} chars")
            for i, ((parent, _, island), (prompt, parent_island), reply) in enumerate(zip(picks, prompts, replies)):
                iteration += 1
                code = parse_rewrite(reply["response"], config.language)
                if code is None or code == parent.code:
                    continue
                add_program(db, config, code=code, parent_id=parent.id, parent_generation=parent.generation,
                            parent_metrics=parent.metrics, parent_island=parent_island, target_island=island,
                            iteration=iteration, metrics={"combined_score": 1.0 + 0.01 * iteration},
                            artifacts={"status": "completed"}, prompt=prompt, response=reply["response"],
                            token_usage=reply["token_usage"])
            ckpt = save_checkpoint(db, tmp / "checkpoints", iteration, ".h")

        # OpenEvolve's own loader reads the campaign's checkpoint.
        reloaded = ProgramDatabase(config.database)
        reloaded.load(str(ckpt))
        meta = json.loads((ckpt / "metadata.json").read_text())
        check("OpenEvolve reloads the last checkpoint", len(reloaded.programs) == len(db.programs) > 1,
              f"{len(reloaded.programs)} programs, last_iteration {meta['last_iteration']}")
        logged = [f for f in (ckpt / "programs").glob("*.json")
                  if (json.loads(f.read_text()).get("prompts") or {}).get(PROMPT_TEMPLATE_KEY)]
        check("children's prompts saved in the checkpoint", len(logged) == len(db.programs) - 1, f"{len(logged)} programs")
        check("no partial checkpoints left", not list((tmp / "checkpoints").glob(".*partial")))
    print("oe_adapter selftest passed" if failures == 0 else f"oe_adapter selftest FAILED: {failures} case(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--selftest"]:
        sys.exit("usage: python -m controller.oe_adapter --selftest")
    sys.exit(selftest())
