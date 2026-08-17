from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ApplicationServices import (
    kAXChildrenAttribute,
    kAXListRole,
    kAXPositionAttribute,
    kAXSizeAttribute,
    kAXTitleAttribute,
    kAXValueAttribute,
)
from PIL import ImageGrab

from .logging_config import logger
from .wechat_accessibility import (
    ax_get,
    axvalue_to_point,
    axvalue_to_size,
    get_list_center,
    get_wechat_ax_app,
    post_scroll,
    dfs,
)


def get_messages_list(ax_app: Any) -> Any:
    """
    Find the AX list that contains chat messages in the current WeChat window.
    """

    def is_message_list(el, role, title, identifier):
        return role == kAXListRole and (title or "") == "Messages"

    msg_list = dfs(ax_app, is_message_list)
    if msg_list is None:
        raise RuntimeError("Could not find WeChat 'Messages' list in AX tree")
    return msg_list


def capture_message_area(msg_list: Any):
    """
    Capture a screenshot of the visible message area for the given list and
    return the image together with the list origin and size.
    """
    pos_ref = ax_get(msg_list, kAXPositionAttribute)
    size_ref = ax_get(msg_list, kAXSizeAttribute)
    origin = axvalue_to_point(pos_ref)
    size = axvalue_to_size(size_ref)
    if origin is None or size is None:
        raise RuntimeError("Failed to get bounds for WeChat messages list")

    x, y = origin
    w, h = size

    bbox = (int(x), int(y), int(x + w), int(y + h))
    image = ImageGrab.grab(bbox=bbox)
    return image, origin, size


def _viewport_signature(msg_list: Any) -> tuple[tuple[str, float | None], ...]:
    """Return visible message text and vertical positions for scroll detection."""
    signature: list[tuple[str, float | None]] = []
    children = ax_get(msg_list, kAXChildrenAttribute) or []
    for child in children:
        text = ax_get(child, kAXValueAttribute) or ax_get(child, kAXTitleAttribute)
        if not text:
            continue
        point = axvalue_to_point(ax_get(child, kAXPositionAttribute))
        y = round(point[1], 1) if point is not None else None
        signature.append((str(text), y))
    return tuple(signature)


def _wait_for_viewport_change(
    msg_list: Any,
    previous: tuple[tuple[str, float | None], ...],
    timeout: float = 0.75,
    poll_interval: float = 0.05,
) -> tuple[tuple[str, float | None], ...]:
    """Wait briefly for WeChat to apply a scroll or lazy-load history."""
    deadline = time.monotonic() + timeout
    current = _viewport_signature(msg_list)
    while current == previous and time.monotonic() < deadline:
        time.sleep(poll_interval)
        current = _viewport_signature(msg_list)
    return current


def _scroll_and_wait_for_change(
    msg_list: Any,
    center: tuple[float, float],
    delta_lines: int,
) -> bool:
    """Post one scroll event and report whether the message viewport moved."""
    previous = _viewport_signature(msg_list)
    post_scroll(center, delta_lines)
    current = _wait_for_viewport_change(msg_list, previous)
    return current != previous


def scroll_to_bottom(
    msg_list: Any,
    center: tuple[float, float],
    max_scrolls: int = 1000,
    stable_attempts: int = 5,
) -> None:
    """
    Scroll the messages list to the bottom (newest messages) by repeatedly
    sending large negative scroll events until the last visible message
    stabilizes.
    """
    stable = 0
    for _ in range(max_scrolls):
        if _scroll_and_wait_for_change(msg_list, center, -1000):
            stable = 0
            continue

        stable += 1
        if stable >= stable_attempts:
            break
    else:
        logger.warning(
            "Could not confirm the bottom of chat history after %d scrolls",
            max_scrolls,
        )

    time.sleep(0.2)


def count_colored_pixels(
    image, left: float, top: float, right: float, bottom: float
) -> tuple[int, int]:
    left_i = max(0, int(left))
    top_i = max(0, int(top))
    right_i = min(image.width, int(right))
    bottom_i = min(image.height, int(bottom))
    if right_i <= left_i or bottom_i <= top_i:
        return 0, 0

    region = image.crop((left_i, top_i, right_i, bottom_i)).convert("RGB")
    pixels = region.load()

    width, height = region.size
    colored = 0
    total = width * height

    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            brightness = (r + g + b) / 3.0
            if brightness < 20:
                continue
            if brightness > 40 or (max(r, g, b) - min(r, g, b)) > 10:
                colored += 1

    return colored, total


SenderLabel = Literal["ME", "OTHER", "UNKNOWN"]


def classify_sender_for_message(
    image, list_origin, message_pos, message_size
) -> SenderLabel:
    """
    Heuristic classification of a message sender by sampling coloured pixels on
    the left/right side of the message bubble.
    """
    list_x, list_y = list_origin
    msg_x, msg_y = message_pos
    msg_w, msg_h = message_size

    rel_x = msg_x - list_x
    rel_y = msg_y - list_y

    band_height = min(40.0, msg_h)
    center_y = rel_y + msg_h / 2.0
    top = center_y - band_height / 2.0
    bottom = top + band_height

    margin = 5.0
    sample_width = min(100.0, msg_w / 3.0)

    left_left = rel_x + margin
    left_right = left_left + sample_width

    right_right = rel_x + msg_w - margin
    right_left = right_right - sample_width

    left_colored, left_total = count_colored_pixels(
        image, left_left, top, left_right, bottom
    )
    right_colored, right_total = count_colored_pixels(
        image, right_left, top, right_right, bottom
    )

    avg_area = (left_total + right_total) / 2.0 if (left_total + right_total) else 0.0
    min_signal = max(10.0, avg_area * 0.01)

    if left_colored < min_signal and right_colored < min_signal:
        return "UNKNOWN"

    if right_colored > left_colored * 1.5:
        return "ME"
    if left_colored > right_colored * 1.5:
        return "OTHER"
    return "UNKNOWN"


@dataclass
class ChatMessage:
    sender: SenderLabel
    text: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _merge_older_messages(
    visible: list[ChatMessage], messages: list[ChatMessage]
) -> tuple[list[ChatMessage], int]:
    """Prepend a viewport using its longest overlap with collected messages."""
    if not messages:
        return list(visible), len(visible)

    visible_text = [message.text for message in visible]
    message_text = [message.text for message in messages]
    max_overlap = min(len(visible_text), len(message_text))

    for overlap in range(max_overlap, 0, -1):
        if visible_text[-overlap:] == message_text[:overlap]:
            new_older = visible[:-overlap]
            return new_older + messages, len(new_older)

    return visible + messages, len(visible)


def _collect_visible_messages(msg_list: Any) -> list[ChatMessage]:
    """Capture and classify the messages currently exposed by Accessibility."""
    image, list_origin, _ = capture_message_area(msg_list)
    children = ax_get(msg_list, kAXChildrenAttribute) or []
    visible: list[ChatMessage] = []

    for child in children:
        text = ax_get(child, kAXValueAttribute) or ax_get(child, kAXTitleAttribute)
        if not text:
            continue

        point = axvalue_to_point(ax_get(child, kAXPositionAttribute))
        size = axvalue_to_size(ax_get(child, kAXSizeAttribute))
        if point is None or size is None:
            sender: SenderLabel = "UNKNOWN"
        else:
            sender = classify_sender_for_message(image, list_origin, point, size)

        visible.append(ChatMessage(sender=sender, text=str(text)))

    return visible


def fetch_recent_messages(
    last_n: int = 100, max_scrolls: int | None = None
) -> list[ChatMessage]:
    """
    Fetch the true last N messages from the currently open chat, even
    when the history spans multiple screens.

    Uses a scrolling strategy that involves:
    - Scrolls to the bottom of the chat history.
    - Repeatedly scrolls upwards in small steps.
    - At each position, captures a screenshot of the message area and
      collects all visible messages plus their positions/sizes.
    - Classifies each message as ME/OTHER/UNKNOWN using the same
      screenshot-based heuristic as before.
    - Waits for each viewport change so lazy-loaded history is not mistaken
      for the top of the conversation.
    - Merges newly revealed older messages using the longest shared sequence,
      which is resilient to repeated message text.
    """
    ax_app = get_wechat_ax_app()
    msg_list = get_messages_list(ax_app)
    center = get_list_center(msg_list)
    scroll_to_bottom(msg_list, center)

    messages: list[ChatMessage] = []
    scrolls = 0
    stalled_scrolls = 0

    while True:
        visible = _collect_visible_messages(msg_list)

        if not visible:
            break

        messages, added = _merge_older_messages(visible, messages)
        if added:
            logger.debug("Collected %d older visible messages", added)

        if len(messages) >= last_n:
            break

        scrolls += 1
        if max_scrolls is not None and scrolls >= max_scrolls:
            break

        if _scroll_and_wait_for_change(msg_list, center, 50):
            stalled_scrolls = 0
        else:
            stalled_scrolls += 1
            if stalled_scrolls >= 5:
                logger.info("Reached the top of the currently available chat history")
                break

    if len(messages) > last_n:
        messages = messages[-last_n:]

    logger.info(
        "Fetched %d messages from current chat (requested last_n=%d)",
        len(messages),
        last_n,
    )
    return messages
