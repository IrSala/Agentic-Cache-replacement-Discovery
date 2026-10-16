"""Load OpenEvolve configs with OpenEvolve's own loader and print what it resolved.

Usage (via `make oe-check-config`):
    .venv/controller/bin/python scripts/oe_check_config.py configs/openevolve/*.yaml

Two checks guard against silent mistakes that OpenEvolve's loader allows:
  * load_config() quietly falls back to defaults when the file does not exist.
  * dacite ignores keys it does not recognize, so a typo such as `manaul_mode`
    would be dropped without warning.
"""

import dataclasses
import sys
import typing
from pathlib import Path

import yaml
from openevolve.config import Config, load_config


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
    print(f"  iterations={cfg.max_iterations} checkpoint_interval={cfg.checkpoint_interval} "
          f"language={cfg.language} file_suffix={cfg.file_suffix} diff_based={cfg.diff_based_evolution}")
    for m in cfg.llm.models:
        print(f"  model {m.name!r} weight={m.weight} manual_mode={m.manual_mode} "
              f"api_base={m.api_base} max_tokens={m.max_tokens} timeout={m.timeout}")
    ev = cfg.evaluator
    print(f"  evaluator timeout={ev.timeout} max_retries={ev.max_retries} cascade={ev.cascade_evaluation} "
          f"thresholds={ev.cascade_thresholds} parallel={ev.parallel_evaluations}")
    db = cfg.database
    print(f"  database population={db.population_size} islands={db.num_islands} log_prompts={db.log_prompts}")


def check(path):
    """Print a summary of one config; return True if it is valid."""
    print(path)
    if not path.is_file():
        print("  FAIL: no such file")
        return False
    unknown = list(unknown_keys(Config, yaml.safe_load(path.read_text()) or {}))
    if unknown:
        print(f"  FAIL: unknown keys (OpenEvolve would ignore them): {', '.join(unknown)}")
        return False
    try:
        summarize(load_config(str(path)))
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        return False
    return True


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    results = [check(Path(p)) for p in argv]
    print(f"{sum(results)}/{len(results)} configs valid")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
