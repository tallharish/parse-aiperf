#!/usr/bin/env python3
"""
aiperf_jsonl_exporter.py

Tails an aiperf profile_export.jsonl file (one JSON object per line, written
incrementally as a benchmark run progresses) and exposes its per-request
metrics as live Prometheus histograms on /metrics.

Unlike the summary CSV (which only has aggregate stats like avg/p99 after a
run finishes), this gives you the raw per-request distribution AS the run
happens — including the full inter_chunk_latency array per request, which is
the richest token-to-token latency data aiperf produces.

Usage:
    python3 aiperf_jsonl_exporter.py [path/to/profile_export.jsonl]

Then add a Prometheus scrape job for this exporter's port (default 9402,
chosen to sit alongside DCGM's 9400 and the NCCL exporter's 9401 from
earlier) and the metrics show up as aiperf_<metric_name>_<unit>_bucket etc.
"""

import sys
import json
import time
import argparse
from prometheus_client import start_http_server, Histogram

EXPORTER_PORT = 9402

# Only label by fields with a small, fixed set of possible values.
# metadata also contains x_request_id, x_correlation_id, conversation_id,
# root_correlation_id, worker_id, record_processor_id — all unbounded or
# high-cardinality identifiers. Labeling by any of those would create a
# new time series per request, forever. They're deliberately left out;
# if you need to look one up, that's what Loki/logs are for, not Prometheus.
LABEL_FIELDS = ["phase_name"]

# prometheus_client's DEFAULT histogram buckets top out at 10 and are designed
# for values in seconds. aiperf reports most latencies in milliseconds
# (TTFT ~400, request latency ~18000), so with the defaults every observation
# lands in the +Inf bucket and histogram_quantile() returns 10 for every
# percentile. Buckets must therefore match each metric's unit and range.
# Quantile precision is limited by bucket width (linear interpolation inside
# a bucket), so ITL gets fine buckets around its typical 30-45 ms range.
MS_BUCKETS = (
    0.5, 1, 2, 5, 10, 15, 20, 25, 30, 32, 34, 36, 38, 40, 45, 50, 60, 75, 100,
    150, 200, 250, 300, 400, 500, 600, 750, 1000, 1500, 2000, 3000, 5000,
    7500, 10000, 15000, 20000, 30000, 60000, 120000,
)
TOKEN_BUCKETS = (
    1, 5, 10, 25, 50, 100, 200, 300, 400, 500, 750, 1000, 1500, 2000, 4000,
    8000, 16000, 32000,
)
TOKENS_PER_SEC_BUCKETS = (
    1, 5, 10, 15, 20, 25, 30, 35, 40, 50, 75, 100, 250, 500, 1000, 2500,
    5000, 10000,
)
COUNT_BUCKETS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 750, 1000, 2500, 5000)
KB_BUCKETS = (0.5, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 4096)
RATIO_BUCKETS = (0.0, 0.25, 0.5, 0.75, 1.0)
PERCENT_BUCKETS = (-100, -50, -10, -5, -2, -1, -0.5, 0, 0.5, 1, 2, 5, 10, 25, 50, 100)

BUCKETS_BY_UNIT = {
    "ms": MS_BUCKETS,
    "tokens": TOKEN_BUCKETS,
    "tokens/sec/user": TOKENS_PER_SEC_BUCKETS,
    "tokens/sec": TOKENS_PER_SEC_BUCKETS,
    "count": COUNT_BUCKETS,
    "kb": KB_BUCKETS,
    "ratio": RATIO_BUCKETS,
    "%": PERCENT_BUCKETS,
}

# Cache of metric_name -> Histogram, built lazily as new metric names are
# encountered in the data, so adding a new aiperf metric later doesn't
# require touching this script.
_histograms = {}


def sanitize(name: str) -> str:
    """Make a string safe to use inside a Prometheus metric name."""
    return (
        name.lower()
        .replace("/", "_per_")
        .replace(" ", "_")
        .replace("-", "_")
    )


def get_histogram(metric_name: str, unit: str) -> Histogram:
    """Return the Histogram for this metric, creating it on first use."""
    key = metric_name
    if key not in _histograms:
        full_name = f"aiperf_{sanitize(metric_name)}_{sanitize(unit)}"
        buckets = BUCKETS_BY_UNIT.get(unit.lower())
        if buckets is None:
            # Unknown unit from a future aiperf version: fall back to a wide
            # log-spaced range rather than the seconds-oriented defaults, and
            # say so, since quantiles for this metric will be coarse.
            buckets = (0.01, 0.1, 1, 10, 100, 1000, 10000, 100000, 1000000)
            print(f"Warning: no bucket layout for unit '{unit}' (metric {metric_name}); using coarse default", file=sys.stderr)
        _histograms[key] = Histogram(
            full_name,
            f"aiperf per-request metric: {metric_name} ({unit})",
            LABEL_FIELDS,
            buckets=buckets,
        )
    return _histograms[key]


def process_line(line: str):
    """Parse one JSONL record and feed its metrics into the right histograms."""
    line = line.strip()
    if not line:
        return
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        print(f"Warning: skipping malformed JSON line: {line[:100]}...", file=sys.stderr)
        return

    metadata = record.get("metadata", {})
    label_values = {f: str(metadata.get(f, "unknown")) for f in LABEL_FIELDS}

    for metric_name, metric_data in record.get("metrics", {}).items():
        value = metric_data.get("value")
        unit = metric_data.get("unit", "unitless")
        hist = get_histogram(metric_name, unit)

        if isinstance(value, list):
            # e.g. inter_chunk_latency: one observation per token-to-token gap
            for v in value:
                if isinstance(v, (int, float)):
                    hist.labels(**label_values).observe(v)
        elif isinstance(value, (int, float)):
            hist.labels(**label_values).observe(value)
        # Non-numeric scalars (booleans, strings) aren't meaningful as a
        # histogram observation and are skipped.


def tail_file(path: str):
    """Yield new lines appended to a file, tail -f style."""
    with open(path, "r") as f:
        f.seek(0, 2)  # jump to end — only process new records from here
        while True:
            line = f.readline()
            if line:
                yield line
            else:
                time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "jsonl_path", nargs="?", default="profile_export.jsonl",
        help="Path to profile_export.jsonl (default: ./profile_export.jsonl)",
    )
    parser.add_argument("--port", type=int, default=EXPORTER_PORT, help=f"Port to serve /metrics on (default: {EXPORTER_PORT})")
    args = parser.parse_args()

    start_http_server(args.port)
    print(f"Serving aiperf JSONL metrics on :{args.port}/metrics")
    print(f"Tailing {args.jsonl_path} ...")

    for line in tail_file(args.jsonl_path):
        process_line(line)


if __name__ == "__main__":
    main()
