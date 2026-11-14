from spybot.normalize import collect_media, contextual_messages, quote_search, sender


def test_recursive_files_preserve_sizes_duplicates_and_unknown_fields():
    raw = {
        "photo": [{"file_id": "small"}, {"file_id": "large"}],
        "future": {
            "file_id": "large",
            "new_field": [1, 2],
            "thumbnail": {"file_id": "thumb", "file_unique_id": "u"},
        },
        "remote": {"file_unique_id": "not_downloadable"},
    }
    records = collect_media(raw)
    assert [r["file_id"] for r in records] == ["small", "large", "large", "thumb"]
    assert len({r["metadata"]["path"] for r in records}) == 4
    assert records[2]["metadata"]["file"]["new_field"] == [1, 2]


def test_context_and_null_sender():
    raw = {
        "from": None,
        "reply_to_message": {"message_id": 2, "chat": {"id": 4}, "photo": [{"file_id": "context"}]},
        "pinned_message": {"animation": {"file_id": "no_identity"}},
    }
    assert sender(raw) == (None, None)
    assert len(list(contextual_messages(raw))) == 1
    assert [v["file_id"] for v in collect_media(raw, skip_context=True)] == ["no_identity"]


def test_search_is_literal():
    assert quote_search("OR foo*") == '"OR" AND "foo*"'
    assert quote_search('a"b') == '"a""b"'
    assert quote_search("  ") == ""
