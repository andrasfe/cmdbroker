#!/usr/bin/env python3
"""Host command-broker web service.

A small stdlib-only HTTP service that executes shell commands on the host on
behalf of callers running inside docker sandboxes (which cannot run those
commands from their own restricted environment).

If a virtualenv exists in the working directory (./venv or ./.venv), its bin
directory is prepended to PATH and VIRTUAL_ENV is set, so commands run as if the
venv were activated.

Security: this is a remote code execution endpoint by design. It binds to
127.0.0.1 by default. Set CLI_SERVE_TOKEN to require a shared-secret bearer
token; if unset, auth is disabled (a warning is printed). Do not expose it on a
public interface.

Usage:
    [CLI_SERVE_TOKEN=secret] python3 cli_serve.py [--host H] [--port P] [--workdir D]

Request (POST /run):
    {
      "command": "pytest -q",        # string (run via shell) OR
      "command": ["pytest", "-q"],   # list (run without shell)
      "cwd": "optional/subdir",      # optional, relative to --workdir
      "timeout": 300,                 # optional seconds, default 600
      "env": {"KEY": "val"}           # optional extra env vars
    }

Response:
    {"exit_code": 0, "stdout": "...", "stderr": "...", "timed_out": false,
     "duration_sec": 1.23, "venv": "/abs/path/.venv" | null}
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 1 << 20  # 1 MiB request cap
DEFAULT_TIMEOUT = 600


def find_venv(workdir: str) -> str | None:
    """Return absolute path to a venv in workdir (venv or .venv), else None."""
    for name in ("venv", ".venv"):
        candidate = os.path.join(workdir, name)
        marker = os.path.join(candidate, "bin", "activate")  # posix
        win_marker = os.path.join(candidate, "Scripts", "activate")
        if os.path.isfile(marker) or os.path.isfile(win_marker):
            return os.path.abspath(candidate)
    return None


def build_env(workdir: str, extra: dict | None) -> tuple[dict, str | None]:
    """Construct the child environment, activating a venv if present."""
    env = os.environ.copy()
    venv = find_venv(workdir)
    if venv:
        bindir = os.path.join(venv, "Scripts" if os.name == "nt" else "bin")
        env["VIRTUAL_ENV"] = venv
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env.pop("PYTHONHOME", None)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env, venv


class Handler(BaseHTTPRequestHandler):
    server_version = "cli-serve/1.0"
    # injected by main()
    token: str = ""
    workdir: str = "."

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.token:  # auth disabled when no token is configured
            return True
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        # constant-time-ish comparison
        if len(header) != len(expected):
            return False
        return all(a == b for a, b in zip(header, expected))

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "workdir": self.workdir})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "request body too large"})
            return
        try:
            raw = self.rfile.read(length)
            req = json.loads(raw or b"{}")
        except (ValueError, OSError) as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return

        command = req.get("command")
        if not command:
            self._send(400, {"error": "missing 'command'"})
            return
        use_shell = isinstance(command, str)
        if not use_shell and not (
            isinstance(command, list) and all(isinstance(x, str) for x in command)
        ):
            self._send(400, {"error": "'command' must be a string or list of strings"})
            return

        # Resolve cwd, confined to workdir.
        cwd = self.workdir
        sub = req.get("cwd")
        if sub:
            resolved = os.path.abspath(os.path.join(self.workdir, sub))
            base = os.path.abspath(self.workdir)
            if os.path.commonpath([base, resolved]) != base:
                self._send(400, {"error": "'cwd' escapes workdir"})
                return
            cwd = resolved

        timeout = req.get("timeout", DEFAULT_TIMEOUT)
        env, venv = build_env(cwd, req.get("env"))

        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                command,
                shell=use_shell,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            exit_code = proc.returncode
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + f"\n[timed out after {timeout}s]"
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
        except (OSError, ValueError) as exc:
            self._send(500, {"error": f"failed to execute: {exc}"})
            return

        self._send(
            200,
            {
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": timed_out,
                "duration_sec": round(time.monotonic() - start, 3),
                "venv": venv,
                "cwd": cwd,
            },
        )

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(
            "%s - %s\n" % (self.address_string(), fmt % args)
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765,
                        help="bind port (default: 8765)")
    parser.add_argument("--workdir", default=os.getcwd(),
                        help="base directory commands run in (default: cwd)")
    args = parser.parse_args()

    token = os.environ.get("CLI_SERVE_TOKEN", "")
    if not token:
        sys.stderr.write(
            "WARNING: CLI_SERVE_TOKEN not set — auth is DISABLED. Anyone who can "
            "reach this port can run commands as you. Only acceptable on a "
            "trusted/localhost-only bind.\n"
        )

    Handler.token = token
    Handler.workdir = os.path.abspath(args.workdir)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write(
            f"WARNING: binding to {args.host} exposes a remote-code-execution "
            "endpoint beyond localhost. Ensure the network is trusted.\n"
        )

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(
        f"cli-serve listening on {args.host}:{args.port} "
        f"(workdir={Handler.workdir})\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nshutting down\n")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
