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
