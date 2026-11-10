"""Command-line interface for AetherOps.

Usage (from the repository root)::

    python -m src.cli verify --common-name db-0.cluster.local --san db-0
    python -m src.cli start --interval 2.5 --nodes 3 --failure-rate 0.1

Exit codes: 0 success, 1 operational failure, 2 usage error, 130 interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from . import __version__
from .orchestrator import ClusterController, ControllerConfig, build_simulated_cluster
from .sys_admin import (
    DEFAULT_REQUIRED_PACKAGES,
    SUPPORTED_KEY_SIZES,
    SysAdminError,
    SystemAdminEngine,
    VerificationReport,
)

logger = logging.getLogger("aetherops.cli")

EXIT_OK: Final[int] = 0
EXIT_FAILURE: Final[int] = 1
EXIT_USAGE: Final[int] = 2
EXIT_INTERRUPTED: Final[int] = 130

LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

Handler = Callable[[argparse.Namespace], int]


# --------------------------------------------------------------------------- #
# argparse value types
# --------------------------------------------------------------------------- #
def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from exc
    if not number > 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be > 0")
    return number


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value!r} must be >= 1")
    return number


def probability(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from exc
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError(f"{value!r} must be within [0, 1]")
    return number


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aetherops",
        description="Distributed database node provisioner and self-healing controller.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=LOG_LEVELS,
        default="INFO",
        help="logging verbosity (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    verify = sub.add_parser(
        "verify",
        help="validate the host (os-release, apt packages) and generate TLS material",
        description="Validate a Debian-family host and generate a self-signed TLS certificate.",
    )
    verify.add_argument(
        "--cert-dir", type=Path, default=Path("certs"),
        help="output directory for certificate material (default: %(default)s)",
    )
    verify.add_argument(
        "--common-name", default="localhost",
        help="certificate CN (default: %(default)s)",
    )
    verify.add_argument(
        "--san", action="append", default=[], metavar="DNS",
        help="additional subjectAltName DNS entry (repeatable)",
    )
    verify.add_argument(
        "--days", type=positive_int, default=365,
        help="certificate validity in days (default: %(default)s)",
    )
    verify.add_argument(
        "--key-size", type=int, choices=sorted(SUPPORTED_KEY_SIZES), default=2048,
        help="RSA key size (default: %(default)s)",
    )
    verify.add_argument(
        "--package", dest="packages", action="append", metavar="NAME",
        help=f"required package (repeatable; default: {' '.join(DEFAULT_REQUIRED_PACKAGES)})",
    )
    verify.add_argument(
        "--skip-os-check", action="store_true",
        help="skip the /etc/os-release Debian-family check",
    )
    verify.add_argument(
        "--overwrite", action="store_true",
        help="regenerate the certificate even if one already exists",
    )
    verify.set_defaults(handler=cmd_verify)

    start = sub.add_parser(
        "start",
        help="run the cluster controller daemon",
        description="Run the asyncio monitoring loop with automated failover and self-healing.",
    )
    start.add_argument(
        "--interval", type=positive_float, default=5.0,
        help="seconds between reconcile cycles (default: %(default)s)",
    )
    start.add_argument(
        "--nodes", type=positive_int, default=3,
        help="number of simulated nodes (default: %(default)s)",
    )
    start.add_argument(
        "--failure-rate", type=probability, default=0.05,
        help="per-probe simulated crash probability (default: %(default)s)",
    )
    start.add_argument(
        "--failure-threshold", type=positive_int, default=3,
        help="consecutive failed probes before a node is unhealthy (default: %(default)s)",
    )
    start.add_argument(
        "--health-timeout", type=positive_float, default=2.0,
        help="per-probe timeout in seconds (default: %(default)s)",
    )
    start.add_argument(
        "--max-restarts", type=int, default=5,
        help="restarts before a node is quarantined (default: %(default)s)",
    )
    start.add_argument(
        "--max-cycles", type=positive_int, default=None,
        help="stop after N cycles (default: run until interrupted)",
    )
    start.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    start.set_defaults(handler=cmd_start)

    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr, force=True)


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _print_report(report: VerificationReport) -> None:
    status = "PASS" if report.ok else "FAIL"
    print(f"AetherOps host verification: {status}")
    if report.os_release is not None:
        print(f"  platform   : {report.os_release.pretty_name}")
    if report.packages is not None:
        for name, version in report.packages.installed.items():
            print(f"  [ok]       {name} {version}")
        for name in report.packages.missing:
            candidate = report.packages.candidates.get(name) or "no candidate"
            print(f"  [missing]  {name} (apt candidate: {candidate})")
    if report.certificate is not None:
        origin = "reused" if report.certificate.reused else "generated"
        print(f"  tls cert   : {report.certificate.cert_path} ({origin})")
        print(f"  tls key    : {report.certificate.key_path}")
    for error in report.errors:
        print(f"  error      : {error}")


def cmd_verify(args: argparse.Namespace) -> int:
    packages: Sequence[str] = args.packages or DEFAULT_REQUIRED_PACKAGES
    engine = SystemAdminEngine(cert_dir=args.cert_dir, required_packages=packages)
    report = engine.run_verification(
        args.common_name,
        check_os=not args.skip_os_check,
        days=args.days,
        key_size=args.key_size,
        san_dns=args.san,
        overwrite=args.overwrite,
    )
    _print_report(report)
    return EXIT_OK if report.ok else EXIT_FAILURE


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #
async def run_daemon(controller: ClusterController, max_cycles: int | None = None) -> None:
    """Run the controller, stopping gracefully on SIGINT/SIGTERM where supported."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows' Proactor loop has no add_signal_handler; Ctrl+C then
            # surfaces as KeyboardInterrupt, handled by cmd_start.
            continue
        installed.append(sig)

    try:
        await controller.run(stop_event=stop, max_cycles=max_cycles)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def cmd_start(args: argparse.Namespace) -> int:
    try:
        config = ControllerConfig(
            interval=args.interval,
            failure_threshold=args.failure_threshold,
            health_timeout=args.health_timeout,
            max_restarts=args.max_restarts,
        )
        nodes = build_simulated_cluster(args.nodes, failure_rate=args.failure_rate, seed=args.seed)
        controller = ClusterController(nodes, config)
    except ValueError as exc:
        logger.error("invalid configuration: %s", exc)
        return EXIT_USAGE

    try:
        asyncio.run(run_daemon(controller, max_cycles=args.max_cycles))
    except KeyboardInterrupt:
        logger.info("interrupted; controller shut down after %d cycles", controller.cycles)
        return EXIT_INTERRUPTED

    logger.info("final cluster state: %s", controller.snapshot())
    return EXIT_OK


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    handler: Handler = args.handler
    try:
        return handler(args)
    except SysAdminError as exc:
        logger.error("%s", exc)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
