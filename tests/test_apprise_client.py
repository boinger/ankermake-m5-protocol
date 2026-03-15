from libflagship.notifications.apprise_client import (
    AppriseClient,
    _attachment_name_from_url,
    _normalize_attachments,
)


def test_normalize_attachments_filters_empty_values():
    assert _normalize_attachments(None) is None
    assert _normalize_attachments(["one.png", "", None, "two.png"]) == ["one.png", "two.png"]
    assert _normalize_attachments("single.png") == ["single.png"]


def test_attachment_name_from_url_uses_path_basename():
    assert _attachment_name_from_url("https://example.test/files/frame.jpg?sig=1") == "frame.jpg"
    assert _attachment_name_from_url("https://example.test") == "attachment"


def test_apprise_client_applies_environment_overrides():
    client = AppriseClient(
        {
            "enabled": False,
            "server_url": "https://config.example/",
            "key": "config-key",
            "events": {"print_started": False},
            "templates": {"print_started": "Started {filename}"},
        },
        env={
            "APPRISE_ENABLED": "true",
            "APPRISE_SERVER_URL": "https://env.example/",
            "APPRISE_KEY": "env-key",
            "APPRISE_EVENT_PRINT_STARTED": "1",
            "APPRISE_PROGRESS_MAX": "90",
        },
    )

    assert client.is_enabled() is True
    assert client.is_event_enabled("print_started") is True
    assert client.settings["progress"]["max_value"] == 90
    assert client._notify_url() == "https://env.example/notify/env-key"


def test_apprise_client_render_template_keeps_missing_placeholders():
    client = AppriseClient(
        {
            "enabled": True,
            "server_url": "https://notify.example",
            "key": "secret",
            "events": {"print_started": True},
            "templates": {"print_started": "Started {filename} for {owner}"},
        },
        env={},
    )

    assert client.render_template("print_started", {"filename": "cube.gcode"}) == "Started cube.gcode for {owner}"


def test_apprise_client_send_short_circuits_when_disabled():
    client = AppriseClient({}, env={})

    ok, message = client.send("print_started", payload={"filename": "cube.gcode"})

    assert ok is False
    assert "disabled" in message.lower()
