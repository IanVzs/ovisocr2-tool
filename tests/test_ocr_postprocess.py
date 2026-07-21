"""后处理单测（不依赖模型 / 网络）。"""

from ocr_tool.ocr_engine import DEFAULT_TASK, clean_repeated_substrings, get_prompt


def test_clean_repeated_substrings_short_unchanged() -> None:
    assert clean_repeated_substrings("abc") == "abc"


def test_clean_repeated_substrings_strips_tail_repeat() -> None:
    # 需超过 min_text_len(2000) 才触发尾部重复清理
    text = "正文内容。" * 100 + "重复" * 1000
    cleaned = clean_repeated_substrings(text)
    assert len(cleaned) < len(text)
    assert cleaned.startswith("正文内容。")


def test_get_prompt_defaults_to_doc_parse() -> None:
    assert get_prompt() == get_prompt(DEFAULT_TASK)
    assert "markdown" in get_prompt("doc_parse")
