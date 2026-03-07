from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class SourceCheck:
    source_key: str
    source_name: str
    source_type: str
    display_name: str
    status: Literal["ONLINE", "OFFLINE"]
    error_message: str | None = None
    details: dict = field(default_factory=dict)
