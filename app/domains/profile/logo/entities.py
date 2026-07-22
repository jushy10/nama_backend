from dataclasses import dataclass


@dataclass(frozen=True)
class Logo:
    content: bytes
    media_type: str
