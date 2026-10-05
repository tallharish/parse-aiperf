#!/usr/bin/env python3
"""
combine_aiperf_runs.py

Walks a parent directory containing one subfolder per aiperf concurrency
run (e.g. artifacts/pareto-c1, artifacts/pareto-c5, artifacts/pareto-c10, ...),
parses each run's profile_export_aiperf.csv, and writes a single wide
comparison CSV with metrics as rows and concurrency levels as columns.

profile_export_aiperf.csv contains three stacked tables, separated by
blank lines, which this script tells apart by their header row:

    1. Request/session metrics : "Metric,avg,min,max,sum,p1,...,std"
    2. Summary metrics         : "Metric,Value"
    3. GPU telemetry           : "Endpoint,GPU_Index,GPU_Name,...,Metric,avg,...,Hostname"

Usage:
    python3 combine_aiperf_runs.py /path/to/artifacts [--stat avg] [--output comparison.csv]

    --stat    Which column to pull from the multi-stat tables (request
              metrics and GPU telemetry). Default: avg. Any column present
              in those tables works, e.g. p99, p90, max, std.
    --output  Output CSV path. Default: aiperf_comparison.csv
Separately, it also looks for each run's aiperf_system_metrics_*.csv
(written by parse_aiperf_run.py — Metric,client_system,server_system) and
combines those into their own side-by-side CSV, independent of the output
above.

    --system-output   Output CSV path for system metrics. Default:
                       aiperf_system_comparison.csv
"""

import argparse
import csv
import fnmatch
import os
import re
import sys


def extract_concurrency(folder_name: str):
    """
    Pull a concurrency level out of a folder name. Tries 'c<digits>' first
    (matches 'pareto-c5' -> 5), falling back to the last run of digits
    anywhere in the name if there's no 'c' prefix convention.
    """
    m = re.search(r"c(\d+)(?:$|[^0-9])", folder_name, re.IGNORECASE)
    if m:
        return int(m.group(1))
    digits = re.findall(r"\d+", folder_name)
    if digits:
        return int(digits[-1])
    return None


def read_blocks(path: str):
    """Split a CSV file into blocks of non-empty lines, separated by blank lines."""
    with open(path, "r", newline="") as f:
        content = f.read()
    raw_blocks = re.split(r"\n\s*\n", content.strip("\n"))
    blocks = []
    for rb in raw_blocks:
        lines = [l for l in rb.splitlines() if l.strip() != ""]
        if lines:
            blocks.append(lines)
    return blocks


def classify_block(header_line: str):
    header = next(csv.reader([header_line]))
    if header[:2] == ["Metric", "Value"]:
        return "summary"
    if header[:1] == ["Endpoint"] and "GPU_Name" in header:
        return "gpu"
    if header[:1] == ["Metric"] and "avg" in header:
        return "stats"
    return "unknown"


def parse_run(csv_path: str, stat: str):
    """
    Parse one profile_export_aiperf.csv into three dicts:
      request_metrics : {metric_name: value}
      summary_metrics  : {metric_name: value}
      gpu_metrics      : {(metric_name, hostname): value}
    """
    request_metrics = {}
    summary_metrics = {}
    gpu_metrics = {}

    for block in read_blocks(csv_path):
        kind = classify_block(block[0])
        rows = list(csv.DictReader(block))

        if kind == "stats":
            for row in rows:
                name = row.get("Metric", "").strip()
                if not name:
                    continue
                request_metrics[name] = row.get(stat, "")

        elif kind == "summary":
            for row in rows:
                name = row.get("Metric", "").strip()
                if not name:
                    continue
                summary_metrics[name] = row.get("Value", "")

        elif kind == "gpu":
            for row in rows:
                name = row.get("Metric", "").strip()
                if not name:
                    continue
                hostname = row.get("Hostname", "").strip()
                gpu_metrics[(name, hostname)] = row.get(stat, "")

        # 'unknown' blocks are silently skipped — a future aiperf version
        # adding a new table shape won't crash the script, just won't
        # appear in the output.

    return request_metrics, summary_metrics, gpu_metrics


def find_file(subdir: str, filename: str = None, pattern: str = None):
    """
    Search a directory tree for files matching an exact filename or a
    glob-style pattern. Returns all matches found, deepest-first order
    not guaranteed — caller decides what to do with multiple matches.
    """
    matches = []
    for dirpath, _, filenames in os.walk(subdir):
        for fn in filenames:
            if filename and fn == filename:
                matches.append(os.path.join(dirpath, fn))
            elif pattern and fnmatch.fnmatch(fn, pattern):
                matches.append(os.path.join(dirpath, fn))
    return matches


def parse_system_metrics(csv_path: str):
    """
    Parse one aiperf_system_metrics_*.csv (Metric,client_system,server_system)
    into {(metric_name, "client_system" | "server_system"): value}.
    """
    result = {}
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Metric") or "").strip()
            if not name:
                continue
            for col in ("client_system", "server_system"):
                if col in row:
                    result[(name, col)] = row.get(col, "")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root_dir", help="Parent directory containing one subfolder per concurrency run")
    parser.add_argument("--stat", default="avg", help="Column to pull from multi-stat tables (default: avg)")
    parser.add_argument(
        "--output", default=None,
        help="Output CSV path. Default: 'aiperf_comparison.csv' inside root_dir.",
    )
    parser.add_argument(
        "--csv-name", default="profile_export_aiperf.csv",
        help="Filename to look for inside each run folder (default: profile_export_aiperf.csv)",
    )
    parser.add_argument(
        "--system-output", default=None,
        help="Output CSV path for system metrics. Default: 'aiperf_system_comparison.csv' inside root_dir.",
    )
    parser.add_argument(
        "--system-csv-pattern", default="aiperf_system_metrics_*.csv",
        help="Glob pattern for the system-metrics file inside each run folder "
             "(default: aiperf_system_metrics_*.csv)",
    )
    args = parser.parse_args()

    if args.output is None:
        args.output = os.path.join(args.root_dir, "aiperf_comparison.csv")
    if args.system_output is None:
        args.system_output = os.path.join(args.root_dir, "aiperf_system_comparison.csv")

    # Discover one run per immediate subfolder of root_dir
    runs = {}         # concurrency -> profile_export_aiperf.csv path
    system_runs = {}  # concurrency -> aiperf_system_metrics_*.csv path
    for entry in sorted(os.listdir(args.root_dir)):
        subdir = os.path.join(args.root_dir, entry)
        if not os.path.isdir(subdir):
            continue
        concurrency = extract_concurrency(entry)
        if concurrency is None:
            print(f"Warning: could not extract a concurrency level from folder name '{entry}', skipping", file=sys.stderr)
            continue

        matches = find_file(subdir, filename=args.csv_name)
        if not matches:
            print(f"Warning: no {args.csv_name} found under '{subdir}', skipping", file=sys.stderr)
        else:
            if len(matches) > 1:
                print(f"Warning: multiple {args.csv_name} found under '{subdir}', using {matches[0]}", file=sys.stderr)
            runs[concurrency] = matches[0]

        sys_matches = sorted(find_file(subdir, pattern=args.system_csv_pattern))
        if not sys_matches:
            print(f"Note: no file matching '{args.system_csv_pattern}' found under '{subdir}'", file=sys.stderr)
        else:
            if len(sys_matches) > 1:
                # Filenames are timestamped, so the lexicographically last
                # one is also the most recent run.
                print(
                    f"Warning: multiple system-metrics files under '{subdir}', "
                    f"using the most recent: {sys_matches[-1]}",
                    file=sys.stderr,
                )
            system_runs[concurrency] = sys_matches[-1]

    if not runs and not system_runs:
        print(f"No runs found under {args.root_dir}", file=sys.stderr)
        sys.exit(1)

    # --- profile_export_aiperf.csv -> args.output -------------------------

    if runs:
        concurrencies = sorted(runs.keys())
        print(f"Found {len(concurrencies)} run(s) with {args.csv_name}: concurrency levels {concurrencies}")

        all_request = {}  # metric -> {concurrency: value}
        all_summary = {}  # metric -> {concurrency: value}
        all_gpu = {}       # (metric, hostname) -> {concurrency: value}
        hostnames_seen = set()

        for c in concurrencies:
            req, summ, gpu = parse_run(runs[c], args.stat)
            for name, val in req.items():
                all_request.setdefault(name, {})[c] = val
            for name, val in summ.items():
                all_summary.setdefault(name, {})[c] = val
            for (name, host), val in gpu.items():
                hostnames_seen.add(host)
                all_gpu.setdefault((name, host), {})[c] = val

        multi_host = len(hostnames_seen) > 1

        fieldnames = ["Section", "Metric"] + [f"c{c}" for c in concurrencies]
        out_rows = []

        for name in sorted(all_request.keys()):
            row = {"Section": "Request Metrics", "Metric": name}
            for c in concurrencies:
                row[f"c{c}"] = all_request[name].get(c, "")
            out_rows.append(row)

        for name in sorted(all_summary.keys()):
            row = {"Section": "Summary Metrics", "Metric": name}
            for c in concurrencies:
                row[f"c{c}"] = all_summary[name].get(c, "")
            out_rows.append(row)

        for (name, host), values in sorted(all_gpu.items()):
            label = f"{name} [{host}]" if multi_host else name
            row = {"Section": "GPU Metrics", "Metric": label}
            for c in concurrencies:
                row[f"c{c}"] = values.get(c, "")
            out_rows.append(row)

        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(out_rows)

        print(f"Stat used for multi-stat tables: {args.stat}")
        print(f"Wrote {len(out_rows)} metric rows across {len(concurrencies)} concurrency levels to {args.output}")
    else:
        print(f"No {args.csv_name} files found — skipping {args.output}", file=sys.stderr)

    # --- aiperf_system_metrics_*.csv -> args.system_output ----------------

    if system_runs:
        sys_concurrencies = sorted(system_runs.keys())
        print(f"\nFound {len(sys_concurrencies)} run(s) with system metrics: concurrency levels {sys_concurrencies}")

        all_system = {}  # (metric, system) -> {concurrency: value}
        for c in sys_concurrencies:
            for (name, system), val in parse_system_metrics(system_runs[c]).items():
                all_system.setdefault((name, system), {})[c] = val

        sys_fieldnames = ["Metric", "System"] + [f"c{c}" for c in sys_concurrencies]
        sys_rows = []
        for (name, system), values in sorted(all_system.items()):
            row = {"Metric": name, "System": system}
            for c in sys_concurrencies:
                row[f"c{c}"] = values.get(c, "")
            sys_rows.append(row)

        with open(args.system_output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=sys_fieldnames)
            writer.writeheader()
            writer.writerows(sys_rows)

        print(f"Wrote {len(sys_rows)} system-metric rows across {len(sys_concurrencies)} concurrency levels to {args.system_output}")
    else:
        print(f"No files matching '{args.system_csv_pattern}' found — skipping {args.system_output}", file=sys.stderr)


if __name__ == "__main__":
    main()
