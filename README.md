# cmdbroker

A tiny host-side command broker for docker-sandboxed programs.

A sandboxed container often can't run something it needs — host tooling, a real
virtualenv, GPU access, privileged network. `cmdbroker` runs a small HTTP service
**on the host** (outside docker) that executes commands on the container's
behalf and returns the output.

> ⚠️ **This is a remote-code-execution endpoint by design.** It runs commands
> with the host user's full privileges. Require the token, keep it on a trusted
> network, and never expose it publicly.

## Components

| File                 | Runs where      | Purpose                                              |
| -------------------- | --------------- | ---------------------------------------------------- |
| `cli_serve.py`       | host (outside)  | HTTP broker that executes commands                   |
| `cli_client.py`      | inside container| stdlib-only client / drop-in command runner          |
| `Dockerfile.example` | —               | example sandbox using the client                     |

Both scripts are **stdlib-only** — no pip installs anywhere.

## Quick start

Start the broker on the host (token is optional — omit it for a localhost-only,
auth-disabled run):

```bash
CLI_SERVE_TOKEN=yoursecret python3 cli_serve.py --workdir /path/to/project
# listens on 127.0.0.1:8765 by default
```

If a `venv` or `.venv` exists in the working directory, the broker activates it
(prepends its `bin` to `PATH`, sets `VIRTUAL_ENV`) so `python`/`pytest`/etc.
resolve to that environment.

Call it from inside a container:

```bash
export CLI_SERVE_URL=http://host.docker.internal:8765
export CLI_SERVE_TOKEN=yoursecret

python3 cli_client.py -- pytest -q                # argv form (no shell on host)
python3 cli_client.py --shell "ls | grep py"      # shell form
python3 cli_client.py --cwd pkg --timeout 120 -- make build
python3 cli_client.py --health
```

The client mirrors the remote result: stdout→stdout, stderr→stderr, and the
remote exit code becomes the client's exit code. Timeout → `124`,
transport/auth error → `125`.

Or use it as a library:

```python
from cli_client import run
res = run(["pytest", "-q"], cwd="pkg", timeout=300)
print(res["stdout"], res["exit_code"])
```

## HTTP API

`POST /run` — `Authorization: Bearer <token>`

```json
{
  "command": "pytest -q",        // string (shell) OR ["pytest","-q"] (no shell)
  "cwd": "subdir",                // optional, confined to broker --workdir
  "timeout": 300,                 // optional seconds, default 600
  "env": {"KEY": "val"}           // optional extra env vars
}
```

Response:

```json
{
  "exit_code": 0, "stdout": "...", "stderr": "...",
  "timed_out": false, "duration_sec": 1.23,
  "venv": "/abs/.venv", "cwd": "/abs/cwd"
}
```

`GET /health` → `{"status":"ok","workdir":"..."}`

## Docker networking

- **Docker Desktop (Mac/Windows):** `host.docker.internal` resolves automatically.
- **Linux:** add `--add-host=host.docker.internal:host-gateway` to `docker run`.
- The broker must bind where the container can reach it (`--host 0.0.0.0` or a
  private docker network) instead of `127.0.0.1`. That widens exposure beyond
  localhost — keep it on a trusted network and rely on the token.

See `Dockerfile.example` for a complete sandbox-to-host demo.

## Security notes

- `CLI_SERVE_TOKEN` is optional. If unset, auth is **disabled** and the broker
  prints a warning — only acceptable on a localhost-only / trusted bind.
- Binds to `127.0.0.1` by default; any other bind prints a warning.
- `cwd` is confined to `--workdir` (escape attempts are rejected).
- Commands run as the **host user** — consider running the broker as a
  restricted user if the threat model warrants it.
