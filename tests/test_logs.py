from pathlib import Path

from sisu_kudos.logs import collect_hermes_logs, parse_log_line, redact_message


def test_structured_log_line_is_parsed_and_redacted():
    row = parse_log_line(
        "2026-08-19 20:10:11,123 ERROR [sess_private] gateway.run: "
        "request failed token=supersecretvalue user@example.com session_abcdef123456 "
        "https://example.test/path?access_token=secret",
        "gateway",
    )
    assert row is not None
    assert row["level"] == "ERROR"
    assert row["agent"] == "default"
    assert row["source"] == "gateway"
    rendered = repr(row)
    assert "supersecretvalue" not in rendered
    assert "user@example.com" not in rendered
    assert "abcdef123456" not in rendered
    assert "access_token" not in rendered
    assert "[REDACTED]" in rendered


def test_unstructured_continuations_are_not_exposed():
    assert parse_log_line("raw traceback with private values", "errors") is None


def test_redaction_bounds_message_and_masks_home():
    text = redact_message("/home/demo/private/thing " + "x" * 500)
    assert "/home/demo" not in text
    assert len(text) <= 321


def test_redaction_removes_multitoken_sensitive_values_and_url_userinfo():
    prompt = redact_message("request prompt: PRIVATE_PROMPT_A PRIVATE_PROMPT_B")
    basic = redact_message("failed Authorization: Basic PRIVATE_BASIC_SECRET")
    url = redact_message("failed https://user:PRIVATE_URL_SECRET@example.test/api?token=nope")
    command = redact_message("shutdown parent_cmdline='/usr/bin/private --secret value'")

    assert prompt == "request prompt=[REDACTED]"
    assert basic == "failed Authorization=[REDACTED]"
    assert "PRIVATE_URL_SECRET" not in url
    assert "token=nope" not in url
    assert url.startswith("failed https://")
    assert url.endswith("@example.test/api")
    assert command == "shutdown parent_cmdline=[REDACTED]"


def test_collector_uses_only_allowlisted_files_and_is_bounded(tmp_path: Path):
    (tmp_path / "agent.log").write_text(
        "".join(f"2026-08-19 20:10:{i % 60:02d} INFO agent.core: safe {i}\n" for i in range(400))
    )
    (tmp_path / "private.log").write_text("2026-08-19 20:10:11 ERROR x: SHOULD_NOT_LEAK\n")
    data = collect_hermes_logs(tmp_path, limit=25)
    assert len(data["records"]) == 25
    assert "SHOULD_NOT_LEAK" not in repr(data)
    assert data["redacted"] is True


def test_collector_preserves_rows_for_each_available_source(tmp_path: Path):
    for source in ("agent.log", "errors.log", "gateway.log"):
        (tmp_path / source).write_text(
            "".join(
                f"2026-08-19 20:10:{i % 60:02d} INFO test.component: {source} {i}\n"
                for i in range(40)
            )
        )
    data = collect_hermes_logs(tmp_path, limit=10)
    counts = {
        source: sum(row["source"] == source for row in data["records"])
        for source in ("agent", "errors", "gateway")
    }
    assert counts == {"agent": 10, "errors": 10, "gateway": 10}


def test_collector_labels_and_balances_multiple_hermes_profiles(tmp_path: Path):
    profiles = {}
    for agent, dirname in (("default", "default"), ("Worker", "worker")):
        root = tmp_path / dirname
        root.mkdir()
        profiles[agent] = root
        for filename in ("agent.log", "errors.log", "gateway.log"):
            (root / filename).write_text(
                "".join(
                    f"2026-08-19 20:10:{i % 60:02d} ERROR test.component: safe {i}\n"
                    for i in range(80)
                )
            )

    data = collect_hermes_logs(profiles, limit=10)
    # Same event mirrored in three files is one row, preserving all provenance.
    assert len(data["records"]) == 20
    assert {row["agent"] for row in data["records"]} == {"default", "Worker"}
    assert {(row["agent"], source) for row in data["records"] for source in row["sources"]} == {
        (agent, source)
        for agent in ("default", "Worker")
        for source in ("agent", "errors", "gateway")
    }
