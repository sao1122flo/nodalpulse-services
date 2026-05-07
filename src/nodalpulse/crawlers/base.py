from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RawFiling:
    source_slug: str
    external_id: str
    doc_type: str
    title: str
    source_url: str
    filed_at: str  # ISO 8601
    content: bytes
    file_ext: str
    metadata: dict


class BaseCrawler(ABC):
    source_slug: str

    @abstractmethod
    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        """Fetch filings not yet seen. since = ISO date string."""
        ...
