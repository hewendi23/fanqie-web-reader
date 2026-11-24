from routers.books import _map_unidbg_book, _tag_text


def test_tag_text_accepts_list_and_string():
    assert _tag_text(["玄幻", "升级"]) == "玄幻,升级"
    assert _tag_text("玄幻,升级") == "玄幻,升级"
    assert _tag_text(None) == ""


def test_unidbg_search_book_tags_are_text_and_id_is_string():
    book = _map_unidbg_book({
        "bookId": 7276384138653862966,
        "bookName": "斗破苍穹",
        "tags": ["玄幻", "升级"],
        "creationStatus": "1",
    })
    assert book["BookID"] == "7276384138653862966"
    assert book["Tags"] == "玄幻,升级"
    assert isinstance(book["Tags"], str)
