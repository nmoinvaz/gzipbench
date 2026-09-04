## Project Basics

- Everything is Python standard library only, no third party packages.
- `bench.py` measures whole processes, stdin to stdout, so startup,
  threading, and IO all count. In-process buffer codec benchmarks belong in
  codecbench instead.
- Tools resolve from `tools/bin` first, then PATH; `GZIPNG`, `PIGZ`, `GZIP`,
  `MINIGZIP`, `BGZIP`, and `JAVA` override the lookup.
- `tools/build_tools.py` builds gzip-ng, minigzip, pigz, and bgzip against
  zlib-ng develop into `tools/bin` and writes `tools/zlibng.json`. Always
  benchmark those builds, not the Homebrew tools, so every zlib-linked tool
  measures the same library. GNU gzip and MiGz do not link zlib.
- `ZLIBNG_TAG` pins a zlib-ng commit, `GZIPNG_SRC` points the build at a
  local gzip-ng checkout for work in progress.
- MiGz needs `python3 tools/fetch_migz.py` once and a JDK.
- Benchmark names are stable identifiers
  (`compress/<variant>/level:<n>[/threads:<t>]`,
  `decompress/<variant>/from:<producer>`, `blocks/<variant>/mode:<m>`), runs
  compare row by row on them.

## Benchmarking

- The level ladder must include 6, the decompression grid and block census
  run on level 6 output.
- Run one bench.py at a time and keep the machine otherwise idle, whole
  process wall times are sensitive to contention. Look for other benchmark
  processes before starting.
- The graph footers flag skipped tools, high load, and benchmarks with CV
  above 3%; treat a flagged run as suspect and rerun it.
- Trim the matrix while iterating (`--size-mb 16 --levels 1,6,9 --runs 1
  --threads 1,4`), full defaults are for publishable runs only.
- A full default run takes tens of minutes, run it in one background
  process and wait, never several at once.

### Thermal Throttling

Sustained runs heat the CPU until it downclocks and later benchmarks run
slower than earlier ones. Signs: later rows slower than equivalent earlier
ones, wildly different results across runs, CV above 3% on rows that are
normally under 1%. Verify with a quick A/B sanity check before committing
to a full run. See
https://gist.github.com/nmoinvaz/42d997329fc4878993ec0f4f8e600c91 for
platform-specific steps to stabilize benchmark environments.

### Comparing Results

- `python3 scripts/compare_runs.py base.json contender.json` matches rows
  by name and reports time and ratio deltas.

### Presenting Results

- `python3 scripts/graph_runs.py run.json` renders the SVG and prints the
  aggregate table. Refresh `results/all-tools.json` and its SVG together,
  the README showcases them.
- Always show performance changes as percentages (e.g. -18.4%), not as
  speedup ratios (e.g. 1.23x).
- When publishing results as a GitHub gist, start the title and filename
  with the project name and include the machine specs.
