from _paths import PROJECT_ROOT


def test_vault_writer_services_do_not_use_private_tmp() -> None:
    service_templates = sorted((PROJECT_ROOT / "deploy").glob("*.service.in"))

    assert service_templates
    for template in service_templates:
        content = template.read_text(encoding="utf-8")

        assert "NoNewPrivileges=true" in content
        assert "PrivateTmp" not in content
        assert "Environment=UV_BIN=@UV_BIN@" in content
