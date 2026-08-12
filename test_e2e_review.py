#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end test: run review_contest.py CLI against a local mock
OpenAI-compatible HTTP server. Verifies the real async aiohttp path:
request, retry-on-429, concurrency, interactive review, JS generation,
cleanup of files.

Usage: python3 test_e2e_review.py
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
FIXTURE = BASE / ".test_tmp" / "e2e_tasks"
PORT = 18999


def main():
    # --- fixture ---
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    (FIXTURE / "A" / "solutions").mkdir(parents=True)
    (FIXTURE / "A" / "statement.md").write_text("s1 statement", encoding="utf-8")
    (FIXTURE / "A" / "solutions" / "123").write_text("s1 code", encoding="utf-8")
    (FIXTURE / "B" / "solutions").mkdir(parents=True)
    (FIXTURE / "B" / "statement.md").write_text("s2 statement", encoding="utf-8")
    (FIXTURE / "B" / "solutions" / "987").write_text("s2 code", encoding="utf-8")

    # --- start server in background process (separate asyncio loop) ---
    server_script = BASE / ".test_e2e_server.py"
    server_script.write_text(
        """
import asyncio, json
from aiohttp import web

LOG = __LOG_PATH__
request_log = []

async def handle(request):
    body = await request.json()
    request_log.append(body)
    with open(LOG, "w") as f:      # persist after every request
        json.dump(request_log, f)
    if len(request_log) == 1:      # first ever request: 429 -> retry
        return web.Response(status=429, text="rate limited")
    content = json.dumps({"percent": 85, "summary": "Good", "remarks": ["x"]})
    return web.json_response({"choices": [{"message": {"content": content}}]})

async def main():
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", __PORT__)
    await site.start()
    print("READY", flush=True)
    await asyncio.sleep(60)

asyncio.run(main())
""".replace("__PORT__", str(PORT)).replace("__LOG_PATH__", repr(str(BASE / ".test_e2e_log.json")))
    )

    proc = subprocess.Popen(
        [sys.executable, str(server_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # wait until READY
    for _ in range(50):
        line = proc.stdout.readline()
        if "READY" in line:
            break
        time.sleep(0.1)
    else:
        print("SERVER DID NOT START")
        proc.kill()
        raise SystemExit(1)

    env = dict(os.environ)
    env["REVIEW_API_KEY"] = "test-key"
    # feed interactive: approve both as OK (+), then quit (q) — actually only 2 units
    stdin = "+\n+\n"

    cmd = [
        sys.executable, str(BASE / "review_contest.py"), "1426",
        "--tasks-dir", str(FIXTURE),
        "--base-url", f"http://127.0.0.1:{PORT}/v1/",
        "--concurrency", "2",
        "--prompt-file", str(BASE / "review_prompt.md"),
        "--delay", "100",
    ]
    result = subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, env=env,
        timeout=120,
    )
    print("=== STDOUT ===")
    print(result.stdout)
    print("=== STDERR ===")
    print(result.stderr)
    print("=== EXIT:", result.returncode, "===")

    # Cleanup of solution files? approved -> files deleted. Check:
    sol_a = FIXTURE / "A" / "solutions" / "123"
    sol_b = FIXTURE / "B" / "solutions" / "987"
    print("solution 123 exists:", sol_a.exists())
    print("solution 987 exists:", sol_b.exists())

    # Assertions
    assert result.returncode == 0, f"exit {result.returncode}"
    assert "JavaScript для админ-панели" in result.stdout
    assert "runId=" in result.stdout and "123" in result.stdout
    assert "Итог:" in result.stdout
    assert not sol_a.exists(), "approved file 123 should be deleted"
    assert not sol_b.exists(), "approved file 987 should be deleted"
    assert "Конкурентность: 2" in result.stdout

    proc.kill()
    proc.wait()
    server_script.unlink(missing_ok=True)

    # Verify the server saw 3 requests: 1 initial (429) + retry + 1 for B
    log_path = BASE / ".test_e2e_log.json"
    if log_path.exists():
        log = json.loads(log_path.read_text(encoding="utf-8"))
        retried = [b for b in log if "s1" in json.dumps(b)]
        print(f"server saw {len(log)} requests; retried s1: {len(retried) > 0}")
        assert len(log) == 3, f"expected 3 requests, got {len(log)}"
        # first request must have been the 429 (which got retried)
        log_path.unlink(missing_ok=True)
    print("\nE2E TEST PASSED")


if __name__ == "__main__":
    main()