"""
Converts Markdown text to Quill Delta format.

Quill Delta is a JSON-based rich text format used by Journiv to store entry content.
Each entry's content is: {"ops": [{"insert": "text"}, {"insert": "\n", "attributes": {...}}, ...]}

Block-level formatting (headers, lists, blockquotes) is expressed on the trailing \n
of each block. Inline formatting (bold, italic, code) is expressed on the text span.
"""

import re
from typing import Any


def _text_ops(text: str) -> list[dict]:
    """Parse inline Markdown (bold, italic, code, links) into Quill ops."""
    ops = []
    # Pattern order matters: bold before italic to avoid partial matches
    pattern = re.compile(
        r"\*\*(.+?)\*\*"       # **bold**
        r"|\*(.+?)\*"           # *italic*
        r"|`(.+?)`"             # `code`
        r"|__(.+?)__"           # __bold__
        r"|_(.+?)_"             # _italic_
        r"|\[(.+?)\]\((.+?)\)"  # [text](url)
    )
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            ops.append({"insert": text[pos:m.start()]})
        if m.group(1):
            ops.append({"insert": m.group(1), "attributes": {"bold": True}})
        elif m.group(2):
            ops.append({"insert": m.group(2), "attributes": {"italic": True}})
        elif m.group(3):
            ops.append({"insert": m.group(3), "attributes": {"code": True}})
        elif m.group(4):
            ops.append({"insert": m.group(4), "attributes": {"bold": True}})
        elif m.group(5):
            ops.append({"insert": m.group(5), "attributes": {"italic": True}})
        elif m.group(6) and m.group(7):
            ops.append({"insert": m.group(6), "attributes": {"link": m.group(7)}})
        pos = m.end()
    if pos < len(text):
        ops.append({"insert": text[pos:]})
    return ops


def md_to_quill_delta(markdown: str) -> dict[str, Any]:
    """
    Convert a Markdown string to a Quill Delta object.

    Returns: {"ops": [...]} ready to be serialised into Journal.json.
    """
    ops: list[dict] = []
    lines = markdown.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.startswith("```"):
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                ops.append({"insert": lines[i] + "\n", "attributes": {"code-block": True}})
                i += 1
            i += 1  # skip closing ```
            continue

        # ATX heading
        heading_match = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading_match:
            level = len(heading_match.group(1))
            content = heading_match.group(2).strip()
            ops.extend(_text_ops(content))
            ops.append({"insert": "\n", "attributes": {"header": level}})
            i += 1
            continue

        # Unordered list item
        ul_match = re.match(r"^[-*+]\s+(.*)", line)
        if ul_match:
            ops.extend(_text_ops(ul_match.group(1)))
            ops.append({"insert": "\n", "attributes": {"list": "bullet"}})
            i += 1
            continue

        # Ordered list item
        ol_match = re.match(r"^\d+\.\s+(.*)", line)
        if ol_match:
            ops.extend(_text_ops(ol_match.group(1)))
            ops.append({"insert": "\n", "attributes": {"list": "ordered"}})
            i += 1
            continue

        # Blockquote
        bq_match = re.match(r"^>\s*(.*)", line)
        if bq_match:
            ops.extend(_text_ops(bq_match.group(1)))
            ops.append({"insert": "\n", "attributes": {"blockquote": True}})
            i += 1
            continue

        # Blank line → paragraph break (Quill uses \n for paragraph spacing)
        if line.strip() == "":
            ops.append({"insert": "\n"})
            i += 1
            continue

        # Plain paragraph line
        ops.extend(_text_ops(line))
        ops.append({"insert": "\n"})
        i += 1

    # Quill Delta must end with a newline
    if not ops or ops[-1].get("insert", "")[-1:] != "\n":
        ops.append({"insert": "\n"})

    return {"ops": ops}
