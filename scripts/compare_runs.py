#!/usr/bin/env python3
"""Compare two bench.py JSON outputs with time and ratio deltas.

Rows are matched by benchmark name, so the runs should come from the same
variant matrix, two machines, two gzip-ng builds, or two inputs of the same
size. Compression rows also compare output ratio.

Usage:
    python3 scripts/compare_runs.py base.json contender.json

Color thresholds follow Google Benchmark's compare.py, over +5% time is a
regression in red, under -7% is a win in cyan.
"""
import json
import math
import sys

BC_FAIL = "\033[91m"
BC_CYAN = "\033[96m"
BC_GREEN = "\033[92m"
BC_WHITE = "\033[0m"


def load(path):
    with open(path) as f:
        d = json.load(f)
    return {b["name"]: b for b in d["benchmarks"]}, d.get("context", {})


def calculate_change(old_val, new_val):
    if old_val == 0 and new_val == 0:
        return 0.0
    if old_val == 0:
        return (new_val - old_val) / ((old_val + new_val) / 2.0) * 100.0
    return (new_val - old_val) / abs(old_val) * 100.0


def color_time(delta_pct):
    if delta_pct > 5.0:
        return BC_FAIL
    if delta_pct < -7.0:
        return BC_CYAN
    return BC_WHITE


def color_ratio(delta_pct):
    if delta_pct > 0.1:
        return BC_GREEN
    if delta_pct < -0.1:
        return BC_FAIL
    return BC_WHITE


def geomean(values):
    filtered = [v for v in values if v > 0]
    if not filtered:
        return 0.0
    return math.exp(sum(math.log(v) for v in filtered) / len(filtered))


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    base, bctx = load(sys.argv[1])
    cont, cctx = load(sys.argv[2])
    # Block census rows carry counts, not times, so they have no delta here
    common = sorted(n for n in set(base) & set(cont)
                    if "seconds_mean" in base[n] and "seconds_mean" in cont[n])
    if not common:
        print("No benchmarks in common between the two runs.", file=sys.stderr)
        sys.exit(1)
    if bctx.get("input", {}).get("bytes") != cctx.get("input", {}).get("bytes"):
        print("warning: input sizes differ, time deltas still compare, "
              "speeds do not", file=sys.stderr)

    width = max(20, max(len(n) for n in common))
    header = (f"{'Benchmark':<{width}} {'Δ time':>9} {'Δ ratio':>9}  "
              f"{'base time':>10} {'new time':>10}  {'base ratio':>10} {'new ratio':>10}")
    print(header)
    print("-" * len(header))

    base_times = []
    new_times = []
    ratio_deltas = []
    for name in common:
        b, c = base[name], cont[name]
        dtime = calculate_change(b["seconds_mean"], c["seconds_mean"])
        base_times.append(b["seconds_mean"])
        new_times.append(c["seconds_mean"])
        has_ratio = "ratio" in b and "ratio" in c
        dratio = calculate_change(b["ratio"], c["ratio"]) if has_ratio else 0.0
        if has_ratio:
            ratio_deltas.append(dratio)
        ct, cr = color_time(dtime), color_ratio(dratio) if has_ratio else BC_WHITE
        b_r = f"{b['ratio']:>10.4f}" if has_ratio else f"{'-':>10}"
        c_r = f"{c['ratio']:>10.4f}" if has_ratio else f"{'-':>10}"
        print(f"{name:<{width}} {ct}{dtime:>+8.2f}%{BC_WHITE} "
              f"{cr}{dratio:>+8.2f}%{BC_WHITE}  "
              f"{b['seconds_mean']:>9.3f}s {c['seconds_mean']:>9.3f}s  {b_r} {c_r}")

    print("-" * len(header))
    gm_delta = calculate_change(geomean(base_times), geomean(new_times))
    avg_r = sum(ratio_deltas) / len(ratio_deltas) if ratio_deltas else 0.0
    print(f"{'OVERALL_GEOMEAN':<{width}} {color_time(gm_delta)}{gm_delta:>+8.2f}%{BC_WHITE} "
          f"{color_ratio(avg_r)}{avg_r:>+8.2f}%{BC_WHITE}  (ratio as arithmetic mean)")


if __name__ == "__main__":
    main()
