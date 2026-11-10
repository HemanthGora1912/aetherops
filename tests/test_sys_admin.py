"""Unit tests for :mod:`src.sys_admin`.

``subprocess.run`` and ``builtins.open`` are mocked throughout, so the apt,
dpkg and OpenSSL logic runs identically on Linux, macOS and Windows without
executing any real system command. An autouse guard fails any test that
reaches the real ``subprocess.run``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from src import sys_admin
from src.sys_admin import (
    CertificateGenerationError,
    CommandExecutionError,
    OSRelease,
    PackageValidationError,
    ServiceAction,
    ServiceManagementError,
    ServiceState,
    SystemAdminEngine,
    UnsupportedPlatformError,
    parse_apt_candidate,
    parse_os_release,
    render_openssl_config,
)

OPENSSL_BIN = "/usr/bin/openssl"

UBUNTU_OS_RELEASE = """\
PRETTY_NAME="Ubuntu 24.04.1 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
VERSION="24.04.1 LTS (Noble Numbat)"
ID=ubuntu
ID_LIKE=debian
HOME_URL="https://www.ubuntu.com/"
"""

MINT_OS_RELEASE = """\
NAME="Linux Mint"
ID=linuxmint
ID_LIKE="ubuntu debian"
PRETTY_NAME="Linux Mint 22"
VERSION_ID="22"
"""

FEDORA_OS_RELEASE = """\
NAME="Fedora Linux"
ID=fedora
VERSION_ID=40
PRETTY_NAME="Fedora Linux 40 (Workstation Edition)"
"""

APT_POLICY_OUTPUT = """\
postgresql-client:
  Installed: (none)
  Candidate: 16+257build1
  Version table:
     16+257build1 500
        500 http://archive.ubuntu.com/ubuntu noble/main amd64 Packages
"""


# --------------------------------------------------------------------------- #
# Helpers & fixtures
# --------------------------------------------------------------------------- #
def completed(
    args: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout=stdout, stderr=stderr)


def dpkg_installed(version: str) -> str:
    return f"install ok installed\t{version}"


@pytest.fixture(autouse=True)
def _forbid_real_subprocess() -> Iterator[None]:
    """Fail loudly if any test reaches the real subprocess.run."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"real subprocess.run invoked with {args!r} {kwargs!r}")

    with mock.patch("subprocess.run", side_effect=_boom):
        yield


@pytest.fixture
def mock_run() -> Iterator[mock.MagicMock]:
    with mock.patch("subprocess.run") as patched:
        yield patched


@pytest.fixture
def sleep() -> mock.MagicMock:
    return mock.MagicMock(name="sleep")


@pytest.fixture
def engine(tmp_path: Path, sleep: mock.MagicMock) -> SystemAdminEngine:
    return SystemAdminEngine(
        cert_dir=tmp_path / "certs",
        required_packages=("openssl", "postgresql-client"),
        max_retries=3,
        retry_backoff=0.5,
        sleep=sleep,
    )


@pytest.fixture
def openssl_env() -> Iterator[dict[str, mock.MagicMock]]:
    """Patch everything generate_tls_certificate touches outside Python."""
    file_mock = mock.mock_open()
    with mock.patch("builtins.open", file_mock), mock.patch(
        "src.sys_admin.shutil.which", return_value=OPENSSL_BIN
    ) as which, mock.patch("src.sys_admin.os.chmod") as chmod, mock.patch(
        "subprocess.run", side_effect=lambda argv, **_: completed(argv)
    ) as run:
        yield {"open": file_mock, "which": which, "chmod": chmod, "run": run}


def dispatching_run(
    *,
    packages: dict[str, str | None],
    openssl_rc: int = 0,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Fake subprocess.run routing dpkg-query / apt-cache / openssl invocations."""

    def _run(argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        program = Path(argv[0]).name
        if program == "dpkg-query":
            version = packages.get(argv[-1])
            if version is None:
                return completed(argv, 1, stderr=f"dpkg-query: no packages found matching {argv[-1]}")
            return completed(argv, 0, stdout=dpkg_installed(version))
        if program == "apt-cache":
            return completed(argv, 0, stdout=APT_POLICY_OUTPUT)
        if program == "openssl":
            return completed(argv, openssl_rc, stderr="" if openssl_rc == 0 else "bad config")
        raise AssertionError(f"unexpected command: {argv!r}")

    return _run


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #
class TestConstruction:
    @pytest.mark.parametrize(
        "kwargs",
        [{"command_timeout": 0}, {"max_retries": 0}, {"retry_backoff": -1.0}],
    )
    def test_rejects_invalid_tuning(self, tmp_path: Path, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            SystemAdminEngine(cert_dir=tmp_path, **kwargs)  # type: ignore[arg-type]

    def test_rejects_invalid_package_names(self, tmp_path: Path) -> None:
        with pytest.raises(PackageValidationError):
            SystemAdminEngine(cert_dir=tmp_path, required_packages=("openssl; rm -rf /",))

    def test_deduplicates_required_packages(self, tmp_path: Path) -> None:
        engine = SystemAdminEngine(cert_dir=tmp_path, required_packages=("curl", "curl", "jq"))
        assert engine.required_packages == ("curl", "jq")


# --------------------------------------------------------------------------- #
# /etc/os-release
# --------------------------------------------------------------------------- #
class TestOSRelease:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ('PRETTY_NAME="Ubuntu 24.04 LTS"', "Ubuntu 24.04 LTS"),
            ("ID=debian", "debian"),
            ("VERSION_ID='12'", "12"),
            ('NAME="Unterminated', "Unterminated"),
        ],
    )
    def test_parse_handles_quoting(self, line: str, expected: str) -> None:
        key = line.partition("=")[0]
        assert parse_os_release(line)[key] == expected

    def test_parse_skips_comments_and_garbage(self) -> None:
        data = parse_os_release("# comment\n\nnot a pair\n1BAD=x\nID=debian\n")
        assert data == {"ID": "debian"}

    def test_reads_ubuntu(self, engine: SystemAdminEngine) -> None:
        with mock.patch("builtins.open", mock.mock_open(read_data=UBUNTU_OS_RELEASE)) as opened:
            release = engine.read_os_release()

        opened.assert_called_once_with(Path("/etc/os-release"), encoding="utf-8")
        assert release == OSRelease(
            id="ubuntu", version_id="24.04", pretty_name="Ubuntu 24.04.1 LTS", id_like=("debian",)
        )
        assert release.is_debian_family

    def test_derivative_detected_via_id_like(self, engine: SystemAdminEngine) -> None:
        with mock.patch("builtins.open", mock.mock_open(read_data=MINT_OS_RELEASE)):
            release = engine.ensure_debian_family()
        assert release.id == "linuxmint"
        assert release.id_like == ("ubuntu", "debian")

    def test_non_debian_rejected(self, engine: SystemAdminEngine) -> None:
        with mock.patch("builtins.open", mock.mock_open(read_data=FEDORA_OS_RELEASE)):
            with pytest.raises(UnsupportedPlatformError, match="Fedora"):
                engine.ensure_debian_family()

    def test_missing_file_is_unsupported_platform(self, engine: SystemAdminEngine) -> None:
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            with pytest.raises(UnsupportedPlatformError, match="not found"):
                engine.read_os_release()

    def test_unreadable_file_is_sysadmin_error(self, engine: SystemAdminEngine) -> None:
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            with pytest.raises(sys_admin.SysAdminError, match="unable to read"):
                engine.read_os_release()

    def test_missing_id_rejected(self, engine: SystemAdminEngine) -> None:
        with mock.patch("builtins.open", mock.mock_open(read_data='NAME="Mystery"\n')):
            with pytest.raises(UnsupportedPlatformError, match="ID"):
                engine.read_os_release()


# --------------------------------------------------------------------------- #
# dpkg / apt
# --------------------------------------------------------------------------- #
class TestPackageQueries:
    def test_installed_package_returns_version(
        self, engine: SystemAdminEngine, mock_run: mock.MagicMock
    ) -> None:
        mock_run.return_value = completed([], 0, stdout=dpkg_installed("3.0.13-0ubuntu3"))

        assert engine.query_package("openssl") == "3.0.13-0ubuntu3"
        mock_run.assert_called_once_with(
            ["dpkg-query", "-W", "-f=${Status}\t${Version}", "openssl"],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )

    def test_unknown_package_returns_none(
        self, engine: SystemAdminEngine, mock_run: mock.MagicMock
    ) -> None:
        mock_run.return_value = completed([], 1, stderr="no packages found matching nope")
        assert engine.query_package("nope") is None
        assert mock_run.call_count == 1

    def test_config_files_only_counts_as_missing(
        self, engine: SystemAdminEngine, mock_run: mock.MagicMock
    ) -> None:
        mock_run.return_value = completed([], 0, stdout="deinstall ok config-files\t1.0")
        assert engine.query_package("openssl") is None

    def test_transient_failure_is_retried_with_backoff(
        self, engine: SystemAdminEngine, mock_run: mock.MagicMock, sleep: mock.MagicMock
    ) -> None:
        mock_run.side_effect = [
            completed([], 2, stderr="database locked"),
            completed([], 2, stderr="database locked"),
            completed([], 0, stdout=dpkg_installed("1.2.3")),
        ]
        assert engine.query_package("openssl") == "1.2.3"
        assert mock_run.call_count == 3
        assert sleep.call_args_list == [mock.call(0.5), mock.call(1.0)]

    def test_retries_exhausted_raises(
        self, engine: SystemAdminEngine, mock_run: mock.MagicMock, sleep: mock.MagicMock
    ) -> None:
        mock_run.return_value = completed([], 2, stderr="database locked")
        with pytest.raises(PackageValidationError, match="after 3 attempts: database locked"):
            engine.query_package("openssl")
        assert mock_run.call_count == 3
        assert sleep.call_count == 2

    def test_invalid_name_never_reaches_subprocess(
        self, engine: SystemAdminEngine, mock_run: mock.MagicMock
    ) -> None:
        with pytest.raises(PackageValidationError):
            engine.query_package("$(reboot)")
        mock_run.assert_not_called()

    def test_missing_dpkg_binary(self, engine: SystemAdminEngine, mock_run: mock.MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("dpkg-query")
        with pytest.raises(CommandExecutionError, match="executable not found: dpkg-query"):
            engine.query_package("openssl")

    def test_command_timeout(self, engine: SystemAdminEngine, mock_run: mock.MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="dpkg-query", timeout=30)
        with pytest.raises(CommandExecutionError, match="timed out"):
            engine.query_package("openssl")

    def test_validate_packages_report(
        self, engine: SystemAdminEngine, mock_run: mock.MagicMock
    ) -> None:
        mock_run.side_effect = dispatching_run(packages={"openssl": "3.0.13"})
        report = engine.validate_packages()

        assert dict(report.installed) == {"openssl": "3.0.13"}
        assert report.missing == ("postgresql-client",)
        assert dict(report.candidates) == {"postgresql-client": "16+257build1"}
        assert not report.ok
        assert report.install_hint() == "sudo apt-get install -y postgresql-client"

    def test_validate_packages_all_present(
        self, engine: SystemAdminEngine, mock_run: mock.MagicMock
    ) -> None:
        mock_run.side_effect = dispatching_run(
            packages={"openssl": "3.0.13", "postgresql-client": "16"}
        )
        report = engine.validate_packages()
        assert report.ok
        assert report.install_hint() is None
        assert all(call.args[0][0] == "dpkg-query" for call in mock_run.call_args_list)

    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            (APT_POLICY_OUTPUT, "16+257build1"),
            ("pkg:\n  Installed: (none)\n  Candidate: (none)\n", None),
            ("", None),
        ],
    )
    def test_parse_apt_candidate(self, output: str, expected: str | None) -> None:
        assert parse_apt_candidate(output) == expected

    def test_apt_candidate_failure_is_soft(
        self, engine: SystemAdminEngine, mock_run: mock.MagicMock
    ) -> None:
        mock_run.return_value = completed([], 100, stderr="E: cache broken")
        assert engine.apt_candidate("openssl") is None


# --------------------------------------------------------------------------- #
# OpenSSL
# --------------------------------------------------------------------------- #
class TestTLSGeneration:
    def test_renders_config_with_deduplicated_sans(self) -> None:
        config = render_openssl_config("db-0.cluster.local", ["db-0", "db-0.cluster.local"])
        assert "CN = db-0.cluster.local" in config
        assert "DNS.1 = db-0.cluster.local" in config
        assert "DNS.2 = db-0" in config
        assert "DNS.3" not in config
        assert "extendedKeyUsage = serverAuth,clientAuth" in config

    def test_generates_certificate(
        self, engine: SystemAdminEngine, openssl_env: dict[str, mock.MagicMock]
    ) -> None:
        bundle = engine.generate_tls_certificate(
            "db-0.cluster.local", days=90, key_size=4096, san_dns=["db-0"]
        )

        cert_dir = engine.cert_dir
        assert bundle.cert_path == cert_dir / "db-0.cluster.local.crt"
        assert bundle.key_path == cert_dir / "db-0.cluster.local.key"
        assert not bundle.reused

        openssl_env["open"].assert_called_once_with(bundle.config_path, "w", encoding="utf-8")
        written = "".join(c.args[0] for c in openssl_env["open"]().write.call_args_list)
        assert "CN = db-0.cluster.local" in written
        assert "DNS.2 = db-0" in written

        argv = openssl_env["run"].call_args.args[0]
        assert argv[:3] == [OPENSSL_BIN, "req", "-x509"]
        assert argv[argv.index("-newkey") + 1] == "rsa:4096"
        assert argv[argv.index("-days") + 1] == "90"
        assert argv[argv.index("-keyout") + 1] == str(bundle.key_path)
        assert argv[argv.index("-out") + 1] == str(bundle.cert_path)
        assert argv[argv.index("-config") + 1] == str(bundle.config_path)
        assert "-nodes" in argv
        assert openssl_env["run"].call_args.kwargs.get("shell") is not True

        openssl_env["chmod"].assert_called_once_with(bundle.key_path, 0o600)

    def test_missing_openssl_binary(
        self, engine: SystemAdminEngine, openssl_env: dict[str, mock.MagicMock]
    ) -> None:
        openssl_env["which"].return_value = None
        with pytest.raises(CertificateGenerationError, match="not found on PATH"):
            engine.generate_tls_certificate("localhost")
        openssl_env["run"].assert_not_called()
        openssl_env["open"].assert_not_called()

    def test_openssl_failure_is_wrapped(
        self, engine: SystemAdminEngine, openssl_env: dict[str, mock.MagicMock]
    ) -> None:
        openssl_env["run"].side_effect = lambda argv, **_: completed(argv, 1, stderr="unable to load config")
        with pytest.raises(CertificateGenerationError, match="unable to load config"):
            engine.generate_tls_certificate("localhost")
        openssl_env["chmod"].assert_not_called()

    def test_config_write_failure(
        self, engine: SystemAdminEngine, openssl_env: dict[str, mock.MagicMock]
    ) -> None:
        openssl_env["open"].side_effect = PermissionError("read-only")
        with pytest.raises(CertificateGenerationError, match="unable to prepare"):
            engine.generate_tls_certificate("localhost")
        openssl_env["run"].assert_not_called()

    def test_chmod_failure_is_non_fatal(
        self, engine: SystemAdminEngine, openssl_env: dict[str, mock.MagicMock]
    ) -> None:
        openssl_env["chmod"].side_effect = PermissionError("not supported")
        bundle = engine.generate_tls_certificate("localhost")
        assert bundle.key_path.name == "localhost.key"

    def test_existing_certificate_is_reused(
        self, engine: SystemAdminEngine, tmp_path: Path
    ) -> None:
        engine.cert_dir.mkdir(parents=True)
        (engine.cert_dir / "localhost.crt").write_text("cert")
        (engine.cert_dir / "localhost.key").write_text("key")

        with mock.patch("subprocess.run") as run, mock.patch("builtins.open") as opened:
            bundle = engine.generate_tls_certificate("localhost")

        assert bundle.reused
        run.assert_not_called()
        opened.assert_not_called()

    def test_overwrite_regenerates(
        self, engine: SystemAdminEngine, openssl_env: dict[str, mock.MagicMock]
    ) -> None:
        engine.cert_dir.mkdir(parents=True)
        (engine.cert_dir / "localhost.crt").touch()
        (engine.cert_dir / "localhost.key").touch()

        bundle = engine.generate_tls_certificate("localhost", overwrite=True)
        assert not bundle.reused
        openssl_env["run"].assert_called_once()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"common_name": "bad host;rm -rf /"},
            {"common_name": "localhost", "san_dns": ["-evil"]},
            {"common_name": "localhost", "days": 0},
            {"common_name": "localhost", "key_size": 1024},
        ],
    )
    def test_rejects_invalid_arguments(
        self,
        engine: SystemAdminEngine,
        openssl_env: dict[str, mock.MagicMock],
        kwargs: dict[str, Any],
    ) -> None:
        with pytest.raises(ValueError):
            engine.generate_tls_certificate(**kwargs)
        openssl_env["run"].assert_not_called()


# --------------------------------------------------------------------------- #
# Simulated systemd
# --------------------------------------------------------------------------- #
class TestServiceManagement:
    def test_lifecycle(self, engine: SystemAdminEngine, mock_run: mock.MagicMock) -> None:
        unit = "postgresql@16-main.service"
        assert engine.manage_service(unit, ServiceAction.STATUS) is ServiceState.INACTIVE
        assert engine.manage_service(unit, ServiceAction.START) is ServiceState.ACTIVE
        assert engine.manage_service(unit, ServiceAction.STATUS) is ServiceState.ACTIVE
        engine.mark_service_failed(unit)
        assert engine.manage_service(unit, ServiceAction.STATUS) is ServiceState.FAILED
        assert engine.manage_service(unit, ServiceAction.RESTART) is ServiceState.ACTIVE
        assert engine.manage_service(unit, ServiceAction.STOP) is ServiceState.INACTIVE
        mock_run.assert_not_called()

    @pytest.mark.parametrize("unit", ["postgresql", "evil.service; reboot", "../x.service"])
    def test_rejects_invalid_unit(self, engine: SystemAdminEngine, unit: str) -> None:
        with pytest.raises(ServiceManagementError):
            engine.manage_service(unit, ServiceAction.START)


# --------------------------------------------------------------------------- #
# End-to-end verification
# --------------------------------------------------------------------------- #
class TestRunVerification:
    def test_full_pass(self, engine: SystemAdminEngine) -> None:
        fake_run = dispatching_run(packages={"openssl": "3.0.13", "postgresql-client": "16"})
        with mock.patch(
            "builtins.open", mock.mock_open(read_data=UBUNTU_OS_RELEASE)
        ), mock.patch("subprocess.run", side_effect=fake_run) as run, mock.patch(
            "src.sys_admin.shutil.which", return_value=OPENSSL_BIN
        ), mock.patch("src.sys_admin.os.chmod"):
            report = engine.run_verification("db-0.cluster.local", san_dns=["db-0"])

        assert report.ok, report.errors
        assert report.os_release is not None and report.os_release.id == "ubuntu"
        assert report.certificate is not None
        programs = [Path(c.args[0][0]).name for c in run.call_args_list]
        assert programs == ["dpkg-query", "dpkg-query", "openssl"]

    def test_missing_packages_reported(self, engine: SystemAdminEngine) -> None:
        fake_run = dispatching_run(packages={"openssl": "3.0.13"})
        with mock.patch(
            "builtins.open", mock.mock_open(read_data=UBUNTU_OS_RELEASE)
        ), mock.patch("subprocess.run", side_effect=fake_run), mock.patch(
            "src.sys_admin.shutil.which", return_value=OPENSSL_BIN
        ), mock.patch("src.sys_admin.os.chmod"):
            report = engine.run_verification("localhost")

        assert not report.ok
        assert report.certificate is not None
        assert any("sudo apt-get install -y postgresql-client" in e for e in report.errors)

    def test_unsupported_platform_short_circuits(
        self, engine: SystemAdminEngine, mock_run: mock.MagicMock
    ) -> None:
        with mock.patch("builtins.open", mock.mock_open(read_data=FEDORA_OS_RELEASE)):
            report = engine.run_verification("localhost")

        assert not report.ok
        assert report.packages is None and report.certificate is None
        assert "unsupported distribution" in report.errors[0]
        mock_run.assert_not_called()

    def test_skip_os_check(self, engine: SystemAdminEngine) -> None:
        fake_run = dispatching_run(packages={"openssl": "3", "postgresql-client": "16"})
        with mock.patch("subprocess.run", side_effect=fake_run), mock.patch(
            "builtins.open", mock.mock_open()
        ) as opened, mock.patch(
            "src.sys_admin.shutil.which", return_value=OPENSSL_BIN
        ), mock.patch("src.sys_admin.os.chmod"):
            report = engine.run_verification("localhost", check_os=False)

        assert report.ok
        assert report.os_release is None
        assert all(c.args[0] != Path("/etc/os-release") for c in opened.call_args_list)

    def test_tls_failure_collected(self, engine: SystemAdminEngine) -> None:
        fake_run = dispatching_run(packages={"openssl": "3", "postgresql-client": "16"}, openssl_rc=1)
        with mock.patch(
            "builtins.open", mock.mock_open(read_data=UBUNTU_OS_RELEASE)
        ), mock.patch("subprocess.run", side_effect=fake_run), mock.patch(
            "src.sys_admin.shutil.which", return_value=OPENSSL_BIN
        ):
            report = engine.run_verification("localhost")

        assert not report.ok
        assert report.packages is not None and report.packages.ok
        assert any("openssl failed" in e for e in report.errors)
