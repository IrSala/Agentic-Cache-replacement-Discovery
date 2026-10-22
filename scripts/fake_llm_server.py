"""A tiny stand-in for vLLM's OpenAI-compatible server, for testing job scripts
without a GPU (AFAR_LLM_FAKE=1 in slurm/llm_serve.sh).

Serves GET /health, GET /v1/models and POST /v1/chat/completions on 127.0.0.1.
Like vLLM started with VLLM_API_KEY, the /v1 endpoints require
"Authorization: Bearer <key>" and /health does not. Standard library only.
"""

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = "def total(xs):\n    return sum(xs)"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--served-model-name", required=True)
    parser.add_argument("--exit-immediately", action="store_true",
                        help="simulate a server that dies during start-up")
    parser.add_argument("--startup-delay", type=float, default=0,
                        help="seconds to wait before serving, like a model still loading")
    parser.add_argument("--reply-file",
                        help="answer every chat completion with this file's text, e.g. a reply "
                             "recorded from a real run (default: a short canned reply)")
    args = parser.parse_args()
    reply = open(args.reply_file).read() if args.reply_file else REPLY
    if args.exit_immediately:
        print("fake server: simulated start-up failure", flush=True)
        raise SystemExit(1)
    time.sleep(args.startup_delay)

    key = os.environ.get("VLLM_API_KEY")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            return key is None or self.headers.get("Authorization") == f"Bearer {key}"

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {})
            if self.path == "/v1/models":
                if not self._authorized():
                    return self._send(401, {"error": {"message": "Unauthorized"}})
                model = {"id": args.served_model_name, "object": "model",
                         "created": int(time.time()), "owned_by": "fake"}
                return self._send(200, {"object": "list", "data": [model]})
            self._send(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                return self._send(404, {"error": {"message": "not found"}})
            if not self._authorized():
                return self._send(401, {"error": {"message": "Unauthorized"}})
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if request.get("model") != args.served_model_name:
                return self._send(404, {"error": {"message": f"model {request.get('model')!r} not found"}})
            self._send(200, {
                "id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
                "model": args.served_model_name,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": reply}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32},
            })

        def log_message(self, fmt, *fmt_args):
            print("fake server:", fmt % fmt_args, flush=True)

    print(f"fake server: listening on 127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
