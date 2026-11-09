import re

import pytest

from cbrd_dup.text import (
    SPECIAL,
    error_codes,
    has_hangul,
    identifiers,
    mask_key_identifiers,
    mask_keys,
    stack_functions,
    summary_words,
    text_search_jql,
    tokenize,
)


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


def test_mask_keys_hides_keys_in_text_and_urls_and_keeps_the_rest():
    text = "Same as CBRD-123 and cbrd-4567, see https://jira.cubrid.org/browse/CBRD-89?x=1. OTHER-12 stays."
    masked = mask_keys(text)
    assert not re.search(r"cbrd-\d", masked, re.IGNORECASE)
    assert masked.startswith("Same as ")
    assert masked.endswith(". OTHER-12 stays.")
    assert masked.count("https://jira.cubrid.org/browse/") == 1
    assert mask_keys("no keys, CBRD alone, CBRD- too") == "no keys, CBRD alone, CBRD- too"


def test_masked_text_cannot_match_a_key():
    assert mask_keys(mask_keys("CBRD-1")) == mask_keys("CBRD-1")
    assert not re.search(r"CBRD-\d+", mask_keys("CBRD-1"), re.IGNORECASE)


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("Heap Vacuum", ["heap", "vacuum"]),
        ("list_file QFILE_scan", ["list_file", "list", "file", "qfile_scan", "qfile", "scan"]),
        ("_x __ a__b", ["_x", "__", "a__b", "a", "b"]),
        ("CBRD-123: fails", ["cbrd", "123", "fails"]),
        ("중복이슈", ["중복", "복이", "이슈"]),
        ("가 heap중복", ["가", "heap", "중복"]),
        ("a.b/c", ["a", "b", "c"]),
        ("", []),
    ],
)
def test_tokenize(text, tokens):
    assert tokenize(text) == tokens


def test_masked_key_adds_no_search_tokens():
    assert tokenize(mask_keys("vacuum CBRD-123 crash")) == tokenize("vacuum crash")


@pytest.mark.parametrize(
    "text",
    ["cbrd_27516", "CBRD_27516", "cbrd27516", "CBRD-27516", "db25833", "db_25833", "/home/qa/cbrd_27516/x.sql"],
)
def test_mask_key_identifiers_hides_identifiers_named_after_an_issue(text):
    masked = mask_key_identifiers(f"run {text} now")
    assert not re.search(r"\d{5}", masked)
    assert tokenize(masked)[0] == "run" and tokenize(masked)[-1] == "now"


@pytest.mark.parametrize("text", ["testdb1", "db123", "mydb25833", "db258330", "OTHER-27516"])
def test_mask_key_identifiers_keeps_identifiers_that_do_not_name_an_issue(text):
    assert mask_key_identifiers(text) == text


def test_stack_functions_take_the_first_distinct_frames_after_the_signal_noise():
    stack = "#0 0x1 in raise () from libc\n#1 0x2 in abort ()\n  #2 0x3 in heap_update (a=1)\n#3 heap_update ()\n"
    stack += "#4 qexec_run ()"
    assert stack_functions(stack) == ["heap_update", "qexec_run"]
    assert stack_functions(stack, 1) == ["heap_update"]
    assert stack_functions("call heap_update (x) here") == []


def test_identifiers_and_error_codes_in_order_of_first_appearance():
    text = "heap_file.c: heap_update_home failed with ER_HEAP_UNKNOWN_OBJECT (-1114) in QueryExecutor; heap_update_home"
    assert identifiers(text) == ["heap_file.c", "heap_update_home", "ER_HEAP_UNKNOWN_OBJECT", "QueryExecutor"]
    assert error_codes(text) == ["ER_HEAP_UNKNOWN_OBJECT", "-1114"]
    assert error_codes("dates 2026-11-09 and ranges 0-2 are not codes") == []
