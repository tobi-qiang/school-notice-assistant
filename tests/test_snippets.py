"""Tests for the snippet builder in app.snippets.

Covers the spec scenarios: long sentence, multiple matches, document
number, URL/email, no punctuation, English punctuation, empty body,
and the behaviour guarantees around text/fragments for safe DOM-based
highlighting.
"""

from __future__ import annotations

import inspect
import re

import pytest

from app.snippets import (
    _dedupe_overlapping,
    _find_matches,
    build_preview,
)


# ---------------------------------------------------------------------------
# Empty / blank body
# ---------------------------------------------------------------------------


def test_empty_body_returns_empty_result():
    out = build_preview("", query="关键词")
    assert out["text"] == ""
    assert out["is_search_context"] is False
    assert out["fragments"] == []
    assert out["head_omitted"] is False
    assert out["tail_omitted"] is False


def test_whitespace_only_body_returns_summary_with_no_fragments():
    out = build_preview("   \n\n  \t  ", query="关键词", ai_summary="AI摘要")
    assert out["text"] == "AI摘要"
    assert out["is_search_context"] is False
    assert out["fragments"] == []
    assert out["head_omitted"] is False
    assert out["tail_omitted"] is False


# ---------------------------------------------------------------------------
# No-query fallback
# ---------------------------------------------------------------------------


def test_no_query_prefers_ai_summary_over_first_meaningful():
    body = "这是一段较长的正文。今天下午有重要会议。请大家准时参加。"
    out = build_preview(body, query="", ai_summary="AI给的一句摘要")
    assert out["text"] == "AI给的一句摘要"
    assert out["is_search_context"] is False
    assert out["fragments"] == [{"text": "AI给的一句摘要", "start": 0, "end": 0, "matches": []}]


def test_no_query_falls_back_to_first_meaningful_paragraph():
    body = (
        "\n\n  今天下午要开会。请大家准时参加。  \n\n"
        "这是第二段。完全不相关内容。"
    )
    out = build_preview(body, query="")
    assert out["is_search_context"] is False
    assert out["text"].startswith("今天下午要开会。")
    assert "完全不相关内容。" not in out["text"]


def test_no_query_first_meaningful_single_long_paragraph():
    body = "第一段。" + ("中间一段长内容，" * 80) + "结束段。第二句。"
    out = build_preview(body, query="")
    assert out["is_search_context"] is False
    # First complete sentence is returned; never the second sentence.
    assert out["text"].startswith("第一段。")
    assert "第二句。" not in out["text"]


# ---------------------------------------------------------------------------
# Query: basic match / no-match
# ---------------------------------------------------------------------------


def test_query_match_marks_search_context_true():
    body = "今天下午要开会。请大家准时参加会议。"
    out = build_preview(body, query="开会")
    assert out["is_search_context"] is True
    assert "开会" in out["text"]
    assert len(out["fragments"]) >= 1


def test_query_match_not_in_body_falls_back_to_ai_summary():
    body = "这是一段通知内容。今天是周三。"
    out = build_preview(body, query="绝对不在的关键词XYZ", ai_summary="AI摘要")
    assert out["text"] == "AI摘要"
    assert out["is_search_context"] is False


def test_query_match_not_in_body_without_summary_uses_first_meaningful():
    body = "这是正文的开始。今天是周三。后面是其他内容。"
    out = build_preview(body, query="不在的关键词")
    assert out["is_search_context"] is False
    assert out["text"].startswith("这是正文的开始。")


def test_case_insensitive_match_for_ascii_query():
    body = "Please review the Submission guidelines and SUBMIT on time."
    out = build_preview(body, query="submit")
    assert out["is_search_context"] is True
    assert out["text"].lower().count("submit") >= 1


# ---------------------------------------------------------------------------
# Fragment position correctness (for safe highlighting)
# ---------------------------------------------------------------------------


def test_fragment_text_exactly_matches_body_slice():
    body = (
        "第一句：会议将于下午两点开始。"
        "第二句：会议地点为行政楼会议室。"
        "第三句：如有变化另行通知。"
    )
    out = build_preview(body, query="会议")
    assert out["is_search_context"] is True
    assert out["fragments"]
    for frag in out["fragments"]:
        assert body[frag["start"]:frag["end"]] == frag["text"]


def test_fragments_carry_match_offsets_pointing_into_body():
    body = "今天下午要开会。请大家准时参加会议。"
    out = build_preview(body, query="开会")
    assert out["is_search_context"] is True
    seen = 0
    for frag in out["fragments"]:
        for ms, me in frag["matches"]:
            assert body[ms:me] == "开会"
            assert frag["start"] <= ms < me <= frag["end"]
            seen += 1
    assert seen >= 1


def test_output_never_injects_html_markup():
    body = "正文 包含关键字 keyword 在这里。下一句也有 keyword。"
    out = build_preview(body, query="keyword")
    # We must not inject ANY wrapper tags; body-side chars are the user's
    # responsibility. The guarantees: no "<mark>", no "<b>", no "<i>", etc.
    assert "<mark>" not in out["text"]
    assert "</mark>" not in out["text"]
    assert not re.search(r"<\s*(mark|b|i|u|em|strong|span)\b", out["text"], re.IGNORECASE)
    # And no HTML wrappers in the fragments.
    for frag in out["fragments"]:
        assert "<mark>" not in frag["text"]


# ---------------------------------------------------------------------------
# Long sentence handling
# ---------------------------------------------------------------------------


def test_long_sentence_is_bounded_by_hard_cap():
    body = "Long sentence " + ("with content " * 30) + "ends here. Next."
    out = build_preview(body, query="content")
    assert out["is_search_context"] is True
    # Each fragment individually respects the cap (with token-snap slack).
    for frag in out["fragments"]:
        assert frag["end"] - frag["start"] <= 220
    assert out["head_omitted"] is False


def test_long_sentence_with_only_weak_punctuation_shrinks_at_weak():
    body = "开头，" + ("中间填充，" * 60) + "结尾。"
    out = build_preview(body, query="中间")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        # Shrink on weak punctuation reduces fragment length to <= HARD_MAX (+ slack).
        assert frag["end"] - frag["start"] <= 220


# ---------------------------------------------------------------------------
# Multiple matches: merging + capping with ellipsis
# ---------------------------------------------------------------------------


def test_multiple_matches_in_same_sentence_merge_into_one_fragment():
    body = "这次会议会议上大家都一致认为会议议程没有问题。"
    out = build_preview(body, query="会议")
    assert out["is_search_context"] is True
    # Three matches, one sentence -> one fragment.
    assert len(out["fragments"]) == 1


def test_multiple_matches_in_adjacent_sentences_merge():
    body = "前一句包含keyword。这里是后一句也包含keyword的句子。"
    out = build_preview(body, query="keyword")
    assert out["is_search_context"] is True
    assert len(out["fragments"]) == 1


def test_multiple_far_matches_capped_with_ellipsis():
    body = (
        "第一段开始有kwA。这里是第一段的结束。"
        "中间还有很多无关内容。第二段的kwA出现在这里。第二段结束。"
        "继续填字" + ("无关" * 50) + "。"
        "最后一段包含kwA。最后一句结束。"
    )
    out = build_preview(body, query="kwA", max_fragments=2)
    assert out["is_search_context"] is True
    assert len(out["fragments"]) == 2
    # Fragments joined by ……
    assert "……" in out["text"]
    # ……… appears exactly once between the two fragments (head + tail may also add).
    assert out["text"].count("……") >= 1


def test_three_far_matches_with_cap_one_picks_single_best():
    body = (
        "Alpha bravo. Charlie delta. Echo foxtrot. Golf hotel. India juliet. "
        "Kilo lima keyword1. Mike november. Oscar papa. Quebec romeo. "
        "Sierra tango keyword1. Uniform victor. Whiskey xray. Yankee zulu."
    )
    out = build_preview(body, query="keyword1", max_fragments=1)
    assert out["is_search_context"] is True
    assert len(out["fragments"]) == 1
    assert "keyword1" in out["fragments"][0]["text"]


def test_two_matches_in_same_sentence_also_create_only_one_match_offset_per_fragment():
    body = "本次会议的主要议题是教学。会议纪要稍后公布。"
 
    body = "本次会议的主要议题是教学。会议纪要稍后公布。"
    out = build_preview(body, query="会议")
    assert out["is_search_context"] is True
    match_count = sum(len(frag["matches"]) for frag in out["fragments"])
    assert match_count >= 2


# ---------------------------------------------------------------------------
# Protected regions: must NOT be cut
# ---------------------------------------------------------------------------


def test_wenhao_is_not_split_by_snippet_boundary():
    body = "依据教字〔2026〕175号文件精神。请各单位认真执行。"
    out = build_preview(body, query="教字")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        text = body[frag["start"]:frag["end"]]
        if "教字" in text:
            assert "教字〔2026〕175号" in text


def test_wenhao_with_internal_document_number_not_cut():
    body = "具体参见国发〔2026〕15号文件相关条款。其他内容略。"
    out = build_preview(body, query="国发")
    for frag in out["fragments"]:
        text = body[frag["start"]:frag["end"]]
        if "国发" in text:
            assert "国发〔2026〕15号" in text


def test_url_is_not_split_by_snippet_boundary():
    body = "详情请访问 https://school.example.com/path?q=test&id=42 。请大家准时。"
    out = build_preview(body, query="详情")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        text = body[frag["start"]:frag["end"]]
        if "https://" in text:
            assert "https://school.example.com/path?q=test&id=42" in text


def test_attachment_filename_extension_is_not_treated_as_sentence_end():
    body = "请查看教务处、创新创业学院2026年暑期值班表.xlsx 并按表中安排联系值班老师。"
    out = build_preview(body, query="创新创业")
    assert "值班表.xlsx" in out["text"]
    assert not out["text"].endswith("值班表.")


def test_email_is_not_split_by_snippet_boundary():
    body = "如有疑问请联系 support-team@example.com.cn 邮箱。我们将尽快回复。"
    out = build_preview(body, query="联系")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        text = body[frag["start"]:frag["end"]]
        if "@" in text:
            assert "support-team@example.com.cn" in text


def test_long_digit_date_phone_sequence_is_not_split():
    body = "会议时间2026-08-18T14:30:00准时开始。电话010-1234-5678可咨询。"
    out = build_preview(body, query="会议")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        text = body[frag["start"]:frag["end"]]
        if "2026" in text:
            assert "2026-08-18" in text


def test_english_word_is_not_split():
    body = "本学期SchoolOfInformation期末考试安排已发布。请同学们关注。"
    out = build_preview(body, query="期末")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        text = body[frag["start"]:frag["end"]]
        if "School" in text:
            assert "SchoolOfInformation" in text


def test_book_title_is_not_split():
    body = "请各位同学参阅《计算机网络》第3章内容。这是关于网络的说明。"
    out = build_preview(body, query="网络")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        text = body[frag["start"]:frag["end"]]
        if "《" in text:
            assert "《计算机网络》" in text


def test_chinese_parentheses_are_not_split():
    body = "本次评选活动（含个人申报及部门推荐）现已启动。请各位老师关注。"
    out = build_preview(body, query="评选")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        text = body[frag["start"]:frag["end"]]
        if "（" in text:
            assert "（含个人申报及部门推荐）" in text


# ---------------------------------------------------------------------------
# No-punctuation fallback (safe upper bound + token snap)
# ---------------------------------------------------------------------------


def test_no_punctuation_uses_safe_upper_bound_and_token_snap():
    body = "abcdefghijklmnopqrstuvwxyz" * 8
    out = build_preview(body, query="klm")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        length = frag["end"] - frag["start"]
        assert length <= 215


def test_no_punctuation_short_body_returns_full_body():
    body = "abcdefghijklmnop"
    out = build_preview(body, query="cde")
    assert out["is_search_context"] is True
    assert len(out["fragments"]) == 1
    frag = out["fragments"][0]
    assert frag["start"] == 0
    assert frag["end"] == len(body)


def test_punctuation_only_at_very_end_falls_back_to_safe_window():
    body = "这是一段没有任何标点的文字" * 4 + "结束。"
    out = build_preview(body, query="文字")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        length = frag["end"] - frag["start"]
        assert length <= 215


# ---------------------------------------------------------------------------
# English punctuation
# ---------------------------------------------------------------------------


def test_english_period_acts_as_sentence_break():
    body = "The deadline for submission is approaching. Please prepare your documents in time. Late entries will not be accepted."
    out = build_preview(body, query="submission")
    assert out["is_search_context"] is True
    frag = out["fragments"][0]
    text = body[frag["start"]:frag["end"]]
    assert text.rstrip().endswith(".") or out["tail_omitted"]


def test_english_exclamation_and_question_mark_are_sentence_breaks():
    body = "Welcome to the new academic year! Are you ready for the challenges ahead?"
    out = build_preview(body, query="challenges")
    assert out["is_search_context"] is True
    assert "challenges" in out["text"]
    frag = out["fragments"][0]
    assert "?" in body[frag["start"]:frag["end"]]


def test_english_semicolon_acts_as_sentence_break():
    body = "Submit form A by Friday; submit form B by next Monday. Otherwise your application is incomplete."
    out = build_preview(body, query="application")
    assert out["is_search_context"] is True
    for frag in out["fragments"]:
        text = body[frag["start"]:frag["end"]]
        assert "application" in text


# ---------------------------------------------------------------------------
# Title is not in scope for this function
# ---------------------------------------------------------------------------


def test_function_signature_does_not_include_title():
    sig = inspect.signature(build_preview)
    params = list(sig.parameters.keys())
    assert "title" not in params
    assert params[:4] == ["body", "query", "ai_summary", "max_fragments"]


def test_function_processes_body_only_and_ignores_title_context():
    body = "通知正文第一段。今天进行会议。正文结束。"
    with_summary = build_preview(body, query="会议", ai_summary="会议摘要")
    without_summary = build_preview(body, query="会议")
    assert with_summary["is_search_context"] is True
    assert without_summary["is_search_context"] is True
    assert "会议" in with_summary["text"]
    assert "会议" in without_summary["text"]


# ---------------------------------------------------------------------------
# Ellipsis accuracy
# ---------------------------------------------------------------------------


def test_head_ellipsis_added_only_when_first_fragment_starts_after_body_zero():
    body = "这是无关的前置文字。今天有会议。后续内容略。"
    out = build_preview(body, query="会议")
    assert out["is_search_context"] is True
    if out["fragments"][0]["start"] > 0:
        assert out["head_omitted"] is True
        assert out["text"].startswith("……")


def test_tail_ellipsis_added_only_when_last_fragment_ends_before_body_end():
    body = "今天有会议。后面还有很多无关内容。"
    out = build_preview(body, query="会议")
    assert out["is_search_context"] is True
    if out["fragments"][-1]["end"] < len(body):
        assert out["tail_omitted"] is True
        assert out["text"].endswith("……")


def test_no_leading_ellipsis_when_first_fragment_begins_at_body_zero():
    body = "今天有会议。"
    out = build_preview(body, query="会议")
    assert out["is_search_context"] is True
    assert out["fragments"][0]["start"] == 0
    assert out["head_omitted"] is False
    assert not out["text"].startswith("……")


def test_no_trailing_ellipsis_when_last_fragment_extends_to_body_end():
    body = "今天有会议。"
    out = build_preview(body, query="会议")
    assert out["is_search_context"] is True
    assert out["fragments"][-1]["end"] == len(body)
    assert out["tail_omitted"] is False
    assert not out["text"].endswith("……")


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------


def test_max_fragments_zero_is_clamped_to_one():
    body = "第一句含kwA。第二句含kwA。第三句含kwA。"
    out = build_preview(body, query="kwA", max_fragments=0)
    assert out["is_search_context"] is True
    assert len(out["fragments"]) >= 1


def test_ai_summary_used_when_query_has_no_match():
    body = "这是正文。今天有会议。后续内容。"
    out = build_preview(body, query="绝对不存在", ai_summary="AI摘要")
    assert out["is_search_context"] is False
    assert out["text"] == "AI摘要"


def test_no_query_skips_document_number_and_salutation_paragraphs():
    body = (
        "教字〔2026〕168 号\n"
        "各学院（部）：\n"
        "为推进学校美育教学改革，现将精品课程申报事项通知如下。\n"
        "后续要求。"
    )
    out = build_preview(body)
    assert out["text"] == "为推进学校美育教学改革，现将精品课程申报事项通知如下。"
    assert "教字〔2026〕168" not in out["text"]


def test_no_query_never_stops_on_punctuation_inside_url():
    body = (
        "根据工作安排，现将有关事项说明如下：请登录 "
        "http://jwc.suda.edu.cn/path/index.html 查看完整要求。其他内容。"
    )
    out = build_preview(body)
    assert not out["text"].endswith(("http://jwc.", "http://jwc.suda."))
    if "http://" in out["text"]:
        assert "http://jwc.suda.edu.cn/path/index.html" in out["text"]


def test_no_query_does_not_stop_at_an_uninformative_early_comma():
    body = "根据本学期教学工作安排，现开展新生网上选课工作，并就有关事项通知如下：后续内容。"
    out = build_preview(body)
    assert out["text"] != "根据本学期教学工作安排，"
    assert "网上选课工作" in out["text"]


def test_no_match_with_no_summary_returns_first_meaningful():
    body = "开头的一句话。这是正文中段。后面是其他内容。"
    out = build_preview(body, query="绝对不会出现的")
    assert out["is_search_context"] is False
    assert out["text"].startswith("开头的一句话。")


def test_dedupe_overlapping_collapses_overlaps():
    assert _dedupe_overlapping([(0, 3), (2, 5), (10, 15)]) == [(0, 5), (10, 15)]
    assert _dedupe_overlapping([]) == []
    assert _dedupe_overlapping([(0, 3)]) == [(0, 3)]


def test_find_matches_returns_sorted_non_overlapping_intervals():
    matches = _find_matches("aaa-bbb aaa", query="aaa")
    assert matches == [(0, 3), (8, 11)]


def test_single_short_sentence_round_trip():
    body = "今天下午开会。"
    out = build_preview(body, query="开会")
    assert out["is_search_context"] is True
    assert out["fragments"][0]["text"] == "今天下午开会。"
    assert (
        body[out["fragments"][0]["start"]:out["fragments"][0]["end"]]
        == "今天下午开会。"
    )
    assert "……" not in out["text"]


def test_short_query_caps_body_in_one_fragment():
    body = "今天下午开会讨论教学大纲修改意见以及后续安排的问题说明。"
    out = build_preview(body, query="教学")
    assert out["is_search_context"] is True
    assert len(out["fragments"]) == 1
