"""Load OpenEvolve configs with OpenEvolve's own loader and print what it resolved.

Usage (via `make oe-check-config`):
    .venv/controller/bin/python scripts/oe_check_config.py configs/openevolve/*.yaml

Two checks guard against silent mistakes that OpenEvolve's loader allows:
  * load_config() quietly falls back to defaults when the file does not exist.
  * dacite ignores keys it does not recognize, so a typo such as `manaul_mode`
    would be dropped without warning.
"""

import sys
from pathlib import Path

import yaml
from openevolve.config import Config, load_config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from controller.oe_config import summarize, unknown_keys  # noqa: E402


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
        print(summarize(load_config(str(path))))
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
