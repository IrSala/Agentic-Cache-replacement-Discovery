# Vendored example: function_minimization

These files are an unmodified copy of OpenEvolve's canonical example, used to test OpenEvolve itself (config, loop, checkpoints, LLM serving) before any ChampSim code is involved.

- Source: https://github.com/algorithmicsuperintelligence/openevolve/tree/v0.4.0/examples/function_minimization
- Tag: `v0.4.0`, matching `openevolve==0.4.0` in `env/controller-requirements.in`
- License: Apache-2.0, see `LICENSE.openevolve`

`config.yaml` is kept for reference only. AFAR_HPC runs the example with its own configs in `configs/openevolve/`, which change only the LLM, iteration and parallelism settings.

`requirements.txt` lists `scipy`, but neither `initial_program.py` nor `evaluator.py` imports it, so it is not installed.
