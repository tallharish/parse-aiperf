#!/usr/bin/env python3
"""
parse_aiperf_run.py

Parses an aiperf.log file for the profiling run's start/end timestamps,
computes the run duration, queries Prometheus for a set of system metrics
(CPU, memory, disk, network) over that window, and prints a
metric / client_system / server_system comparison table.

Usage:
    python3 parse_aiperf_run.py [path/to/aiperf.log]

If no path is given, it defaults to "aiperf.log" in the current directory.
"""

import re
import os
import sys
import csv
import math
import json
import argparse
import subprocess
from datetime import datetime, timezone

# --- Configuration -----------------------------------------------------

PROM_URL = "http://llm-obs:9090/api/v1/query"

# aiperf.log timestamps have no timezone offset. Set this to the timezone
# those timestamps are actually written in. If your VM/container logs in
# UTC (common for infra), leave this as timezone.utc. If it logs in local
# time, change this (e.g. to a specific offset) or the query time sent to
# Prometheus will be wrong even though the duration calculation stays correct.
LOG_TIMEZONE = timezone.utc

# --- Log line patterns ---------------------------------------------------

TIMESTAMP_RE = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)"

START_PATTERN = re.compile(
    rf"^{TIMESTAMP_RE} - system_controller - INFO - AIPerf System is PROFILING"
)
END_PATTERN = re.compile(
    rf"^{TIMESTAMP_RE} - system_controller - INFO - Received credits complete from\s+'timing_manager'"
)

# --- Metric definitions ---------------------------------------------------
#
# Each query template may use "{range}" as a placeholder for the Prometheus
# duration string (e.g. "10m") computed from the run window. Templates that
# don't need a range window (plain gauges like memory/disk %) simply don't
# reference "{range}" — .format() ignores the unused keyword argument.
#
# Literal PromQL label-matcher braces are escaped as "{{" / "}}" so they
# survive str.format() without being treated as substitution fields.

METRICS = [
    {
        "name": "CPU Usage",
        "unit": "%",
        "query": '100 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[{range}])*100)',
    },
    {
        "name": "Memory Usage",
        "unit": "%",
        "query": (
            "100 - (avg by (instance) (node_memory_MemAvailable_bytes) "
            "/ avg by (instance) (node_memory_MemTotal_bytes) * 100)"
        ),
    },
    {
        "name": "Disk Read BW",
        "unit": "MB/s",
        "query": "avg by (instance) (rate(node_disk_read_bytes_total[{range}])) / 1048576",
    },
    {
        "name": "Disk Write BW",
        "unit": "MB/s",
        "query": "avg by (instance) (rate(node_disk_written_bytes_total[{range}])) / 1048576",
    },
    {
        "name": "Disk Reads",
        "unit": "ops/s",
        "query": 'avg by (instance) (rate(node_disk_reads_completed_total{{device="sda"}}[{range}]))',
    },
    {
        "name": "Disk Writes",
        "unit": "ops/s",
        "query": 'avg by (instance) (rate(node_disk_writes_completed_total{{device="sda"}}[{range}]))',
    },
    {
        "name": "Network Receive",
        "unit": "MB/s",
        "query": 'avg by (instance) (rate(node_network_receive_bytes_total{{device=~"ens.*"}}[{range}])) / 1048576',
    },
    {
        "name": "Network Transmit",
        "unit": "MB/s",
        "query": 'avg by (instance) (rate(node_network_transmit_bytes_total{{device=~"ens.*"}}[{range}])) / 1048576',
    },
    {
        "name": "Data points",
        "unit": "Count",
        "query": 'count_over_time(node_cpu_seconds_total{{mode="idle"}}[{range}])',
    }
]

''' Future Metrics
    {
        "name": "Disk Usage (root fs)",
        "unit": "%",
        "query": (
            '100 - (avg by (instance) (node_filesystem_avail_bytes{{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}}) '
            '/ avg by (instance) (node_filesystem_size_bytes{{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}}) * 100)'
        ),
    },
'''

def parse_timestamp(ts_str: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM:SS.mmm' into an aware datetime in LOG_TIMEZONE."""
    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
    return dt.replace(tzinfo=LOG_TIMEZONE)


def find_run_window(log_path: str):
    """
    Scan the log for the first PROFILING start marker and the first
    matching 'credits complete' end marker after it.

    If your log can contain multiple runs and you want the LAST run
    instead of the first, remove the 'break' below and keep overwriting
    start_dt/end_dt on every match.
    """
    start_dt = None
    end_dt = None

    with open(log_path, "r", errors="replace") as f:
        for line in f:
            if start_dt is None:
                m = START_PATTERN.match(line)
                if m:
                    start_dt = parse_timestamp(m.group("ts"))
                continue

            if end_dt is None:
                m = END_PATTERN.match(line)
                if m:
                    end_dt = parse_timestamp(m.group("ts"))
                    break

    if start_dt is None or end_dt is None:
        raise ValueError(
            f"Could not find both markers in {log_path} "
            f"(start found: {start_dt is not None}, end found: {end_dt is not None})"
        )
    return start_dt, end_dt


def duration_to_promql_range(start_dt: datetime, end_dt: datetime):
    """
    Round the run duration UP to the nearest whole minute and format it
    as a Prometheus range-vector duration string, e.g. '10m'.

    Rounding up (not down) matters here: a range vector shorter than the
    actual run window would clip data from the start of the run.
    """
    total_seconds = (end_dt - start_dt).total_seconds()
    minutes = math.floor(total_seconds / 60)
    return f"{minutes}m", total_seconds


def format_prom_time(dt: datetime) -> str:
    """Prometheus expects RFC3339 UTC, e.g. 2026-09-26T00:10:00Z"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_prometheus(query: str, time_str: str) -> dict:
    """
    Equivalent of:
        curl -sG --data-urlencode 'query=...' --data-urlencode 'time=...' \\
             http://llm-obs:9090/api/v1/query | jq

    Uses curl via subprocess (so it behaves identically to your existing
    workflow / firewall rules) and parses the JSON response itself.
    """
    cmd = [
        "curl", "-sG",
        "--data-urlencode", f"query={query}",
        "--data-urlencode", f"time={time_str}",
        PROM_URL,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def extract_values_by_instance(response: dict) -> dict:
    """
    Turn a Prometheus vector response into {instance: float_value}.

        {"metric": {"instance": "llm-client:9100"}, "value": [ts, "15.91"]}
        -> {"llm-client:9100": 15.91}
    """
    values = {}
    for item in response.get("data", {}).get("result", []):
        instance = item.get("metric", {}).get("instance", "unknown")
        raw_val = item.get("value", [None, None])[1]
        try:
            values[instance] = float(raw_val)
        except (TypeError, ValueError):
            values[instance] = None
    return values


def classify_instance(instance: str) -> str:
    """
    Map a Prometheus 'instance' label to one of the two output columns.
    Matches on substring, case-insensitively, so 'llm-client:9100' and
    'llm-server:9100' both resolve correctly. Adjust the keywords here
    if your instance naming convention differs.
    """
    low = instance.lower()
    if "client" in low:
        return "client_system"
    if "server" in low:
        return "server_system"
    return "unknown"


def format_table(headers, rows):
    """Simple fixed-width text table, no external dependencies required."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def fmt_row(row):
        return " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))

    sep = "-+-".join("-" * w for w in widths)
    lines = [fmt_row(headers), sep]
    lines += [fmt_row(row) for row in rows]
    return "\n".join(lines)


def write_csv(path, headers, rows):
    """Write the metric/client_system/server_system table out as CSV."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def process_run(log_path, output_csv=None):
    """
    Run the full pipeline for one aiperf.log: find the run window, query
    Prometheus for every metric, print the comparison table, and write it
    to CSV. Returns the CSV path written.
    """
    start_dt, end_dt = find_run_window(log_path)
    range_str, raw_seconds = duration_to_promql_range(start_dt, end_dt)
    end_time_str = format_prom_time(end_dt)

    print(f"Log file         : {log_path}")
    print(f"Run start        : {start_dt.isoformat()}")
    print(f"Run end          : {end_dt.isoformat()}")
    print(f"Duration         : {raw_seconds:.1f}s -> rounded to {range_str} for the query range")
    print(f"Query time param : {end_time_str}\n")

    rows = []
    for metric in METRICS:
        query = metric["query"].format(range=range_str)
        response = query_prometheus(query, end_time_str)
        values = extract_values_by_instance(response)

        client_val = server_val = None
        for instance, val in values.items():
            col = classify_instance(instance)
            if col == "client_system":
                client_val = val
            elif col == "server_system":
                server_val = val
            else:
                print(
                    f"Warning: could not classify instance '{instance}' "
                    f"as client or server (metric: {metric['name']})",
                    file=sys.stderr,
                )

        client_str = f"{client_val:.2f}" if client_val is not None else "N/A"
        server_str = f"{server_val:.2f}" if server_val is not None else "N/A"
        rows.append([f"{metric['name']} ({metric['unit']})", client_str, server_str])

    headers = ["Metric", "client_system", "server_system"]
    print(format_table(headers, rows))

    if output_csv:
        csv_path = output_csv
    else:
        log_dir = os.path.dirname(os.path.abspath(log_path))
        csv_name = f"aiperf_system_metrics_{end_dt.strftime('%Y%m%d_%H%M%S')}.csv"
        csv_path = os.path.join(log_dir, csv_name)

    write_csv(csv_path, headers, rows)
    print(f"\nWrote CSV results to {csv_path}")
    return csv_path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "path",
        help=(
            "Either a single aiperf.log file, or a parent directory "
            "containing one subfolder per run (each expected to have "
            "<subfolder>/logs/aiperf.log, matching --log-rel-path)."
        ),
    )
    parser.add_argument(
        "--output-csv", default=None,
        help=(
            "Single-file mode only: path for the CSV output. Default: "
            "auto-named 'aiperf_system_metrics_<run-end-timestamp>.csv' "
            "in the same directory as the log file."
        ),
    )
    parser.add_argument(
        "--log-rel-path", default="logs/aiperf.log",
        help="Directory mode: relative path to aiperf.log within each run subfolder (default: logs/aiperf.log)",
    )
    args = parser.parse_args()

    if os.path.isfile(args.path):
        # Single-run mode — same behavior as before this change.
        process_run(args.path, args.output_csv)
        return

    if not os.path.isdir(args.path):
        print(f"Error: '{args.path}' is neither a file nor a directory", file=sys.stderr)
        sys.exit(1)

    # Directory mode — one run per immediate subfolder, mirroring
    # combine_aiperf_runs.py's folder-scanning pattern.
    subfolders = sorted(
        entry for entry in os.listdir(args.path)
        if os.path.isdir(os.path.join(args.path, entry))
    )

    if not subfolders:
        print(f"No subfolders found under {args.path}", file=sys.stderr)
        sys.exit(1)

    processed, skipped = 0, 0
    for entry in subfolders:
        log_path = os.path.join(args.path, entry, args.log_rel_path)
        print(f"\n{'=' * 60}\n{entry}\n{'=' * 60}")
        if not os.path.isfile(log_path):
            print(f"Warning: no log found at '{log_path}', skipping", file=sys.stderr)
            skipped += 1
            continue
        try:
            process_run(log_path, output_csv=None)  # auto-named, written alongside this run's aiperf.log
            processed += 1
        except Exception as e:
            print(f"Warning: failed to process '{log_path}': {e}", file=sys.stderr)
            skipped += 1

    print(f"\nDone. Processed {processed} run(s), skipped {skipped}.")


if __name__ == "__main__":
    main()
