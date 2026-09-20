from gateway.authz_mixin import _coerce_allow_set
from hermes_cli.config import _parse_telegram_chat_ids, _telegram_response_gate_issues


def test_free_response_chat_must_also_pass_hard_allowed_chat_gate():
    config = {
        "gateway": {
            "platforms": {
                "telegram": {
                    "extra": {
                        "allowed_chats": ["-100"],
                        "free_response_chats": ["-100", "-200"],
                    }
                }
            }
        }
    }

    assert _telegram_response_gate_issues(config) == [
        "Telegram free-response chats blocked by allowed_chats: -200"
    ]


def test_empty_allowed_chat_gate_does_not_block_free_response_chats():
    config = {
        "gateway": {
            "platforms": {
                "telegram": {
                    "extra": {
                        "free_response_chats": ["-200"],
                    }
                }
            }
        }
    }

    assert _telegram_response_gate_issues(config) == []


def test_csv_allowlists_are_normalized_like_runtime():
    config = {
        "gateway": {
            "platforms": {
                "telegram": {
                    "extra": {
                        "allowed_chats": "-100,-200",
                        "free_response_chats": "-201",
                    }
                }
            }
        }
    }

    assert _telegram_response_gate_issues(config) == [
        "Telegram free-response chats blocked by allowed_chats: -201"
    ]


def test_json_list_strings_are_normalized_like_runtime():
    config = {
        "gateway": {
            "platforms": {
                "telegram": {
                    "extra": {
                        "allowed_chats": '["-100", "-200"]',
                        "free_response_chats": '["-200", "-300"]',
                    }
                }
            }
        }
    }

    assert _telegram_response_gate_issues(config) == [
        "Telegram free-response chats blocked by allowed_chats: -300"
    ]


def test_malformed_json_like_allowlist_matches_runtime_fallback():
    config = {
        "gateway": {
            "platforms": {
                "telegram": {
                    "extra": {
                        "allowed_chats": '["-100"',
                        "free_response_chats": '["-100"]',
                    }
                }
            }
        }
    }

    assert _telegram_response_gate_issues(config) == [
        "Telegram free-response chats blocked by allowed_chats: -100"
    ]


def test_scalar_allowlists_are_normalized_like_runtime():
    config = {"gateway": {"platforms": {"telegram": {"extra": {
        "allowed_chats": -100,
        "free_response_chats": -200,
    }}}}}

    assert _telegram_response_gate_issues(config) == [
        "Telegram free-response chats blocked by allowed_chats: -200"
    ]


def test_chat_id_parser_matches_runtime_for_supported_shapes():
    values = [
        None,
        [],
        ["-1"],
        "-1,-2",
        '["-1", "-2"]',
        '["-1"',
        -100,
        True,
        {"x": 1},
        ("-1", "-2"),
    ]
    for value in values:
        assert _parse_telegram_chat_ids(value) == _coerce_allow_set(value)
