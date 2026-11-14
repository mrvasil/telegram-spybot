from spybot.render import event_text, history_text


def record():
    return {
        "message": {
            "id": 5,
            "chat_id": 10,
            "message_id": 42,
            "chat_title": "Антон <тест>",
            "deleted_at": 100,
        },
        "versions": [
            {
                "id": 1,
                "kind": "original",
                "source_date": 90,
                "text": None,
                "payload": {"photo": [{"file_id": "PRIVATE_FILE_ID", "width": 100, "height": 200}]},
            }
        ],
        "media": [],
    }


def test_photo_cards_are_compact_and_never_render_raw_metadata():
    history = record()
    for text in [
        event_text(history, "delete", None, "Europe/Moscow"),
        history_text(history, 0, "Europe/Moscow"),
    ]:
        assert "Фото" in text
        assert "<b>" in text
        assert "Антон &lt;тест&gt;" in text
        assert "PRIVATE_FILE_ID" not in text
        assert "file_id" not in text and "photo" not in text
        assert len(text) < 500


def test_edit_has_two_highlighted_versions_without_technical_diff_or_repetition():
    history = record()
    history["versions"][0].update(text="hello <old>")
    history["versions"].append(
        {"id": 2, "kind": "edit", "source_date": 95, "text": "hello <new>", "payload": {}}
    )
    for text in [
        event_text(history, "edit", 2, "Europe/Moscow"),
        history_text(history, 1, "Europe/Moscow"),
    ]:
        assert "<b>Было</b>" in text and "<b>Стало</b>" in text
        assert text.count("hello &lt;old&gt;") == 1
        assert text.count("hello &lt;new&gt;") == 1
        assert "<blockquote>" in text
        assert "@@" not in text and "---" not in text and "+++" not in text


def test_long_untrusted_card_is_bounded_balanced_and_cannot_inject_html():
    from html.parser import HTMLParser

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []

        def handle_starttag(self, tag, attrs):
            assert tag in {"b", "i", "blockquote"}
            assert not attrs
            self.stack.append(tag)

        def handle_endtag(self, tag):
            assert self.stack.pop() == tag

    history = record()
    history["message"]["chat_title"] = '<a href="bad">😀&</a>' * 1000
    history["versions"][0]["text"] = "<script>😀&</script>" * 3000
    text = history_text(history, 0, "Europe/Moscow")
    parser = Tags()
    parser.feed(text)
    assert not parser.stack
    assert len(text.encode("utf-16-le")) // 2 < 3900
    assert "<script>" not in text
