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


class MarketAdapter(ABC):
    """Contract for all market/source crawlers.

    Implementations fetch raw filings from one source and emit normalized
    RawFiling objects. Everything downstream (R2 upload, DB persist, extraction
    queue) is shared and source-agnostic via run_adapter().
    """

    source_slug: str

    @abstractmethod
    async def fetch_new(self, since: str | None = None) -> list[RawFiling]:
        """Fetch filings not yet seen. since = ISO date string."""
        ...


# Backward-compat alias — existing crawler imports continue to work unchanged.
BaseCrawler = MarketAdapter
