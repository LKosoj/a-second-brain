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
