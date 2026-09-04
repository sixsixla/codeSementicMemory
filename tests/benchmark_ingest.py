"""Manual Phase 1 latency probe: ``py tests/benchmark_ingest.py``."""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

from codememory.ingest.service import IngestService
from codememory.storage.database import Database
from codememory.storage.repository import MemoryRepository


def main() -> None:
    rows = [
        json.loads(line)
        for line in (Path(__file__).parents[1] / "fixtures" / "route-success.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    database = Database(Path(tempfile.gettempdir()) / f"codememory-p95-{time.time_ns()}.sqlite3")
    service = IngestService(MemoryRepository(database))
    timings: list[float] = []
    for index in range(100):
        event = dict(rows[1])
        event["event_id"] = f"p95-{index}"
        event["external_event_id"] = f"p95-ext-{index}"
        event["seq"] = index
        started = time.perf_counter()
        service.ingest(event)
        timings.append((time.perf_counter() - started) * 1000)
    print(
        "p50_ms=%.2f p95_ms=%.2f max_ms=%.2f"
        % (statistics.median(timings), statistics.quantiles(timings, n=20)[18], max(timings))
    )


if __name__ == "__main__":
    main()
