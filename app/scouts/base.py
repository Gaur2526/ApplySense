from abc import ABC, abstractmethod


class Scout(ABC):
    @abstractmethod
    async def fetch_jobs(self, board: str) -> list[dict]:
        """Fetch and normalize all currently open jobs from a source board."""
