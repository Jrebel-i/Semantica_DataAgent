#!/usr/bin/env python3
"""Measure embedding throughput with synthetic text.

Usage:
    uv run --with httpx scripts/embedding_bench.py \
        http://172.16.242.124:31603 model/bge-m3 16,64 1,2,4,8 20

Arguments are the base URL, model id, comma-separated batch sizes,
comma-separated concurrency values, and optional seconds per combination
(default 20). The output lists texts/s, request latency p50/p95, and failures
for every combination. Text is generated from fixed word lists, so the
benchmark reads no real data.
"""

from __future__ import annotations

import asyncio
import random
import sys
import time

import httpx

WORDS_ZH = [
    "订单",
    "金额",
    "销售",
    "机票",
    "供应商",
    "买家",
    "航司",
    "分区",
    "指标",
    "口径",
    "出票",
    "退款",
    "状态",
    "渠道",
    "日期",
    "汇总",
    "明细",
    "维度",
    "字段",
    "表",
    "注释",
]
WORDS_EN = [
    "order",
    "amount",
    "supplier",
    "buyer",
    "carrier",
    "partition",
    "metric",
    "status",
    "channel",
    "date",
    "total",
    "detail",
    "dimension",
    "field",
    "table",
    "comment",
]

random.seed(7)


def synthetic_text() -> str:
    length = random.randint(8, 40)
    words = WORDS_ZH if random.random() < 0.5 else WORDS_EN
    return " ".join(random.choice(words) for _ in range(length))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    return values[min(len(values) - 1, int(len(values) * fraction))]


async def run_combination(
    base_url: str, model: str, batch_size: int, concurrency: int, seconds: float
) -> tuple[float, float, float, int]:
    latencies: list[float] = []
    failures = 0
    completed = 0
    deadline = time.monotonic() + seconds

    async with httpx.AsyncClient(base_url=base_url, timeout=300) as client:

        async def worker() -> None:
            nonlocal completed, failures
            while time.monotonic() < deadline:
                payload = {
                    "model": model,
                    "input": [synthetic_text() for _ in range(batch_size)],
                }
                started = time.monotonic()
                try:
                    response = await client.post("/embeddings", json=payload)
                    response.raise_for_status()
                    latencies.append(time.monotonic() - started)
                    completed += batch_size
                except httpx.HTTPError:
                    failures += 1

        started = time.monotonic()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        elapsed = time.monotonic() - started

    latencies.sort()
    throughput = completed / elapsed if elapsed else float("nan")
    return throughput, percentile(latencies, 0.5), percentile(latencies, 0.95), failures


def parse_args(argv: list[str]) -> tuple[str, str, list[int], list[int], float]:
    if len(argv) not in (4, 5):
        raise SystemExit(
            "usage: embedding_bench.py <base_url> <model> <batch sizes> "
            "<concurrency values> [seconds]"
        )
    base_url, model = argv[0].rstrip("/"), argv[1]
    batches = [int(value) for value in argv[2].split(",")]
    concurrencies = [int(value) for value in argv[3].split(",")]
    seconds = float(argv[4]) if len(argv) == 5 else 20.0
    if any(value <= 0 for value in batches + concurrencies) or seconds <= 0:
        raise SystemExit("batch sizes, concurrency values and seconds must be positive")
    return base_url, model, batches, concurrencies, seconds


async def main(argv: list[str]) -> int:
    base_url, model, batches, concurrencies, seconds = parse_args(argv)
    print(
        f"{'批大小':>6} {'并发':>4} {'texts/s':>9} "
        f"{'p50(s)':>8} {'p95(s)':>8} {'失败':>4}"
    )
    for batch_size in batches:
        for concurrency in concurrencies:
            throughput, p50, p95, failures = await run_combination(
                base_url, model, batch_size, concurrency, seconds
            )
            print(
                f"{batch_size:>6} {concurrency:>4} {throughput:>9.1f} "
                f"{p50:>8.2f} {p95:>8.2f} {failures:>4}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
