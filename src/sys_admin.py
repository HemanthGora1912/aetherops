"""Host-level system administration for AetherOps database nodes.

The :class:`SystemAdminEngine` prepares a Debian-family host to run a database
node. It:

* reads ``/etc/os-release`` and refuses non-Debian platforms,
* validates required packages through ``dpkg-query`` (with retry/backoff for
  transient failures) and reports ``apt-cache`` install candidates,
* generates a self-signed TLS certificate with SANs through ``openssl``,
* manages service units through a simulated, in-memory systemd.

Every external command is executed via :func:`subprocess.run` with an argument
list (never ``shell=True``) so the module is safe to unit-test by patching
``subprocess.run`` and ``builtins.open``.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

OS_RELEASE_PATH: Final[Path] = Path("/etc/os-release")
DEBIAN_FAMILY_IDS: Final[frozenset[str]] = frozenset({"debian", "ubuntu"})
DEFAULT_REQUIRED_PACKAGES: Final[tuple[str, ...]] = (
    "openssl",
    "ca-certificates",
    "postgresql-client",
)
DPKG_INSTALLED_STATUS: Final[str] = "install ok installed"
DPKG_NOT_FOUND_RC: Final[int] = 1
SUPPORTED_KEY_SIZES: Final[frozenset[int]] = frozenset({2048, 3072, 4096})

# Debian Policy 5.6.1: lowercase alphanumerics plus '+', '-', '.'; min length 2.
_PACKAGE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9+.\-]+$")
_HOSTNAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?=.{1,253}$)(\*\.)?[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$"
)
_UNIT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9:_.@\-]+\.service$")
_APT_CANDIDATE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*Candidate:\s*(\S+)\s*$", re.MULTILINE)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class SysAdminError(Exception):
    """Base class for every error raised by this module."""


class UnsupportedPlatformError(SysAdminError):
    """The host is not a supported Debian-family Linux distribution."""


class CommandExecutionError(SysAdminError):
    """An external command could not be run or exited unsuccessfully."""

    def __init__(self, cmd: Sequence[str], returncode: int | None, detail: str) -> None:
        self.cmd: tuple[str, ...] = tuple(cmd)
        self.returncode = returncode
        self.detail = detail
        rc = "n/a" if returncode is None else str(returncode)
        super().__init__(f"command failed (rc={rc}): {shlex.join(self.cmd)}: {detail}")


class PackageValidationError(SysAdminError):
    """Package state could not be determined."""


class CertificateGenerationError(SysAdminError):
    """TLS material could not be generated."""


class ServiceManagementError(SysAdminError):
    """A service unit operation was rejected."""


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class OSRelease:
    """Parsed subset of ``/etc/os-release``."""

    id: str
    version_id: str
    pretty_name: str
    id_like: tuple[str, ...] = ()

    @property
    def is_debian_family(self) -> bool:
        return self.id in DEBIAN_FAMILY_IDS or any(i in DEBIAN_FAMILY_IDS for i in self.id_like)


@dataclass(frozen=True, slots=True)
class PackageReport:
    """Result of validating a set of Debian packages."""

    installed: Mapping[str, str]
    missing: tuple[str, ...]
    candidates: Mapping[str, str | None] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.missing

    def install_hint(self) -> str | None:
        if not self.missing:
            return None
        return "sudo apt-get install -y " + " ".join(self.missing)


@dataclass(frozen=True, slots=True)
class CertificateBundle:
    """Paths and metadata for a generated certificate/key pair."""

    cert_path: Path
    key_path: Path
    config_path: Path
    common_name: str
    valid_days: int
    reused: bool = False


@dataclass(slots=True)
class VerificationReport:
    """Aggregate outcome of :meth:`SystemAdminEngine.run_verification`."""

    os_release: OSRelease | None = None
    packages: PackageReport | None = None
    certificate: CertificateBundle | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.errors
            and self.packages is not None
            and self.packages.ok
            and self.certificate is not None
        )


class ServiceState(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    FAILED = "failed"


class ServiceAction(str, Enum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    STATUS = "status"


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def parse_os_release(text: str) -> dict[str, str]:
    """Parse ``os-release(5)`` content into a dict, honouring shell quoting."""
    data: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.isidentifier():
            logger.debug("os-release line %d ignored: %r", lineno, raw)
            continue
        try:
            tokens = shlex.split(value, posix=True)
        except ValueError:
            tokens = [value.strip("\"'")]
        data[key] = " ".join(tokens)
    return data


def parse_apt_candidate(policy_output: str) -> str | None:
    """Extract the ``Candidate:`` version from ``apt-cache policy`` output."""
    match = _APT_CANDIDATE_RE.search(policy_output)
    if match is None or match.group(1) == "(none)":
        return None
    return match.group(1)


def render_openssl_config(common_name: str, san_dns: Sequence[str]) -> str:
    """Build an OpenSSL ``req`` config with server/client auth and SANs."""
    names = list(dict.fromkeys([common_name, *san_dns]))
    alt_names = "\n".join(f"DNS.{i} = {name}" for i, name in enumerate(names, start=1))
    return (
        "[req]\n"
        "distinguished_name = req_dn\n"
        "x509_extensions = v3_req\n"
        "prompt = no\n"
        "\n"
        "[req_dn]\n"
        f"CN = {common_name}\n"
        "O = AetherOps\n"
        "\n"
        "[v3_req]\n"
        "basicConstraints = critical,CA:FALSE\n"
        "keyUsage = critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage = serverAuth,clientAuth\n"
        "subjectAltName = @alt_names\n"
        "\n"
        "[alt_names]\n"
        f"{alt_names}\n"
    )


def _validate_hostname(name: str) -> None:
    if not _HOSTNAME_RE.match(name):
        raise ValueError(f"invalid DNS name: {name!r}")


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class SystemAdminEngine:
    """Validates and prepares a Debian-family host for an AetherOps node."""

    def __init__(
        self,
        *,
        cert_dir: Path,
        required_packages: Sequence[str] = DEFAULT_REQUIRED_PACKAGES,
        os_release_path: Path = OS_RELEASE_PATH,
        command_timeout: float = 30.0,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be >= 0")
        for package in required_packages:
            self._validate_package_name(package)

        self.cert_dir = Path(cert_dir)
        self.required_packages: tuple[str, ...] = tuple(dict.fromkeys(required_packages))
        self._os_release_path = Path(os_release_path)
        self._command_timeout = command_timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._sleep = sleep
        self._services: dict[str, ServiceState] = {}

    # ---- subprocess plumbing ------------------------------------------------
    def _run(self, cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        argv = [str(part) for part in cmd]
        logger.debug("exec: %s", shlex.join(argv))
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._command_timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CommandExecutionError(argv, None, f"executable not found: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandExecutionError(
                argv, None, f"timed out after {self._command_timeout:g}s"
            ) from exc

        if check and result.returncode != 0:
            raise CommandExecutionError(argv, result.returncode, (result.stderr or "").strip())
        return result

    @staticmethod
    def _validate_package_name(package: str) -> None:
        if not _PACKAGE_NAME_RE.match(package):
            raise PackageValidationError(f"invalid Debian package name: {package!r}")

    # ---- platform -----------------------------------------------------------
    def read_os_release(self) -> OSRelease:
        """Read and parse the os-release file."""
        path = self._os_release_path
        try:
            with open(path, encoding="utf-8") as handle:
                data = parse_os_release(handle.read())
        except FileNotFoundError as exc:
            raise UnsupportedPlatformError(
                f"{path} not found; AetherOps requires a Debian-family Linux host"
            ) from exc
        except OSError as exc:
            raise SysAdminError(f"unable to read {path}: {exc}") from exc

        if "ID" not in data:
            raise UnsupportedPlatformError(f"{path} does not define ID")

        release = OSRelease(
            id=data["ID"].lower(),
            version_id=data.get("VERSION_ID", ""),
            pretty_name=data.get("PRETTY_NAME", data["ID"]),
            id_like=tuple(data.get("ID_LIKE", "").lower().split()),
        )
        logger.info("detected platform: %s", release.pretty_name)
        return release

    def ensure_debian_family(self) -> OSRelease:
        release = self.read_os_release()
        if not release.is_debian_family:
            raise UnsupportedPlatformError(
                f"unsupported distribution {release.pretty_name!r} "
                f"(ID={release.id}, ID_LIKE={' '.join(release.id_like) or '-'})"
            )
        return release

    # ---- packages -----------------------------------------------------------
    def query_package(self, package: str) -> str | None:
        """Return the installed version of *package*, or ``None`` if absent.

        ``dpkg-query`` exits 1 for unknown packages; any other non-zero exit is
        treated as transient and retried with exponential backoff.
        """
        self._validate_package_name(package)
        cmd = ["dpkg-query", "-W", "-f=${Status}\t${Version}", package]
        last_error = ""

        for attempt in range(1, self._max_retries + 1):
            result = self._run(cmd, check=False)
            if result.returncode == 0:
                status, _, version = result.stdout.partition("\t")
                if status.strip() == DPKG_INSTALLED_STATUS:
                    return version.strip() or None
                logger.debug("%s present but not installed (status=%r)", package, status.strip())
                return None
            if result.returncode == DPKG_NOT_FOUND_RC:
                return None

            last_error = (result.stderr or "").strip() or f"exit code {result.returncode}"
            if attempt < self._max_retries:
                delay = self._retry_backoff * (2 ** (attempt - 1))
                logger.warning(
                    "dpkg-query %s failed (attempt %d/%d): %s; retrying in %.2fs",
                    package, attempt, self._max_retries, last_error, delay,
                )
                self._sleep(delay)

        raise PackageValidationError(
            f"dpkg-query failed for {package!r} after {self._max_retries} attempts: {last_error}"
        )

    def apt_candidate(self, package: str) -> str | None:
        """Return the version apt would install for *package*, if any."""
        self._validate_package_name(package)
        try:
            result = self._run(["apt-cache", "policy", package])
        except CommandExecutionError as exc:
            logger.warning("apt-cache policy %s failed: %s", package, exc.detail)
            return None
        return parse_apt_candidate(result.stdout)

    def validate_packages(self, packages: Sequence[str] | None = None) -> PackageReport:
        targets = tuple(dict.fromkeys(packages)) if packages is not None else self.required_packages
        installed: dict[str, str] = {}
        missing: list[str] = []

        for package in targets:
            version = self.query_package(package)
            if version is None:
                logger.warning("package missing: %s", package)
                missing.append(package)
            else:
                logger.info("package ok: %s %s", package, version)
                installed[package] = version

        candidates = {package: self.apt_candidate(package) for package in missing}
        return PackageReport(installed=installed, missing=tuple(missing), candidates=candidates)

    # ---- TLS ----------------------------------------------------------------
    def generate_tls_certificate(
        self,
        common_name: str,
        *,
        days: int = 365,
        key_size: int = 2048,
        san_dns: Sequence[str] = (),
        overwrite: bool = False,
    ) -> CertificateBundle:
        """Generate a self-signed X.509 certificate and private key via openssl."""
        _validate_hostname(common_name)
        for name in san_dns:
            _validate_hostname(name)
        if days <= 0:
            raise ValueError("days must be positive")
        if key_size not in SUPPORTED_KEY_SIZES:
            raise ValueError(f"key_size must be one of {sorted(SUPPORTED_KEY_SIZES)}")

        stem = common_name.replace("*", "wildcard")
        cert_path = self.cert_dir / f"{stem}.crt"
        key_path = self.cert_dir / f"{stem}.key"
        config_path = self.cert_dir / f"{stem}.cnf"

        if not overwrite and cert_path.exists() and key_path.exists():
            logger.info("reusing existing certificate %s", cert_path)
            return CertificateBundle(cert_path, key_path, config_path, common_name, days, reused=True)

        openssl = shutil.which("openssl")
        if openssl is None:
            raise CertificateGenerationError("openssl executable not found on PATH")

        try:
            self.cert_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(render_openssl_config(common_name, san_dns))
        except OSError as exc:
            raise CertificateGenerationError(f"unable to prepare {self.cert_dir}: {exc}") from exc

        cmd = [
            openssl, "req", "-x509",
            "-newkey", f"rsa:{key_size}",
            "-sha256", "-nodes",
            "-days", str(days),
            "-keyout", str(key_path),
            "-out", str(cert_path),
            "-config", str(config_path),
        ]
        try:
            self._run(cmd)
        except CommandExecutionError as exc:
            raise CertificateGenerationError(f"openssl failed: {exc.detail}") from exc

        try:
            os.chmod(key_path, 0o600)
        except OSError as exc:
            logger.warning("could not restrict permissions on %s: %s", key_path, exc)

        logger.info("generated TLS certificate %s (CN=%s, %d days)", cert_path, common_name, days)
        return CertificateBundle(cert_path, key_path, config_path, common_name, days)

    # ---- simulated systemd ----------------------------------------------------
    def manage_service(self, unit: str, action: ServiceAction) -> ServiceState:
        """Apply *action* to *unit* in the simulated systemd and return its state."""
        if not _UNIT_NAME_RE.match(unit):
            raise ServiceManagementError(f"invalid unit name: {unit!r}")

        current = self._services.get(unit, ServiceState.INACTIVE)
        transitions: dict[ServiceAction, ServiceState] = {
            ServiceAction.START: ServiceState.ACTIVE,
            ServiceAction.STOP: ServiceState.INACTIVE,
            ServiceAction.RESTART: ServiceState.ACTIVE,
            ServiceAction.STATUS: current,
        }
        new_state = transitions[action]
        self._services[unit] = new_state
        logger.info("[simulated] systemctl %s %s -> %s", action.value, unit, new_state.value)
        return new_state

    def mark_service_failed(self, unit: str) -> None:
        if not _UNIT_NAME_RE.match(unit):
            raise ServiceManagementError(f"invalid unit name: {unit!r}")
        self._services[unit] = ServiceState.FAILED
        logger.error("[simulated] %s entered failed state", unit)

    # ---- orchestration ------------------------------------------------------
    def run_verification(
        self,
        common_name: str,
        *,
        check_os: bool = True,
        days: int = 365,
        key_size: int = 2048,
        san_dns: Sequence[str] = (),
        overwrite: bool = False,
    ) -> VerificationReport:
        """Run platform, package and TLS checks; collect failures instead of raising."""
        report = VerificationReport()

        if check_os:
            try:
                report.os_release = self.ensure_debian_family()
            except SysAdminError as exc:
                report.errors.append(str(exc))
                logger.error("platform check failed: %s", exc)
                return report

        try:
            report.packages = self.validate_packages()
        except SysAdminError as exc:
            report.errors.append(str(exc))
            logger.error("package validation failed: %s", exc)
        else:
            hint = report.packages.install_hint()
            if hint is not None:
                report.errors.append(
                    f"missing packages: {', '.join(report.packages.missing)} (run: {hint})"
                )

        try:
            report.certificate = self.generate_tls_certificate(
                common_name, days=days, key_size=key_size, san_dns=san_dns, overwrite=overwrite
            )
        except (SysAdminError, ValueError) as exc:
            report.errors.append(str(exc))
            logger.error("TLS generation failed: %s", exc)

        return report
