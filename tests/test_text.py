import pytest

from cbrd_dup.text import SPECIAL, has_hangul, summary_words, text_search_jql


@pytest.mark.parametrize(
    ("summary", "words"),
    [
        ('Fix "quoted" (paren) [bracket] {brace}', ["Fix", "quoted", "paren", "bracket", "brace"]),
        ("a+b-c&d|e!f^g~h*i?j:k\\l/m", list("abcdefghijklm")),
        ("CBRD-123 fails", ["CBRD", "123", "fails"]),
        ("loaddb AND vacuum OR heap NOT x TO y", ["loaddb", "and", "vacuum", "or", "heap", "not", "x", "to", "y"]),
        (
            "and or not to stay as typed, Android ORDER stay too",
            ["and", "or", "not", "to", "stay", "as", "typed,", "Android", "ORDER", "stay", "too"],
        ),
        ("중복 이슈: NOT 중복", ["중복", "이슈", "not", "중복"]),
        ("   ", []),
    ],
)
def test_summary_words(summary, words):
    assert summary_words(summary) == words


def test_summary_words_never_leave_query_syntax():
    words = summary_words("""x" + - && || ! ( ) { } [ ] ^ " ~ * ? : \\ / y""")
    assert words == ["x", "y"]
    assert not any(SPECIAL.search(w) for w in words)


@pytest.mark.parametrize(
    ("variant", "text"),
    [("and", "heap overflow"), ("or", "heap OR overflow")],
)
def test_text_search_jql_variants(variant, text):
    jql = text_search_jql(["heap", "overflow"], variant, "2024-03-05T14:23:11.000+0900")
    assert jql == f'project = CBRD AND text ~ "{text}" AND created < "2024-03-05 14:23"'


def test_has_hangul():
    assert has_hangul("vacuum 오류")
    assert has_hangul("ㅋㅋ")
    assert not has_hangul("vacuum error")
    assert not has_hangul(None)
