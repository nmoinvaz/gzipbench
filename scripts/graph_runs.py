#!/usr/bin/env python3
"""Graph a bench.py JSON output as a tool comparison SVG.

Plots the compression benchmarks as a speed versus ratio chart, one point
per level with the ladder connected in order, serial and parallel variants
as separate series. Decompression rows, including the cross-decode pairs,
get a throughput bar panel, the thread sweeps get scaling line panels, and
the deflate block census gets paired bar panels, block counts and average
block sizes across the normal, rsyncable, and independent modes.

Usage:
    python3 scripts/graph_runs.py run.json [-o out.svg] [--title text]

An aggregate table is also printed to stdout. Two runs are compared with
scripts/compare_runs.py instead. Needs only the Python standard library.
"""
import argparse
import json
import math
import sys

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SOFT = "#52514e"
GRID = "#e7e6e2"
# Categorical slots in fixed series order, never cycled
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                 "#e87ba4", "#008300", "#4a3aa7", "#b0368f", "#7a7668"]

SERIES_ORDER = ["gzip-ng -p", "pigz -p", "bgzip -@", "migz",
                "gzip-ng", "minigzip", "gzip", "pigzpp -p"]

REPO_URL = "https://github.com/nmoinvaz/gzipbench"


MODE_ORDER = ["normal", "rsyncable", "independent"]
MODE_OPACITY = {"normal": 1.0, "rsyncable": 0.66, "independent": 0.4}


def fmt_speed(bps):
    if bps >= 1e9:
        return f"{bps / 1e9:.2f} GB/s"
    return f"{bps / 1e6:.0f} MB/s"


def fmt_bytes(v):
    if v >= 1e6:
        return f"{v / 1e6:.1f} MB"
    if v >= 1e3:
        return f"{v / 1e3:.1f} kB"
    return f"{v:.0f} B"


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Svg:
    """Collects body elements; the height is decided at finish time."""

    def __init__(self, width):
        self.w = width
        self.parts = []

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=12, fill=INK_SOFT, anchor="start", weight="normal"):
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
                 f'text-anchor="{anchor}" font-weight="{weight}">{esc(s)}</text>')

    def line(self, x1, y1, x2, y2, stroke, width=1):
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                 f'stroke="{stroke}" stroke-width="{width}"/>')

    def dot(self, x, y, color, title, r=5.5):
        self.add(f'<g><circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}" '
                 f'stroke="{SURFACE}" stroke-width="2"/><title>{esc(title)}</title></g>')

    def finish(self, height):
        header = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
            f'height="{height}" viewBox="0 0 {self.w} {height}" '
            f'font-family="system-ui, sans-serif">',
            f'<rect width="{self.w}" height="{height}" fill="{SURFACE}"/>']
        return "\n".join(header + self.parts + ["</svg>"]) + "\n"


def better_arrow(svg, x1, y1, x2, y2):
    """Semi-transparent direction-of-better hint with a label along the shaft."""
    ln = math.hypot(x2 - x1, y2 - y1)
    ux, uy = (x2 - x1) / ln, (y2 - y1) / ln
    head = (f"{x2:.1f},{y2:.1f} "
            f"{x2 - 9 * ux - 4 * uy:.1f},{y2 - 9 * uy + 4 * ux:.1f} "
            f"{x2 - 9 * ux + 4 * uy:.1f},{y2 - 9 * uy - 4 * ux:.1f}")
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
    if ang > 90 or ang < -90:
        ang += 180  # keep the label reading left to right
    svg.add(f'<g opacity="0.35"><line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2 - 6 * ux:.1f}" '
            f'y2="{y2 - 6 * uy:.1f}" stroke="{INK_SOFT}" stroke-width="2"/>'
            f'<polygon points="{head}" fill="{INK_SOFT}"/>'
            f'<text x="{mx:.1f}" y="{my - 7:.1f}" font-size="11" fill="{INK_SOFT}" '
            f'text-anchor="middle" transform="rotate({ang:.1f} {mx:.1f} {my:.1f})">'
            f'better</text></g>')


def nice_log_ticks(lo, hi):
    ticks = []
    e = math.floor(math.log10(lo))
    while 10 ** e <= hi * 1.001:
        for mult in (1, 2, 5):
            v = mult * 10 ** e
            if lo * 0.999 <= v <= hi * 1.001:
                ticks.append(v)
        e += 1
    return ticks


def series_color(series):
    i = SERIES_ORDER.index(series) if series in SERIES_ORDER else len(SERIES_ORDER)
    return SERIES_COLORS[i]


def variant_label(series, threads):
    return f"{series} {threads}" if threads is not None else series


def cv_of(b):
    return b["seconds_stddev"] / b["seconds_mean"] if b["seconds_mean"] else 0.0


def machine_line(ctx):
    parts = []
    if ctx.get("cpu_brand"):
        parts.append(ctx["cpu_brand"])
    if ctx.get("num_cpus"):
        parts.append(f"{ctx['num_cpus']} cpus")
    inp = ctx.get("input", {})
    if inp:
        parts.append(f"{inp.get('description', 'input')}"
                     f" ({inp.get('bytes', 0) / 1048576:.0f} MiB)")
    if ctx.get("host_name"):
        parts.append(ctx["host_name"])
    if ctx.get("date"):
        parts.append(ctx["date"].split("T")[0])
    return ", ".join(parts)


def run_warnings(ctx, benchmarks):
    warns = []
    if ctx.get("missing"):
        warns.append("skipped: " + ", ".join(ctx["missing"]))
    load1 = (ctx.get("load_avg") or [0])[0]
    ncpus = ctx.get("num_cpus") or 1
    if load1 > ncpus / 2:
        warns.append(f"load {load1:.1f} during run")
    n = sum(1 for b in benchmarks if "seconds_mean" in b and cv_of(b) > 0.03)
    if n:
        warns.append(f"{n} benchmarks with cv above 3%")
    return warns


def render(ctx, benchmarks, title, out_path):
    tmax = max(ctx.get("threads", [1]))
    compress = [b for b in benchmarks if b["kind"] == "compress"]
    decompress = [b for b in benchmarks if b["kind"] == "decompress"]

    # The scatter takes the ladder points, serial or at the full thread count;
    # sweep intermediates feed the scaling panels only, where serial tools
    # appear too, their single value at the first thread count
    ladder = [b for b in compress if b["threads"] in (None, tmax)]
    sweep_c = [b for b in compress if b["level"] == 6]
    own = [b for b in decompress if b["variant"] == b["producer_variant"]]
    sweep_d = own
    rows = [b for b in own if b["threads"] in (None, tmax)]

    series_seen = []
    for b in benchmarks:
        if b["series"] not in series_seen:
            series_seen.append(b["series"])
    series_seen.sort(key=lambda s: SERIES_ORDER.index(s)
                     if s in SERIES_ORDER else len(SERIES_ORDER))

    width = 1080
    svg = Svg(width)
    svg.text(16, 28, title, size=15, fill=INK, weight="bold")

    # Legend, color carries the variant identity
    lx = width - 16
    for s in reversed(series_seen):
        svg.text(lx, 28, s, size=12, fill=INK, anchor="end")
        lx -= 7.2 * len(s) + 12
        svg.add(f'<circle cx="{lx:.1f}" cy="24" r="5" fill="{series_color(s)}"/>')
        lx -= 20

    # Decompression panel, own-output throughput bars; the cross-decode
    # pairs get their own panels below the census
    bx, by, bw = 812, 76, 220
    svg.text(bx, by - 22, "decompress, own output", size=12, fill=INK)
    rows.sort(key=lambda b: -b["bytes_per_second"])
    bar_max = max((b["bytes_per_second"] for b in rows), default=1)
    y = by
    for b in rows:
        label = variant_label(b["series"], b["threads"])
        speed = b["bytes_per_second"]
        svg.text(bx, y, label, size=10, fill=INK)
        svg.text(bx + bw, y, fmt_speed(speed), size=10, fill=INK, anchor="end")
        w = max(bw * speed / bar_max, 6)
        tip = (f"{label} - {fmt_speed(speed)}, {b['seconds_mean']:.2f} s"
               + (f", cv {cv_of(b) * 100:.1f}%" if cv_of(b) > 0 else ""))
        svg.add(f'<path d="M{bx} {y + 5} h{w - 4:.1f} a4 4 0 0 1 4 4 v4 '
                f'a4 4 0 0 1 -4 4 h{-(w - 4):.1f} z" fill="{series_color(b["series"])}">'
                f'<title>{esc(tip)}</title></path>')
        y += 34
    better_arrow(svg, bx, y + 2, bx + 64, y + 2)
    right_bottom = y + 12

    # Compression panel, speed versus ratio, level ladders connected in order
    px, py, pw = 78, 76, 640
    ph = max(360, right_bottom - py - 40)
    if not ladder:
        print("No compression benchmarks to plot.", file=sys.stderr)
        sys.exit(1)
    speeds = [b["bytes_per_second"] for b in ladder]
    ratios = [b["ratio"] for b in ladder]
    smin, smax = min(speeds) / 1.6, max(speeds) * 1.6
    rmin, rmax = min(ratios) * 0.96, max(ratios) * 1.04

    def sx(ratio):
        return px + (ratio - rmin) / (rmax - rmin) * pw

    def sy(speed):
        return py + ph - (math.log10(speed) - math.log10(smin)) / \
            (math.log10(smax) - math.log10(smin)) * ph

    for v in nice_log_ticks(smin, smax):
        yy = sy(v)
        svg.line(px, yy, px + pw, yy, GRID)
        svg.text(px - 8, yy + 4, fmt_speed(v), size=11, anchor="end")
    rstep = max(round((rmax - rmin) / 6, 1), 0.1)
    r = math.ceil(rmin / rstep) * rstep
    while r <= rmax:
        x = sx(r)
        svg.line(x, py, x, py + ph, GRID)
        svg.text(x, py + ph + 18, f"{r:g}", size=11, anchor="middle")
        r = round(r + rstep, 6)
    svg.line(px, py + ph, px + pw, py + ph, INK_SOFT)
    svg.text(px + pw / 2, py + ph + 40, "compression ratio", size=12, anchor="middle")
    svg.text(px, py - 22, "compress", size=12, fill=INK)
    better_arrow(svg, px + pw - 128, py + 82, px + pw - 40, py + 24)

    labeled = []

    def label_point(x, y, tag):
        """Skip labels crowding an already labeled point, tooltips still work."""
        for ox, oy, _ in labeled:
            if abs(x - ox) < 24 and abs(y - oy) < 14:
                return
        labeled.append((x, y, tag))

    by_series = {}
    for b in ladder:
        by_series.setdefault(b["series"], []).append(b)
    for s in series_seen:
        pts = sorted(by_series.get(s, []), key=lambda b: b["level"])
        if len(pts) > 1:
            path = " ".join(
                f"{'M' if j == 0 else 'L'}{sx(b['ratio']):.1f},{sy(b['bytes_per_second']):.1f}"
                for j, b in enumerate(pts))
            svg.add(f'<path d="{path}" fill="none" stroke="{series_color(s)}" '
                    f'stroke-width="2" stroke-opacity="0.65"/>')
        for b in pts:
            x, yy = sx(b["ratio"]), sy(b["bytes_per_second"])
            cv = cv_of(b)
            if cv > 0:
                y1 = sy(b["bytes_per_second"] * (1 - cv))
                y2 = sy(b["bytes_per_second"] * (1 + cv))
                svg.add(f'<g stroke="{series_color(s)}" stroke-opacity="0.7" stroke-width="1.5">'
                        f'<line x1="{x:.1f}" y1="{y1:.1f}" x2="{x:.1f}" y2="{y2:.1f}"/>'
                        f'<line x1="{x - 3:.1f}" y1="{y1:.1f}" x2="{x + 3:.1f}" y2="{y1:.1f}"/>'
                        f'<line x1="{x - 3:.1f}" y1="{y2:.1f}" x2="{x + 3:.1f}" y2="{y2:.1f}"/></g>')
            tip = (f"{variant_label(s, b['threads'])} level:{b['level']} - "
                   f"{fmt_speed(b['bytes_per_second'])}, ratio {b['ratio']:.3f}"
                   + (f", cv {cv * 100:.1f}%" if cv > 0 else ""))
            svg.dot(x, yy, series_color(s), tip)
            label_point(x, yy, f"L{b['level']}")
    # Labels last so markers never cover them
    for x, yy, tag in labeled:
        svg.text(x + 7, yy - 7, tag, size=10)

    # Thread scaling panels, compression at level 6 and each parallel decoder
    # on its own output, across the swept thread counts
    facets = [("compress, level 6", sweep_c), ("decompress, own output", sweep_d)]
    facets = [(cap, pts) for cap, pts in facets
              if len({b["threads"] for b in pts} - {None}) > 1]
    ftop = max(py + ph + 76, right_bottom + 36)
    fh, fw, gapx = 190, 450, 44
    for fi, (caption, pts) in enumerate(facets):
        fx = 78 + fi * (fw + gapx)
        svg.text(fx, ftop - 18, f"{caption}, by threads", size=12, fill=INK)
        tvals = sorted({b["threads"] for b in pts} - {None})

        def tx(t, fx=fx, tvals=tvals):
            return fx + tvals.index(t) / max(len(tvals) - 1, 1) * fw

        fspeeds = [b["bytes_per_second"] for b in pts]
        lo, hi = min(fspeeds) / 1.3, max(fspeeds) * 1.3

        def fy(s, lo=lo, hi=hi):
            return ftop + fh - (math.log10(s) - math.log10(lo)) / \
                (math.log10(hi) - math.log10(lo)) * fh

        fseries = {}
        for b in pts:
            fseries.setdefault(b["series"], []).append(b)
        lines = {s: sorted((b for b in rows if b["threads"] is not None),
                           key=lambda b: b["threads"])
                 for s, rows in fseries.items()}
        end_ys = [fy(line[-1]["bytes_per_second"])
                  for line in lines.values() if len(line) > 1]

        # Later facets label their scale inside the right edge, the outside
        # left would collide with the previous facet's value labels, and the
        # inside labels yield to the line endpoint values drawn there
        for v in nice_log_ticks(lo, hi):
            yy = fy(v)
            svg.line(fx, yy, fx + fw, yy, GRID)
            if fi == 0:
                svg.text(fx - 8, yy + 4, fmt_speed(v), size=9, anchor="end")
            elif all(abs(yy - ey) > 16 for ey in end_ys):
                svg.text(fx + fw - 4, yy - 3, fmt_speed(v), size=8, anchor="end")
        svg.line(fx, ftop + fh, fx + fw, ftop + fh, INK_SOFT)
        for t in tvals:
            svg.text(tx(t), ftop + fh + 14, str(t), size=10, anchor="middle")
        svg.text(fx + fw / 2, ftop + fh + 30, "threads", size=11, anchor="middle")

        # One pool for endpoint and serial value labels, crowding labels
        # yield in series order, the tooltips still tell the dots apart
        label_ys = []

        def place_label(x, yy, value, anchor="start"):
            if all(abs(yy - oy) > 10 for oy in label_ys):
                svg.text(x, yy, fmt_speed(value), size=9, anchor=anchor)
                label_ys.append(yy)

        for s in series_seen:
            line = lines.get(s, [])
            if len(line) >= 2:
                coords = [(tx(b["threads"]), fy(b["bytes_per_second"])) for b in line]
                path = " ".join(f"{'M' if q == 0 else 'L'}{x:.1f},{yy:.1f}"
                                for q, (x, yy) in enumerate(coords))
                svg.add(f'<path d="{path}" fill="none" stroke="{series_color(s)}" '
                        f'stroke-width="2" stroke-opacity="0.7"/>')
                for b, (x, yy) in zip(line, coords):
                    tip = (f"{s} threads:{b['threads']} - {fmt_speed(b['bytes_per_second'])}"
                           + (f", cv {cv_of(b) * 100:.1f}%" if cv_of(b) > 0 else ""))
                    svg.dot(x, yy, series_color(s), tip, r=4)
                lb = line[-1]
                place_label(tx(lb["threads"]) - 8, fy(lb["bytes_per_second"]) - 8,
                            lb["bytes_per_second"], anchor="end")
            # Serial tools sit at the first thread count with a single marker
            for b in fseries.get(s, []):
                if b["threads"] is not None:
                    continue
                x, yy = tx(tvals[0]), fy(b["bytes_per_second"])
                tip = (f"{s} - {fmt_speed(b['bytes_per_second'])}"
                       + (f", cv {cv_of(b) * 100:.1f}%" if cv_of(b) > 0 else ""))
                svg.dot(x, yy, series_color(s), tip, r=4)
                place_label(x + 8, yy + 3, b["bytes_per_second"])
        if fi == len(facets) - 1:
            better_arrow(svg, fx + fw + 22, ftop + 92, fx + fw + 22, ftop + 30)
    body_bottom = ftop + fh + 44 if facets else max(py + ph + 56, right_bottom)

    # Deflate block census, paired bar panels: how many blocks each mode
    # cuts the stream into, and how big the average block is, per variant,
    # bar shade carries the mode within a variant's color
    census = [b for b in benchmarks if b["kind"] == "blocks"]
    if census:
        census.sort(key=lambda b: (
            SERIES_ORDER.index(b["series"]) if b["series"] in SERIES_ORDER
            else len(SERIES_ORDER), MODE_ORDER.index(b["mode"])))
        sample_mib = census[0]["input_bytes"] / 1048576
        lx0, cw = 16, 340
        cx, sx2 = 200, 580
        gtop = body_bottom + 60
        svg.text(lx0, gtop - 34, f"deflate blocks, level 6, "
                 f"{sample_mib:g} MiB sample", size=12, fill=INK)
        ux = width - 16
        cw2 = ux - 96 - sx2
        svg.text(cx - 12, gtop - 16, "members", size=11, anchor="end")
        svg.text(cx, gtop - 16, "blocks in stream", size=11)
        svg.text(sx2, gtop - 16, "average block size", size=11)
        svg.text(sx2 + cw2, gtop - 16, "compressed", size=11, anchor="end")
        svg.text(ux, gtop - 16, "uncompressed", size=11, anchor="end")
        cmax = max(b["blocks"] for b in census)
        smax = max(b["block_output_bytes"] for b in census)
        y = gtop + 8
        for b in census:
            color = series_color(b["series"])
            op = MODE_OPACITY.get(b["mode"], 1.0)
            row_label = (b["series"] if b["mode"] == "normal"
                         else f"{b['series']} · {b['mode']}")
            svg.text(lx0, y + 12, row_label, size=10, fill=INK)
            svg.text(cx - 12, y + 12, f"{b['members']:,}", size=10, anchor="end")
            for px0, pw, value, vmax, text, tip in (
                    (cx, cw, b["blocks"], cmax, f"{b['blocks']:,}",
                     f"{b['blocks']:,} blocks ({b['stored']} stored, "
                     f"{b['fixed']} fixed, {b['dynamic']} dynamic), "
                     f"{b['members']:,} members"),
                    (sx2, cw2, b["block_output_bytes"], smax,
                     fmt_bytes(b["block_output_bytes"]),
                     f"avg {fmt_bytes(b['block_output_bytes'])} compressed, "
                     f"{fmt_bytes(b['block_input_bytes'])} of input per block")):
                w = max((pw - 64) * value / vmax, 6)
                svg.add(f'<path d="M{px0} {y + 5} h{w - 4:.1f} a4 4 0 0 1 4 4 '
                        f'v4 a4 4 0 0 1 -4 4 h{-(w - 4):.1f} z" fill="{color}" '
                        f'fill-opacity="{op}">'
                        f'<title>{esc(row_label + " - " + tip)}</title></path>')
                svg.text(px0 + pw, y + 12, text, size=10, fill=INK, anchor="end")
            svg.text(ux, y + 12, fmt_bytes(b["block_input_bytes"]),
                     size=10, fill=INK, anchor="end")
            y += 22
        body_bottom = y + 6

    # Cross-decode panels, every decoder against the block-framed streams
    cross = []
    for producer, title in (("migz", "migz output"), ("bgzip-p", "bgzip -@ output")):
        crows = [b for b in decompress if b["producer_variant"] == producer
                 and b["threads"] in (None, tmax)]
        if crows:
            cross.append((title, crows))
    if cross:
        ctop = body_bottom + 46
        cbw = 380
        cy_max = ctop
        for pi, (title, crows) in enumerate(cross):
            px = 78 + pi * (cbw + 114)
            svg.text(px, ctop - 18, f"decompress {title}", size=12, fill=INK)
            crows.sort(key=lambda b: -b["bytes_per_second"])
            pmax = max(b["bytes_per_second"] for b in crows)
            y = ctop
            for b in crows:
                label = variant_label(b["series"], b["threads"])
                speed = b["bytes_per_second"]
                svg.text(px, y, label, size=10, fill=INK)
                svg.text(px + cbw, y, fmt_speed(speed), size=10, fill=INK,
                         anchor="end")
                w = max(cbw * speed / pmax, 6)
                tip = (f"{label} - {fmt_speed(speed)}, {b['seconds_mean']:.2f} s"
                       + (f", cv {cv_of(b) * 100:.1f}%" if cv_of(b) > 0 else ""))
                svg.add(f'<path d="M{px} {y + 5} h{w - 4:.1f} a4 4 0 0 1 4 4 '
                        f'v4 a4 4 0 0 1 -4 4 h{-(w - 4):.1f} z" '
                        f'fill="{series_color(b["series"])}">'
                        f'<title>{esc(tip)}</title></path>')
                y += 34
            cy_max = max(cy_max, y)
        body_bottom = cy_max + 4

    # Version and machine footnote, wrapped when the tools make it long
    versions = " · ".join(v for v in ctx.get("tools", {}).values() if v)
    zn = ctx.get("zlibng")
    if zn:
        seg = ("zlib-ng " + zn.get("version", "")).strip()
        if zn.get("commit"):
            seg += f" @ {zn['commit'][:9]}"
        versions = seg + (" · " + versions if versions else "")
    machine = machine_line(ctx)
    warnings = run_warnings(ctx, benchmarks)
    height = body_bottom + 42 + (16 if warnings else 0)
    svg.text(16, height - 26, versions, size=10)
    svg.text(16, height - 12, machine, size=10)
    if warnings:
        svg.text(16, height - 42, "⚠", size=10, fill="#c98500")
        svg.text(30, height - 42, " · ".join(warnings), size=10)
    svg.add(f'<a href="{REPO_URL}"><text x="{width - 16}" y="{height - 12}" '
            f'font-size="10" fill="{INK_SOFT}" text-anchor="end" '
            f'text-decoration="underline">{esc(REPO_URL.removeprefix("https://"))}'
            f'</text></a>')

    with open(out_path, "w") as f:
        f.write(svg.finish(height))


def print_table(benchmarks):
    width = max(len(b["name"]) for b in benchmarks)
    print(f"{'benchmark':<{width}} {'time':>9} {'speed':>10} {'ratio':>7}")
    print("-" * (width + 30))
    for b in benchmarks:
        if b["kind"] == "blocks":
            print(f"{b['name']:<{width}} {b['blocks']:>8,} blocks, "
                  f"{b['members']:,} members, avg "
                  f"{fmt_bytes(b['block_output_bytes'])} compressed")
            continue
        ratio = f"{b['ratio']:>7.3f}" if "ratio" in b else f"{'-':>7}"
        print(f"{b['name']:<{width}} {b['seconds_mean']:>8.2f}s "
              f"{fmt_speed(b['bytes_per_second']):>10} {ratio}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json", help="bench.py JSON output")
    ap.add_argument("-o", "--output", default=None, help="output SVG path")
    ap.add_argument("--title", default=None, help="chart title")
    args = ap.parse_args()

    with open(args.json) as f:
        run = json.load(f)
    ctx = run.get("context", {})
    benchmarks = run["benchmarks"]
    title = args.title or "gzip tools, compress and decompress"
    out = args.output or args.json.rsplit(".", 1)[0] + ".svg"

    print_table(benchmarks)
    render(ctx, benchmarks, title, out)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
