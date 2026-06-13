#!/usr/bin/env python3
"""Client helper for the cli-serve host command broker.

Lets a docker-sandboxed program offload a command to the host service. Stdlib
only, so it runs in a minimal container with no pip installs.

Config via env (overridable by flags):
    CLI_SERVE_URL    base URL of the service (default: http://host.docker.internal:8765)
    CLI_SERVE_TOKEN  shared secret (optional; omit if the broker has no token)

CLI usage:
    python3 cli_client.py pytest -q
    python3 cli_client.py --cwd subdir --timeout 120 -- make build
    python3 cli_client.py --shell "ls -la | grep py"
    python3 cli_client.py --health

The process mirrors the remote result: stdout->stdout, stderr->stderr, and the
remote exit code becomes this process's exit code, so it behaves like a drop-in
command runner. A timeout exits 124; a transport error exits 125.

Library usage:
    from cli_client import run
    res = run(["pytest", "-q"], cwd="pkg", timeout=300)
    print(res["stdout"], res["exit_code"])
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://host.docker.internal:8765"


def run(
    command,
    *,
    url: str | None = None,
    token: str | None = None,
    cwd: str | None = None,
    timeout: int = 600,
    env: dict | None = None,
    connect_timeout: float | None = None,
) -> dict:
    """Send a command to the broker and return the parsed result dict.

    `command` is a string (run via shell on the host) or a list of args.
    Raises RuntimeError on transport/auth errors.
    """
    base = (url or os.environ.get("CLI_SERVE_URL") or DEFAULT_URL).rstrip("/")
    tok = token or os.environ.get("CLI_SERVE_TOKEN")

    payload: dict = {"command": command, "timeout": timeout}
    if cwd:
        payload["cwd"] = cwd
    if env:
        payload["env"] = env

    headers = {"Content-Type": "application/json"}
    if tok:  # broker may run without auth; only send a token if we have one
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(
        f"{base}/run",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    # Local network timeout for the HTTP call: a bit beyond the command timeout.
    net_timeout = connect_timeout if connect_timeout is not None else timeout + 30
    try:
        with urllib.request.urlopen(req, timeout=net_timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"broker returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach broker at {base}: {exc.reason}") from exc


def health(url: str | None = None, token: str | None = None) -> dict:
    base = (url or os.environ.get("CLI_SERVE_URL") or DEFAULT_URL).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach broker at {base}: {exc.reason}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", help="broker base URL (default: $CLI_SERVE_URL)")
    parser.add_argument("--cwd", help="run in this subdir (relative to broker workdir)")
    parser.add_argument("--timeout", type=int, default=600, help="command timeout (s)")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VAL",
                        help="extra env var for the command (repeatable)")
    parser.add_argument("--shell", metavar="CMDLINE",
                        help="run a single shell command string instead of argv")
    parser.add_argument("--health", action="store_true",
                        help="check broker health and exit")
    parser.add_argument("--json", action="store_true",
                        help="print the raw JSON result instead of streaming output")
    parser.add_argument("cmd", nargs=argparse.REMAINDER,
                        help="command and args (use -- to separate)")
    args = parser.parse_args()

    if args.health:
        try:
            print(json.dumps(health(args.url), indent=2))
            return 0
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 125

    extra_env = {}
    for item in args.env:
        if "=" not in item:
            parser.error(f"--env expects KEY=VAL, got {item!r}")
        k, v = item.split("=", 1)
        extra_env[k] = v

    if args.shell is not None:
        command = args.shell
    else:
        argv = args.cmd
        if argv and argv[0] == "--":
            argv = argv[1:]
        if not argv:
            parser.error("no command given (pass argv, or use --shell)")
        command = argv

    try:
        res = run(
            command,
            url=args.url,
            cwd=args.cwd,
            timeout=args.timeout,
            env=extra_env or None,
        )
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 125

    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res.get("exit_code") == 0 else 1

    sys.stdout.write(res.get("stdout", ""))
    sys.stderr.write(res.get("stderr", ""))
    if res.get("timed_out"):
        return 124
    return res.get("exit_code", 1)


if __name__ == "__main__":
    sys.exit(main())
