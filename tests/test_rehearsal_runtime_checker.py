"""Safety contracts for the disposable TCP rehearsal harness."""

import json

from scripts import check_rehearsal_runtime as checker


def test_failed_child_writes_failure_evidence_and_removes_synthetic_root(tmp_path, monkeypatch):
    roots = []
    original_fixture = checker._fixture

    def capture_root(root):
        roots.append(root)
        return original_fixture(root)

    monkeypatch.setattr(checker, "_fixture", capture_root)
    monkeypatch.setattr(checker.Harness, "start", lambda self: False)
    monkeypatch.setattr(
        checker.Harness, "stop", lambda self, force=False: (2, "private study startup failed")
    )
    monkeypatch.setattr(checker.Harness, "shutdown", lambda self: None)
    report_path = tmp_path / "rehearsal-report.json"

    assert checker.run(report_path) == 1
    assert len(roots) == 1
    assert not roots[0].exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["result"] == "fail"
    assert report["participant_admission"] is False
    assert report["failures"]
    assert report_path.stat().st_mode & 0o777 == 0o600


def test_hidden_server_entrypoint_requires_generated_synthetic_root(tmp_path, monkeypatch):
    root = tmp_path / "not-a-rehearsal-root"
    root.mkdir(mode=0o700)
    monkeypatch.delenv(checker.SYNTHETIC_ENV, raising=False)
    monkeypatch.delenv("YOUTH_LAW_SYNTHETIC_ROOT", raising=False)

    assert checker._serve_synthetic(root, -1) == 3


def test_child_environment_does_not_inherit_cloud_or_proxy_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-must-not-propagate")
    monkeypatch.setenv("HTTP_PROXY", "http://synthetic-proxy.invalid")
    environment = checker._safe_child_environment(tmp_path)

    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "HTTP_PROXY" not in environment
    assert environment[checker.SYNTHETIC_ENV] == "1"
    assert environment["YOUTH_LAW_SYNTHETIC_ROOT"] == str(tmp_path.resolve())


def test_report_writer_refuses_to_replace_existing_evidence(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text('{"preserved":true}\n', encoding="utf-8")
    before = report_path.read_bytes()

    try:
        checker._write_report(report_path, {"result": "pass"})
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing evidence was replaced")

    assert report_path.read_bytes() == before


def test_missing_required_phase_cannot_produce_pass():
    assert (
        checker._overall_pass(
            {name: "pass" for name in checker._PHASES if name != "authenticated_load"},
            [],
            [],
        )
        is False
    )


def test_timeout_records_failure_and_removes_temp_root(tmp_path, monkeypatch):
    roots = []
    listeners = []
    original_fixture = checker._fixture
    original_listen = checker._listen

    def capture_root(root):
        roots.append(root)
        return original_fixture(root)

    def capture_listener():
        listener = original_listen()
        listeners.append(listener)
        return listener

    monkeypatch.setattr(checker, "_fixture", capture_root)
    monkeypatch.setattr(checker, "_listen", capture_listener)
    monkeypatch.setattr(checker.Harness, "start", lambda self: True)

    def timeout(_self):
        raise TimeoutError("simulated bounded-run timeout")

    monkeypatch.setattr(checker.Harness, "boundary_checks", timeout)
    report_path = tmp_path / "timeout-report.json"

    assert checker.run(report_path) == 1
    assert not roots[0].exists()
    assert listeners[0].fileno() == -1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["result"] == "fail"
    assert any(failure["reason"] == "TimeoutError" for failure in report["failures"])
    assert report["cleanup"]["temporary_root_removed"] is True
