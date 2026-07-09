from app.modules.monitor.channel_ids import canonicalize_source_channel_id


def test_canonicalize_source_channel_id_accepts_telethon_marked_peer_id() -> None:
    result = canonicalize_source_channel_id("-1001234567890")

    assert result.status == "valid"
    assert result.canonical_tg_id == -1001234567890
    assert result.error_code is None
    assert result.channel_id_canonical_form == "telethon_marked_peer_id"
    assert result.masked_source_ref == "-1***90"


def test_canonicalize_source_channel_id_strips_whitespace() -> None:
    result = canonicalize_source_channel_id("  -100987654321  ")

    assert result.status == "valid"
    assert result.canonical_tg_id == -100987654321


def test_canonicalize_source_channel_id_accepts_int_input() -> None:
    result = canonicalize_source_channel_id(-100123)

    assert result.status == "valid"
    assert result.canonical_tg_id == -100123


def test_canonicalize_source_channel_id_rejects_bare_channel_id() -> None:
    result = canonicalize_source_channel_id("1234567890")

    assert result.status == "invalid_source_ref"
    assert result.canonical_tg_id is None
    assert result.error_code == "SOURCE_REF_NOT_CANONICAL"


def test_canonicalize_source_channel_id_rejects_non_numeric_source_ref() -> None:
    result = canonicalize_source_channel_id("@some_channel")

    assert result.status == "invalid_source_ref"
    assert result.canonical_tg_id is None
    assert result.error_code == "SOURCE_REF_NOT_NUMERIC"


def test_canonicalize_source_channel_id_rejects_empty_source_ref() -> None:
    result = canonicalize_source_channel_id("   ")

    assert result.status == "invalid_source_ref"
    assert result.canonical_tg_id is None
    assert result.error_code == "SOURCE_REF_NOT_NUMERIC"
    assert result.masked_source_ref == ""


def test_canonicalize_source_channel_id_rejects_incomplete_marked_id() -> None:
    result = canonicalize_source_channel_id("-100")

    assert result.status == "invalid_source_ref"
    assert result.canonical_tg_id is None
    assert result.error_code == "SOURCE_REF_OUT_OF_RANGE"


def test_canonicalize_source_channel_id_rejects_non_channel_negative_id() -> None:
    result = canonicalize_source_channel_id("-1234567890")

    assert result.status == "invalid_source_ref"
    assert result.canonical_tg_id is None
    assert result.error_code == "SOURCE_REF_NOT_CANONICAL"
