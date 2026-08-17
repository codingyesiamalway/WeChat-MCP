from __future__ import annotations

from unittest.mock import patch

from wechat_mcp.fetch_messages_by_chat_utils import (
    ChatMessage,
    _merge_older_messages,
    fetch_recent_messages,
    scroll_to_bottom,
)


def message(text: str) -> ChatMessage:
    return ChatMessage(sender="UNKNOWN", text=text)


def test_merge_uses_longest_overlap_when_anchor_text_repeats() -> None:
    visible = [message("old"), message("same"), message("same")]
    collected = [message("same"), message("same"), message("new")]

    merged, added = _merge_older_messages(visible, collected)

    assert [item.text for item in merged] == ["old", "same", "same", "new"]
    assert added == 1


@patch("wechat_mcp.fetch_messages_by_chat_utils.time.sleep")
@patch("wechat_mcp.fetch_messages_by_chat_utils._scroll_and_wait_for_change")
def test_scroll_to_bottom_is_not_limited_to_forty_scrolls(
    scroll_and_wait, _sleep
) -> None:
    scroll_and_wait.side_effect = [True] * 45 + [False] * 3

    scroll_to_bottom(object(), (10.0, 20.0), stable_attempts=3)

    assert scroll_and_wait.call_count == 48
    assert all(item.args[1:] == ((10.0, 20.0), -1000) for item in scroll_and_wait.call_args_list)


@patch("wechat_mcp.fetch_messages_by_chat_utils._scroll_and_wait_for_change")
@patch("wechat_mcp.fetch_messages_by_chat_utils._collect_visible_messages")
@patch("wechat_mcp.fetch_messages_by_chat_utils.scroll_to_bottom")
@patch("wechat_mcp.fetch_messages_by_chat_utils.get_list_center")
@patch("wechat_mcp.fetch_messages_by_chat_utils.get_messages_list")
@patch("wechat_mcp.fetch_messages_by_chat_utils.get_wechat_ax_app")
def test_fetch_keeps_scrolling_when_viewport_moves_without_new_text(
    get_app,
    get_list,
    get_center,
    _scroll_bottom,
    collect_visible,
    scroll_and_wait,
) -> None:
    app = object()
    message_list = object()
    get_app.return_value = app
    get_list.return_value = message_list
    get_center.return_value = (10.0, 20.0)
    collect_visible.side_effect = [
        [message("middle"), message("new")],
        *[[message("middle"), message("new")]] * 6,
        [message("old"), message("middle")],
    ]
    scroll_and_wait.return_value = True

    result = fetch_recent_messages(last_n=3)

    assert [item.text for item in result] == ["old", "middle", "new"]
    assert scroll_and_wait.call_count == 7


@patch("wechat_mcp.fetch_messages_by_chat_utils._scroll_and_wait_for_change")
@patch("wechat_mcp.fetch_messages_by_chat_utils._collect_visible_messages")
@patch("wechat_mcp.fetch_messages_by_chat_utils.scroll_to_bottom")
@patch("wechat_mcp.fetch_messages_by_chat_utils.get_list_center")
@patch("wechat_mcp.fetch_messages_by_chat_utils.get_messages_list")
@patch("wechat_mcp.fetch_messages_by_chat_utils.get_wechat_ax_app")
def test_fetch_stops_after_five_confirmed_stalled_scrolls(
    get_app,
    get_list,
    get_center,
    _scroll_bottom,
    collect_visible,
    scroll_and_wait,
) -> None:
    app = object()
    message_list = object()
    get_app.return_value = app
    get_list.return_value = message_list
    get_center.return_value = (10.0, 20.0)
    collect_visible.return_value = [message("oldest"), message("newest")]
    scroll_and_wait.return_value = False

    result = fetch_recent_messages(last_n=100)

    assert [item.text for item in result] == ["oldest", "newest"]
    assert scroll_and_wait.call_count == 5
