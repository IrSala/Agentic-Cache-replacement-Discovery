"""Checks on OpenEvolve configs that OpenEvolve's own loader doesn't make.

  * load_config() quietly falls back to defaults when the file does not exist.
  * dacite ignores keys it does not recognize, so a typo such as `manaul_mode`
    would be dropped without warning.
"""

import dataclasses
import typing


def unknown_keys(cls, data, prefix=""):
    """Yield dotted paths of keys in `data` that are not fields of dataclass `cls`."""
    fields = {f.name: f for f in dataclasses.fields(cls)}
    for key, value in data.items():
        path = f"{prefix}{key}"
        field = fields.get(key)
        if field is None:
            yield path
            continue
        ftype = field.type
        if dataclasses.is_dataclass(ftype) and isinstance(value, dict):
            yield from unknown_keys(ftype, value, path + ".")
        elif typing.get_origin(ftype) in (list, typing.List) and isinstance(value, list):
            (item_type,) = typing.get_args(ftype)
            if dataclasses.is_dataclass(item_type):
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        yield from unknown_keys(item_type, item, f"{path}[{i}].")


def summarize(cfg):
    """One line per section of a loaded openevolve.config.Config."""
    lines = [f"  iterations={cfg.max_iterations} checkpoint_interval={cfg.checkpoint_interval} "
             f"language={cfg.language} file_suffix={cfg.file_suffix} diff_based={cfg.diff_based_evolution} "
             f"max_code_length={cfg.max_code_length}"]
    for m in cfg.llm.models:
        lines.append(f"  model {m.name!r} weight={m.weight} manual_mode={m.manual_mode} "
                     f"api_base={m.api_base} temperature={m.temperature} max_tokens={m.max_tokens} timeout={m.timeout}")
    ev = cfg.evaluator
    lines.append(f"  evaluator timeout={ev.timeout} max_retries={ev.max_retries} cascade={ev.cascade_evaluation} "
                 f"thresholds={ev.cascade_thresholds} parallel={ev.parallel_evaluations}")
    p = cfg.prompt
    lines.append(f"  prompt template_dir={p.template_dir} system_message={p.system_message[:40]!r} "
                 f"top={p.num_top_programs} diverse={p.num_diverse_programs} artifacts={p.include_artifacts}")
    db = cfg.database
    lines.append(f"  database population={db.population_size} islands={db.num_islands} log_prompts={db.log_prompts}")
    return "\n".join(lines)
