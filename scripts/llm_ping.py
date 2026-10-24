"""R5: check the LLM server through OpenEvolve's own client, then measure throughput.

Run inside a GPU job after slurm/llm_serve.sh's llm_start and llm_wait_ready:
    .venv/controller/bin/python scripts/llm_ping.py \
        --config configs/openevolve/toy.yaml --api-base "$AFAR_LLM_API_BASE"

The API key comes from the config, which references ${AFAR_LLM_API_KEY}.
Exits non-zero if any check fails.
"""

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import openai
from openevolve.config import load_config
from openevolve.llm.ensemble import LLMEnsemble

PROMPT = "Write a Python function total(xs) that returns the sum of a list of numbers. Reply with only the code."


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=120,
                        help="per-request timeout in seconds (fail fast inside a short job)")
    args = parser.parse_args()

    config = load_config(args.config)
    models = config.llm.models
    # Set the address on each model directly: OpenEvolve's --api-base alone
    # does not reach the per-model configs (cli.py only rebuilds them with
    # --primary-model).
    for model in models:
        model.api_base = args.api_base
        model.timeout = args.timeout
        model.retries = 1
    name, key = models[0].name, models[0].api_key

    # 1. The server lists the model under the name OpenEvolve asks for.
    request = urllib.request.Request(f"{args.api_base}/models", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        ids = [m["id"] for m in json.load(response)["data"]]
    print(f"models: {ids}")
    if name not in ids:
        sys.exit(f"FAIL: server does not list {name!r}")

    # 2. One prompt through OpenEvolve's LLMEnsemble (the path evolution uses).
    ensemble = LLMEnsemble(models)
    start = time.monotonic()
    reply = asyncio.run(ensemble.generate(PROMPT))
    seconds = time.monotonic() - start
    usage = ensemble.last_usage or {}
    tokens = usage.get("completion_tokens", 0)
    print(f"--- reply via OpenEvolve ({seconds:.2f}s, {tokens} completion tokens, "
          f"{tokens / seconds:.1f} tokens/s):\n{reply}\n---")
    if not reply.strip():
        sys.exit("FAIL: empty reply")

    # 3. Concurrent requests with the same openai library, for per-response
    #    usage (the ensemble keeps a single last_usage that parallel calls overwrite).
    client = openai.OpenAI(base_url=args.api_base, api_key=key, timeout=args.timeout, max_retries=1)

    def one(_):
        result = client.chat.completions.create(
            model=name, messages=[{"role": "user", "content": PROMPT}], max_tokens=256, temperature=0.7)
        return result.usage.completion_tokens

    start = time.monotonic()
    with ThreadPoolExecutor(args.concurrency) as pool:
        counts = list(pool.map(one, range(args.concurrency)))
    seconds = time.monotonic() - start
    print(f"concurrency {args.concurrency}: {sum(counts)} completion tokens in {seconds:.2f}s "
          f"= {sum(counts) / seconds:.1f} tokens/s overall")
    print("LLM ping OK")


if __name__ == "__main__":
    main()
