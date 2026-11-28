from _paths import PROJECT_ROOT


def test_vault_writer_services_do_not_use_private_tmp() -> None:
    service_templates = sorted((PROJECT_ROOT / "deploy").glob("*.service.in"))

    assert service_templates
    for template in service_templates:
        content = template.read_text(encoding="utf-8")

        assert "NoNewPrivileges=true" in content
        assert "PrivateTmp" not in content
        assert "Environment=UV_BIN=@UV_BIN@" in content


def test_bot_unit_stops_leftover_processes() -> None:
    content = (PROJECT_ROOT / "deploy" / "a-second-brain.service.in").read_text(
        encoding="utf-8"
    )

    assert "KillMode=mixed" in content
    assert "KillMode=process" not in content


def test_network_unit_uses_private_network_command() -> None:
    content = (
        PROJECT_ROOT / "deploy" / "a-second-brain-network.service.in"
    ).read_text(encoding="utf-8")

    assert "a-second-brain network" in content
    assert "EnvironmentFile=@PROJECT_DIR@/.env" in content
    assert "NoNewPrivileges=true" in content


def test_oneshot_services_have_timeout_start_sec() -> None:
    for name in (
        "a-second-brain-morning-brief.service.in",
        "a-second-brain-plaud-sync.service.in",
        "a-second-brain-qmd-maintenance.service.in",
    ):
        content = (PROJECT_ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "TimeoutStartSec=30m" in content, name


def test_process_service_waits_for_network_online() -> None:
    content = (
        PROJECT_ROOT / "deploy" / "a-second-brain-process.service.in"
    ).read_text(encoding="utf-8")

    assert "After=network-online.target" in content
    assert "Wants=network-online.target" in content


def test_network_launchd_plist_template() -> None:
    content = (
        PROJECT_ROOT / "deploy" / "com.second-brain.network.plist.in"
    ).read_text(encoding="utf-8")

    assert "<string>com.second-brain.network</string>" in content
    assert "<string>a-second-brain</string>" in content
    assert "<string>network</string>" in content
    assert "<key>UV_BIN</key>" in content
    assert "<key>KeepAlive</key>" in content
    assert "@LOG_DIR@/network.out.log" in content
