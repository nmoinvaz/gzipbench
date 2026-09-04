#!/usr/bin/env python3
"""Benchmark gzip command line tools against each other.

Measures whole-process wall time compressing and decompressing one input
stream, stdin to stdout, the way the tools sit in pipelines, so startup,
threading, and IO all count. Compression writes a real file, decompression
writes to /dev/null, and every decoder's warmup output is verified against
the input's size and crc.

The variant matrix follows the gzip-ng README benchmarks. Each tool
compresses the input across the level ladder, serial and parallel variants
separately, parallel tools sweep the thread counts at level 6, and the
decompression grid crosses decoders with producers, gzip-ng decoding bgzip
and MiGz output as well as its own. A deflate block census then compresses
a capped sample at level 6 in normal, rsyncable, and independent modes and
counts the blocks and members in each stream.

Usage:
    ./bench.py [files...] [-o out.json] [--size-mb N] [--levels 1,2,...,9]
               [--threads auto|1,2,4] [--runs 3] [--warmup 1] [--blocks-mb 32]

Tools found on PATH are benchmarked, missing ones are skipped and noted.
GZIPNG, PIGZ, GZIP, MINIGZIP, BGZIP, and JAVA override the lookup. MiGz
needs tools/fetch-migz.sh run once. Needs only the Python standard library.
"""
import argparse
import glob
import json
import math
import os
import platform
import random
import shutil
import socket
import statistics
import string
import subprocess
import sys
import tempfile
import time
import zlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(BASE_DIR, "tools")
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
import deflate_blocks  # noqa: E402

# Fixed series order, the graph assigns colors by it
SERIES_ORDER = ["gzip-ng -p", "pigz -p", "bgzip -@", "migz",
                "gzip-ng", "minigzip", "gzip"]


def find_java():
    for c in (os.environ.get("JAVA"), shutil.which("java"),
              "/opt/homebrew/opt/openjdk/bin/java",
              "/usr/local/opt/openjdk/bin/java"):
        if c and os.path.exists(c):
            try:
                subprocess.run([c, "-version"], capture_output=True, check=True)
                return c
            except (subprocess.SubprocessError, OSError):
                continue
    return None


def find_migz():
    """Classpath for the MiGz pipe CLI, None until fetch-migz.sh has run."""
    jars = glob.glob(os.path.join(TOOLS_DIR, "migz", "*.jar"))
    cls = os.path.join(TOOLS_DIR, "migz", "MigzCli.class")
    if not jars or not os.path.exists(cls):
        return None
    return os.pathsep.join(jars + [os.path.join(TOOLS_DIR, "migz")])


def version_of(argv, pattern=None):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return ""
    text = (r.stdout + r.stderr).strip()
    return text.splitlines()[0] if text else ""


class Tool:
    """One executable: where it is, its version, and its command lines."""

    # Extra compress argv per block census mode, tools override what they support
    modes = {"normal": []}

    def __init__(self, slug, path, version):
        self.slug = slug
        self.path = path
        self.version = version

    def compress(self, level, threads):
        raise NotImplementedError

    def decompress(self, threads):
        raise NotImplementedError


class GzipLike(Tool):
    """gzip flag conventions, with an optional threads flag."""

    def __init__(self, slug, path, version, threads_flag=None, modes=None):
        super().__init__(slug, path, version)
        self.threads_flag = threads_flag
        if modes:
            self.modes = modes

    def compress(self, level, threads):
        argv = [self.path, f"-{level}"]
        if threads is not None:
            argv += [self.threads_flag, str(threads)]
        return argv + ["-c"]

    def decompress(self, threads):
        argv = [self.path, "-d"]
        if threads is not None:
            argv += [self.threads_flag, str(threads)]
        return argv + ["-c"]


class Bgzip(Tool):
    def compress(self, level, threads):
        return [self.path, "-l", str(level), "-@", str(threads), "-c"]

    def decompress(self, threads):
        return [self.path, "-d", "-@", str(threads), "-c"]


class Minigzip(Tool):
    def compress(self, level, threads):
        return [self.path, f"-{level}"]

    def decompress(self, threads):
        return [self.path, "-d"]


class Migz(Tool):
    """MiGz picks its own thread count from the common pool."""

    def __init__(self, java, classpath, version):
        super().__init__("migz", java, version)
        self.classpath = classpath

    def compress(self, level, threads):
        return [self.path, "-cp", self.classpath, "MigzCli", "c", str(level)]

    def decompress(self, threads):
        return [self.path, "-cp", self.classpath, "MigzCli", "d"]


def locate_tools():
    tools = {}

    def path_for(env, name):
        built = os.path.join(TOOLS_DIR, "bin", name)
        return (os.environ.get(env)
                or (built if os.path.exists(built) else None)
                or shutil.which(name))

    p = path_for("GZIPNG", "gzip-ng")
    if p:
        tools["gzipng"] = GzipLike("gzipng", p, version_of([p, "-V"]), "-p",
                                   modes={"normal": [], "rsyncable": ["--rsyncable"]})
    p = path_for("PIGZ", "pigz")
    if p:
        tools["pigz"] = GzipLike("pigz", p, version_of([p, "--version"]), "-p",
                                 modes={"normal": [], "rsyncable": ["--rsyncable"],
                                        "independent": ["-i"]})
    p = path_for("GZIP", "gzip")
    if p:
        tools["gzip"] = GzipLike("gzip", p, version_of([p, "--version"]),
                                 modes={"normal": [], "rsyncable": ["--rsyncable"]})
    p = path_for("MINIGZIP", "minigzip")
    if p:
        tools["minigzip"] = Minigzip("minigzip", p, "minigzip")
    p = path_for("BGZIP", "bgzip")
    if p:
        tools["bgzip"] = Bgzip("bgzip", p, version_of([p, "--version"]))
    java = find_java()
    cp = find_migz()
    if java and cp:
        jar = next(j for j in cp.split(os.pathsep) if "migz-" in j)
        ver = os.path.basename(jar)[len("migz-"):-len(".jar")]
        jvm = version_of([java, "-version"])
        tools["migz"] = Migz(java, cp, f"MiGz {ver} on {jvm}")
    return tools


# Variant slugs in benchmark names, each a (tool, parallel) pair
VARIANTS = {
    "gzipng": ("gzipng", False, "gzip-ng"),
    "gzipng-p": ("gzipng", True, "gzip-ng -p"),
    "pigz-p": ("pigz", True, "pigz -p"),
    "gzip": ("gzip", False, "gzip"),
    "minigzip": ("minigzip", False, "minigzip"),
    "bgzip-p": ("bgzip", True, "bgzip -@"),
    "migz": ("migz", False, "migz"),
}

# gzip-ng's parallel blocks are always rsync friendly, --rsyncable only
# changes the plain stream, so the census row would duplicate normal
CENSUS_SKIP = {("gzipng-p", "rsyncable")}


def zlibng_info(tools):
    """zlib-ng provenance, the build_tools.py manifest when present, the
    version gzip-ng compiled in otherwise."""
    manifest = os.path.join(TOOLS_DIR, "zlibng.json")
    if os.path.exists(manifest):
        with open(manifest) as f:
            return json.load(f)
    gz = tools.get("gzipng")
    if gz and "zlib-ng" in gz.version:
        return {"version": gz.version.split("zlib-ng", 1)[1].strip(" ()")}
    return None


def synthesize(path, size_mb, seed=20260904):
    """Deterministic mixed input, source-like text, logs, and binary blocks,
    weighted so gzip -6 lands near the 4.8 to 1 of the gzip-ng README corpus."""
    rng = random.Random(seed)
    words = ["".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 10)))
             for _ in range(400)]
    idents = [w + s for w in words[:80]
              for s in ("_t", "_len", "_ptr", "()", "->next", "[i]")]

    def code_block(n):
        """Source repeats its own lines, so sample from a per-block pool."""
        pool = [("    " * rng.randint(0, 3)
                 + " ".join(rng.choices(idents, k=rng.randint(2, 6)))
                 + rng.choice([";", " {", "}", " = 0;", " return;"]) + "\n")
                for _ in range(140)]
        out = []
        size = 0
        while size < n:
            line = rng.choice(pool)
            out.append(line)
            size += len(line)
        return "".join(out).encode()[:n]

    def log_block(n):
        """Build output repeats paths and messages around unique timestamps."""
        paths = ["/".join(rng.choices(words, k=3)) + ".o" for _ in range(40)]
        msgs = [" ".join(rng.choices(words, k=rng.randint(3, 8)))
                for _ in range(60)]
        out = []
        size = 0
        while size < n:
            line = (f"[{rng.randint(0, 86400):05d}.{rng.randint(0, 999):03d}] "
                    f"/{rng.choice(paths)} {rng.getrandbits(32):08x} "
                    f"{rng.choice(msgs)}\n")
            out.append(line)
            size += len(line)
        return "".join(out).encode()[:n]

    def random_block(n):
        return rng.getrandbits(8 * n).to_bytes(n, "little")

    makers = [code_block] * 12 + [log_block] * 11 + [random_block] * 2
    with open(path, "wb") as f:
        for _ in range(size_mb * 16):
            f.write(rng.choice(makers)(65536))


def crc_of(path):
    crc = 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
    return crc


def run_once(argv, stdin_path, stdout_path):
    with open(stdin_path, "rb") as fi, open(stdout_path, "wb") as fo:
        t0 = time.perf_counter()
        r = subprocess.run(argv, stdin=fi, stdout=fo, stderr=subprocess.PIPE)
    if r.returncode != 0:
        sys.exit(f"{' '.join(argv)} failed: {r.stderr.decode().strip()}")
    return time.perf_counter() - t0


def measure(argv, stdin_path, stdout_path, runs, warmup, verify=None):
    """Wall times over runs, after warmups. A verify pair (size, crc) checks
    the first warmup's output before the timed runs write to /dev/null.
    Short benchmarks repeat up to tenfold, toward two seconds in total."""
    if verify:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(stdin_path),
                                         delete=False) as tmp:
            out = tmp.name
        try:
            run_once(argv, stdin_path, out)
            size, crc = os.path.getsize(out), crc_of(out)
            if (size, crc) != verify:
                sys.exit(f"{' '.join(argv)} output mismatch, "
                         f"{size} bytes crc {crc:08x}, "
                         f"expected {verify[0]} bytes crc {verify[1]:08x}")
        finally:
            os.unlink(out)
        warmup -= 1
    for _ in range(max(warmup, 0)):
        run_once(argv, stdin_path, stdout_path)
    times = [run_once(argv, stdin_path, stdout_path)]
    runs = max(runs, min(10, math.ceil(2.0 / max(times[0], 0.01))))
    while len(times) < runs:
        times.append(run_once(argv, stdin_path, stdout_path))
    return {
        "seconds_mean": statistics.fmean(times),
        "seconds_min": min(times),
        "seconds_stddev": statistics.stdev(times) if len(times) > 1 else 0.0,
    }


def cpu_brand():
    if sys.platform == "darwin":
        try:
            return subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
        except (subprocess.SubprocessError, OSError):
            return ""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return ""


def parse_threads(spec, ncpus):
    if spec != "auto":
        return sorted({int(t) for t in spec.split(",")})
    threads = []
    t = 1
    while t < ncpus:
        threads.append(t)
        t *= 2
    return threads + [ncpus]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="input files, concatenated into one stream")
    ap.add_argument("-o", "--output", default="gzipbench.json", help="output JSON path")
    ap.add_argument("--size-mb", type=int, default=256,
                    help="synthetic input size when no files are given")
    ap.add_argument("--levels", default="1,2,3,4,5,6,7,8,9",
                    help="compression level ladder, must include 6")
    ap.add_argument("--threads", default="auto",
                    help="thread counts to sweep, auto is powers of two up to the cpus")
    ap.add_argument("--runs", type=int, default=3,
                    help="timed runs per benchmark, short ones repeat toward two seconds")
    ap.add_argument("--warmup", type=int, default=1, help="warmup runs per benchmark")
    ap.add_argument("--blocks-mb", type=int, default=32,
                    help="input sample for the deflate block census, 0 disables it")
    args = ap.parse_args()

    levels = sorted({int(x) for x in args.levels.split(",")})
    if 6 not in levels:
        ap.error("the level ladder must include 6, the decompression grid runs on it")
    ncpus = os.cpu_count() or 1
    threads = parse_threads(args.threads, ncpus)
    tmax = threads[-1]

    tools = locate_tools()
    for slug in ("gzipng", "pigz", "gzip", "minigzip", "bgzip", "migz"):
        note = tools[slug].version if slug in tools else "missing, skipped"
        print(f"{slug:<10} {note}")
    missing = [s for s in ("gzipng", "pigz", "gzip", "minigzip", "bgzip", "migz")
               if s not in tools]

    # Sampled before the run, afterwards it reflects the benchmark's own work
    load_avg = list(os.getloadavg())

    work = tempfile.mkdtemp(prefix="gzipbench-")
    try:
        data = os.path.join(work, "data")
        if args.files:
            with open(data, "wb") as out:
                for path in args.files:
                    with open(path, "rb") as f:
                        shutil.copyfileobj(f, out)
            desc = ", ".join(os.path.basename(p) for p in args.files)
        else:
            print(f"generating {args.size_mb} MiB of synthetic mixed input")
            synthesize(data, args.size_mb)
            desc = f"synthetic mixed {args.size_mb} MiB"
        input_bytes = os.path.getsize(data)
        input_crc = crc_of(data)

        benchmarks = []
        outputs = {}

        def compress(vslug, level, t):
            tool, parallel, series = VARIANTS[vslug]
            if tool not in tools:
                return
            argv = tools[tool].compress(level, t if parallel else None)
            out = os.path.join(work, f"{vslug}-{level}-{t or 0}.gz")
            m = measure(argv, data, out, args.runs, args.warmup)
            out_bytes = os.path.getsize(out)
            if level == 6 and t in (tmax, None):
                outputs[vslug] = out
            b = {
                "name": f"compress/{vslug}/level:{level}"
                        + (f"/threads:{t}" if parallel else ""),
                "kind": "compress", "series": series, "variant": vslug,
                "level": level, "threads": t if parallel else None,
                **m,
                "bytes_per_second": input_bytes / m["seconds_mean"],
                "output_bytes": out_bytes,
                "ratio": input_bytes / out_bytes,
            }
            benchmarks.append(b)
            print(f"{b['name']:<44} {b['seconds_mean']:7.2f} s "
                  f"{b['bytes_per_second'] / 1e6:8.0f} MB/s  ratio {b['ratio']:.2f}")

        def decompress(vslug, producer, t):
            tool, parallel, series = VARIANTS[vslug]
            if tool not in tools or producer not in outputs:
                return
            argv = tools[tool].decompress(t if parallel else None)
            m = measure(argv, outputs[producer], os.devnull,
                        args.runs, args.warmup, verify=(input_bytes, input_crc))
            b = {
                "name": f"decompress/{vslug}/from:{producer}"
                        + (f"/threads:{t}" if parallel else ""),
                "kind": "decompress", "series": series, "variant": vslug,
                "producer": VARIANTS[producer][2], "producer_variant": producer,
                "threads": t if parallel else None,
                **m,
                "bytes_per_second": input_bytes / m["seconds_mean"],
            }
            benchmarks.append(b)
            print(f"{b['name']:<44} {b['seconds_mean']:7.2f} s "
                  f"{b['bytes_per_second'] / 1e6:8.0f} MB/s")

        # Level ladders, serial tools and parallel tools at the full thread count
        for level in levels:
            for vslug in ("gzipng", "gzip", "minigzip"):
                compress(vslug, level, None)
            for vslug in ("gzipng-p", "pigz-p", "bgzip-p", "migz"):
                compress(vslug, level, tmax)

        # Thread sweep at level 6, the full count is already measured above
        for t in threads[:-1]:
            for vslug in ("gzipng-p", "pigz-p", "bgzip-p"):
                compress(vslug, 6, t)

        # Decompression grid from the gzip-ng README, gzip-ng and bgzip sweep
        # their own output across the thread counts
        for t in threads:
            decompress("gzipng-p", "gzipng-p", t)
        for t in threads:
            decompress("bgzip-p", "bgzip-p", t)
        decompress("gzipng-p", "bgzip-p", tmax)
        decompress("gzipng-p", "migz", tmax)
        decompress("migz", "migz", None)
        decompress("gzipng", "gzipng", None)
        decompress("minigzip", "minigzip", None)
        decompress("pigz-p", "pigz-p", tmax)
        decompress("gzip", "gzip", None)

        # Deflate block census, level 6 streams in each mode a tool supports,
        # on a capped sample because the scan decodes every Huffman symbol
        # in pure Python
        sample, sample_bytes = data, input_bytes
        if args.blocks_mb and input_bytes > args.blocks_mb << 20:
            sample = os.path.join(work, "sample")
            with open(data, "rb") as f, open(sample, "wb") as out:
                left = args.blocks_mb << 20
                while left > 0 and (chunk := f.read(min(left, 1 << 20))):
                    out.write(chunk)
                    left -= len(chunk)
            sample_bytes = os.path.getsize(sample)

        def census(vslug, mode, t):
            tool, parallel, series = VARIANTS[vslug]
            if tool not in tools or (vslug, mode) in CENSUS_SKIP:
                return
            extra = tools[tool].modes.get(mode)
            if extra is None:
                return
            argv = tools[tool].compress(6, t if parallel else None)
            argv[1:1] = extra
            out = os.path.join(work, f"blocks-{vslug}-{mode}.gz")
            with open(sample, "rb") as fi, open(out, "wb") as fo:
                r = subprocess.run(argv, stdin=fi, stdout=fo,
                                   stderr=subprocess.PIPE)
            if r.returncode != 0:
                print(f"blocks/{vslug}/mode:{mode} skipped: "
                      f"{r.stderr.decode().strip()}")
                return
            try:
                c = deflate_blocks.scan_path(out)
            except deflate_blocks.BadStream as e:
                sys.exit(f"{' '.join(argv)} produced an unparsable stream: {e}")
            if c["produced_bytes"] != sample_bytes:
                sys.exit(f"{' '.join(argv)} stream decodes to "
                         f"{c['produced_bytes']} bytes, expected {sample_bytes}")
            b = {
                "name": f"blocks/{vslug}/mode:{mode}",
                "kind": "blocks", "series": series, "variant": vslug,
                "mode": mode, "level": 6, "threads": t if parallel else None,
                "input_bytes": sample_bytes,
                "output_bytes": os.path.getsize(out),
                "members": c["members"], "blocks": c["blocks"],
                "stored": c["stored"], "fixed": c["fixed"],
                "dynamic": c["dynamic"],
                "block_output_bytes": c["deflate_bytes"] / c["blocks"],
                "block_input_bytes": sample_bytes / c["blocks"],
            }
            benchmarks.append(b)
            print(f"{b['name']:<44} {b['blocks']:>7} blocks "
                  f"{b['members']:>6} members  avg "
                  f"{b['block_output_bytes'] / 1e3:7.1f} kB out "
                  f"{b['block_input_bytes'] / 1e3:7.1f} kB in")

        if args.blocks_mb:
            for vslug in VARIANTS:
                for mode in ("normal", "rsyncable", "independent"):
                    census(vslug, mode, tmax)

        result = {
            "context": {
                "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "host_name": socket.gethostname(),
                "num_cpus": ncpus,
                "cpu_brand": cpu_brand(),
                "os": platform.platform(),
                "load_avg": load_avg,
                "input": {"description": desc, "bytes": input_bytes},
                "runs": args.runs, "warmup": args.warmup,
                "threads": threads,
                "tools": {s: t.version for s, t in tools.items()},
                "zlibng": zlibng_info(tools),
                "missing": missing,
            },
            "benchmarks": benchmarks,
        }
        with open(args.output, "w") as f:
            json.dump(result, f, indent=1)
        print(f"\nwrote {args.output}")
        if missing:
            print("skipped: " + ", ".join(missing))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
