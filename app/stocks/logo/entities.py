"""Enterprise Business Rules: the logo slice's own entity.

Pure domain object — imports nothing from the rest of the app, the web
framework, or the logo vendor.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Logo:
    """A company's logo image plus its MIME type, ready to serve as-is."""

    content: bytes
    media_type: str
