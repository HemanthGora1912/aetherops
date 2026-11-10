"""Tests for :mod:`src.cli` argument parsing and command dispatch."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from src import cli
from src.sys_admin import CertificateBundle, PackageReport, VerificationReport


class TestParser:
    def test_command_is_required(self) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args([])
        assert exc.value.code == 2

    def test_verify_defaults(self) -> None:
        args = cli.build_parser().parse_args(["verify"])
        assert args.handler is cli.cmd_verify
        assert args.cert_dir == Path("certs")
        assert args.common_name == "localhost"
        assert args.packages is None
        assert args.san == []

    def test_start_interval(self) -> None:
        args = cli.build_parser().parse_args(["start", "--interval", "0.25", "--seed", "7"])
        assert args.handler is cli.cmd_start
        assert args.interval == 0.25
        assert args.seed == 7

    @pytest.mark.parametrize(
        "argv",
        [
            ["start", "--interval", "0"],
            ["start", "--interval", "abc"],
            ["start", "--failure-rate", "1.5"],
            ["start", "--nodes", "0"],
            ["verify", "--key-size", "1024"],
        ],
    )
    def test_rejects_invalid_values(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(argv)
        assert exc.value.code == 2

    def test_log_level_is_case_insensitive(self) -> None:
        assert cli.build_parser().parse_args(["--log-level", "debug", "verify"]).log_level == "DEBUG"


class TestVerifyCommand:
    def _report(self, ok: bool) -> VerificationReport:
        missing = () if ok else ("postgresql-client",)
        return VerificationReport(
            packages=PackageReport(installed={"openssl": "3.0"}, missing=missing),
            certificate=CertificateBundle(
                Path("certs/localhost.crt"), Path("certs/localhost.key"),
                Path("certs/localhost.cnf"), "localhost", 365,
            ),
            errors=[] if ok else ["missing packages: postgresql-client"],
        )

    @pytest.mark.parametrize(("ok", "code"), [(True, 0), (False, 1)])
    def test_exit_code_reflects_report(
        self, ok: bool, code: int, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with mock.patch.object(cli, "SystemAdminEngine") as engine_cls:
            engine_cls.return_value.run_verification.return_value = self._report(ok)
            rc = cli.main(["verify", "--skip-os-check", "--san", "db-0", "--package", "openssl"])

        assert rc == code
        engine_cls.assert_called_once_with(cert_dir=Path("certs"), required_packages=["openssl"])
        engine_cls.return_value.run_verification.assert_called_once_with(
            "localhost", check_os=False, days=365, key_size=2048, san_dns=["db-0"], overwrite=False
        )
        assert ("PASS" if ok else "FAIL") in capsys.readouterr().out


class TestStartCommand:
    def test_runs_bounded_daemon(self) -> None:
        rc = cli.main(
            ["--log-level", "WARNING", "start", "--interval", "0.01",
             "--max-cycles", "5", "--seed", "1", "--failure-rate", "0.3"]
        )
        assert rc == 0

    def test_invalid_config_returns_usage(self) -> None:
        assert cli.main(["start", "--max-restarts", "-1", "--max-cycles", "1"]) == 2

    def test_keyboard_interrupt_returns_130(self) -> None:
        def _interrupt(coro: object) -> None:
            coro.close()  # type: ignore[attr-defined]
            raise KeyboardInterrupt

        with mock.patch("src.cli.asyncio.run", side_effect=_interrupt):
            assert cli.main(["start", "--max-cycles", "1"]) == 130
