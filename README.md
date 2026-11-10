# AetherOps

A Python tool that prepares Debian/Ubuntu hosts for database nodes and keeps a primary/replica cluster available through automatic failover.

Standard library only. Python 3.10+. 73 tests. Passes `mypy --strict`.

> The cluster is simulated: nodes crash at a configurable rate so the failover logic can be exercised and tested. Connecting it to real PostgreSQL instances is the next step.

## What it does

**1. Host verification (`aetherops verify`)**

- Reads `/etc/os-release` and refuses anything that isn't Debian or Ubuntu
- Checks required packages (`openssl`, `ca-certificates`, `postgresql-client` by default) with `dpkg-query`, retrying with backoff on transient failures
- Looks up install candidates with `apt-cache policy` and prints the `apt-get install` command for anything missing
- Generates a self-signed TLS certificate and key with OpenSSL, including subjectAltName entries
- Simulates systemd unit control

**2. Self-healing controller (`aetherops start`)**

- An asyncio loop health-checks every node concurrently, each with its own timeout
- A node is marked unhealthy only after N consecutive failed checks, so one network blip doesn't trigger a failover
- When the primary fails, the controller fences it and promotes the healthy replica with the lowest replication lag
- Crashed nodes are restarted; a node that keeps failing is quarantined after a set number of restarts

## Quick start

```bash
python -m pip install -r requirements-dev.txt   # pytest and mypy, for development only

# Validate a Debian/Ubuntu host and generate certificates into ./certs
python -m src.cli verify --common-name db-0.internal --san db-0 --san localhost

# Run the controller: 3 nodes, 15% crash rate, 30 cycles, reproducible
python -m src.cli start --interval 0.4 --nodes 3 --failure-rate 0.15 \
    --failure-threshold 2 --seed 7 --max-cycles 30
```

On Windows, `run_demo.bat` runs the tests, type check, host check and controller in one go.

### Example run

With the command above (seed 7), the controller finished 30 cycles with 3 automatic failovers, and all three nodes ended healthy:

```
WARNING  FAILOVER #1: db-0 -> db-2 (replication lag 0.386s)
WARNING  FAILOVER #2: db-2 -> db-0 (replication lag 1.841s)
WARNING  FAILOVER #3: db-0 -> db-2 (replication lag 1.478s)
INFO     controller stopped after 30 cycles, 3 failovers
```

## Command-line options

| `start` option | Default | Meaning |
|---|---|---|
| `--interval` | 5.0 | Seconds between reconcile cycles |
| `--nodes` | 3 | Number of simulated nodes |
| `--failure-rate` | 0.05 | Chance each health check sees a crash |
| `--failure-threshold` | 3 | Consecutive failed checks before a node is unhealthy |
| `--health-timeout` | 2.0 | Timeout for each health check, in seconds |
| `--max-restarts` | 5 | Restarts before a node is quarantined |
| `--max-cycles` | none | Stop after N cycles |
| `--seed` | none | Random seed for reproducible runs |

`verify` options: `--cert-dir`, `--common-name`, `--san` (repeatable), `--days`, `--key-size` (2048/3072/4096), `--package` (repeatable), `--skip-os-check`, `--overwrite`.

Exit codes: `0` success, `1` failure (e.g. a host check failed), `2` bad arguments, `130` interrupted. CI jobs can act on these directly.

## Testing

```bash
python -m pytest        # 73 tests; subprocess and file access are mocked
python -m mypy          # strict mode, configured in pyproject.toml
```

## Project layout

```
src/
  sys_admin.py      host checks, package validation, TLS generation
  orchestrator.py   asyncio health checks, failover, restart and quarantine
  cli.py            argparse command-line interface
tests/              pytest suites for each module
```

## Author

Hemanth Gorapalli
