"""Downloader for FINTO value vocabularies and ontologies via Skosmos REST API using httpx2."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import httpx2

from reconciliator.progress import ProgressTracker
from reconciliator.version import get_default_user_agent


class FintoDownloader:
    """Client for discovering and downloading SKOS vocabularies from Finto's Skosmos REST API."""

    def __init__(
        self,
        api_url: str = "https://api.finto.fi/rest/v1",
        timeout_seconds: float = 120.0,
        user_agent: Optional[str] = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent if user_agent is not None else get_default_user_agent()

    def list_vocabularies(self, lang: str = "fi") -> list[dict[str, Any]]:
        """
        Query Finto REST API to list all available vocabularies.

        Returns a list of vocabulary metadata dictionaries (e.g., id, title, uri, languages).
        """
        url = f"{self.api_url}/vocabularies"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        with httpx2.Client(
            timeout=self.timeout_seconds, headers=headers, follow_redirects=True
        ) as client:
            resp = client.get(url, params={"lang": lang})
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return list(data.get("vocabularies", []))
            return []

    def download_vocabulary(
        self,
        vocab_id: str,
        output_dir: Path | str,
        overwrite: bool = False,
        format: str = "text/turtle",
    ) -> Path:
        """
        Download a single vocabulary in SKOS format (default: Turtle) to output_dir.

        Uses atomic file replacement to avoid leaving corrupt or partial files.
        """
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        ext = ".ttl" if "turtle" in format else ".rdf"
        dest_file = out_path / f"{vocab_id}{ext}"

        if dest_file.exists() and not overwrite:
            return dest_file

        tmp_file = out_path / f"{vocab_id}{ext}.tmp"
        url = f"{self.api_url}/{vocab_id}/data"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": f"{format}, application/x-turtle, text/n3;q=0.9, */*;q=0.1",
        }

        try:
            with httpx2.Client(
                timeout=self.timeout_seconds, headers=headers, follow_redirects=True
            ) as client:
                with client.stream("GET", url, params={"format": format}) as response:
                    response.raise_for_status()
                    with open(tmp_file, "wb") as out_f:
                        for chunk in response.iter_bytes(chunk_size=64 * 1024):
                            out_f.write(chunk)
            tmp_file.replace(dest_file)
            return dest_file
        except Exception:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise

    def download_vocabularies(
        self,
        vocab_ids: list[str],
        output_dir: Path | str,
        overwrite: bool = False,
        format: str = "text/turtle",
        tracker: Optional[ProgressTracker] = None,
        on_progress: Optional[Callable[[str, int, int], None]] = None,
    ) -> list[Path]:
        """
        Download multiple vocabularies to output_dir sequentially.

        Optionally updates a ProgressTracker or calls an on_progress(vocab_id, index, total) callback.
        """
        downloaded: list[Path] = []
        total = len(vocab_ids)

        if tracker:
            tracker.start_task(stage="downloading_finto")
            tracker.update_progress(items_total=total, force=True)

        for idx, v_id in enumerate(vocab_ids, start=1):
            if on_progress:
                on_progress(v_id, idx, total)
            dest = self.download_vocabulary(
                v_id, output_dir=output_dir, overwrite=overwrite, format=format
            )
            downloaded.append(dest)

            if tracker:
                tracker.update_progress(items_increment=1)

        if tracker:
            tracker.complete_task(
                metadata={"vocabularies_downloaded": [str(p.name) for p in downloaded]}
            )

        return downloaded
