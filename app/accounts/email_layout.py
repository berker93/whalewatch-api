"""One email, two bodies: hand-written table HTML and its plain-text twin.

A message is written once, as blocks (:data:`Block`): paragraphs of plain
text and buttons. Both bodies are drawn from the same blocks, so the text
alternative says everything the HTML does, links included, and every send
carries both.

The HTML is tables and inline styles, nothing else: no ``<style>`` block, no
web font, no image, no script. That is what Outlook's Word renderer and
Gmail's stripped-down CSS both draw the same way. Every string from a block is
escaped, so a name or a title with ``<`` in it is text, never markup.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import Final


@dataclass(frozen=True, slots=True)
class Button:
    """A link drawn as a button, with the address under it for a client that
    draws no buttons. In the text body: ``label: url``."""

    label: str
    url: str


#: A paragraph of plain text, or a button.
type Block = str | Button


@dataclass(frozen=True, slots=True)
class FooterLink:
    label: str
    url: str


#: A footer line: plain text, or a link.
type FooterLine = str | FooterLink

_FONT: Final = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_INK: Final = "#111111"
_BODY: Final = "#2b2b2b"
_MUTED: Final = "#6b6b6b"
_PAPER: Final = "#ffffff"
_PAGE: Final = "#f3f3f1"
_RULE: Final = "#e2e2de"


def text_body(blocks: Sequence[Block], footer: Sequence[FooterLine]) -> str:
    parts = [f"{b.label}: {b.url}" if isinstance(b, Button) else b for b in blocks]
    lines = [f"{f.label}: {f.url}" if isinstance(f, FooterLink) else f for f in footer]
    return "\n\n".join(parts) + "\n\n--\n" + "\n".join(lines) + "\n"


def _paragraph(text: str) -> str:
    return (
        f'<tr><td style="padding:16px 32px 0;font-family:{_FONT};font-size:15px;'
        f'line-height:1.6;color:{_BODY};">{escape(text)}</td></tr>'
    )


def _button(button: Button) -> str:
    url = escape(button.url)
    return (
        '<tr><td style="padding:24px 32px 0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="border-radius:4px;background:{_INK};">'
        f'<a href="{url}" style="display:inline-block;padding:12px 22px;font-family:{_FONT};'
        f'font-size:15px;font-weight:600;line-height:1;color:{_PAPER};text-decoration:none;">'
        f"{escape(button.label)}</a></td></tr></table>"
        f'<div style="padding-top:10px;font-family:{_FONT};font-size:12px;line-height:1.5;'
        f'color:{_MUTED};word-break:break-all;">Or open this link: '
        f'<a href="{url}" style="color:{_MUTED};">{url}</a></div></td></tr>'
    )


def _footer_line(line: FooterLine) -> str:
    if isinstance(line, FooterLink):
        content = (
            f'<a href="{escape(line.url)}" style="color:{_MUTED};text-decoration:underline;">'
            f"{escape(line.label)}</a>"
        )
    else:
        content = escape(line)
    return (
        f'<tr><td style="padding:4px 32px 0;font-family:{_FONT};font-size:12px;'
        f'line-height:1.5;color:{_MUTED};">{content}</td></tr>'
    )


def html_body(
    subject: str, blocks: Sequence[Block], footer: Sequence[FooterLine], *, preheader: str
) -> str:
    """:param preheader: The line an inbox shows after the subject: the first
    paragraph, usually. Hidden in the message itself."""
    rows = "".join(_button(b) if isinstance(b, Button) else _paragraph(b) for b in blocks)
    footer_rows = "".join(_footer_line(line) for line in footer)
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light">'
        f"<title>{escape(subject)}</title></head>"
        f'<body style="margin:0;padding:0;background:{_PAGE};">'
        f'<div style="display:none;max-height:0;overflow:hidden;">{escape(preheader)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{_PAGE};"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="max-width:560px;background:{_PAPER};border:1px solid {_RULE};border-radius:6px;">'
        f'<tr><td style="padding:24px 32px 0;font-family:{_FONT};font-size:16px;font-weight:700;'
        f'letter-spacing:-0.01em;color:{_INK};">WhaleWatch</td></tr>'
        f"{rows}"
        '<tr><td style="padding:0 0 28px;"></td></tr></table>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:560px;">'
        f'<tr><td style="padding:12px 0 0;"></td></tr>{footer_rows}</table>'
        "</td></tr></table></body></html>"
    )
