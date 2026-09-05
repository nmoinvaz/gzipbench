#!/usr/bin/env python3
"""Build the gzip tools against zlib-ng develop into tools/bin.

Clones zlib-ng develop and builds it static with the zlib compatible API,
which also yields minigzip, then builds pigz and htslib's bgzip linking
that library, and gzip-ng pinned to the same zlib-ng commit. GNU gzip
carries its own deflate and MiGz uses the JVM's zlib, so those two stay
system provided. Writes tools/zlibng.json, the manifest bench.py records
and the graphs put in their footer.

bench.py prefers tools/bin over PATH, so a run after this script measures
the zlib-ng build. ZLIBNG_TAG, PIGZ_TAG, HTSLIB_VERSION, and GZIPNG_SRC
override the defaults.
"""
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

ZLIBNG_REPO = "https://github.com/zlib-ng/zlib-ng.git"
ZLIBNG_TAG = os.environ.get("ZLIBNG_TAG", "develop")
PIGZ_REPO = "https://github.com/madler/pigz.git"
PIGZ_TAG = os.environ.get("PIGZ_TAG", "master")
HTSLIB_VERSION = os.environ.get("HTSLIB_VERSION", "1.22.1")
GZIPNG_REPO = "https://github.com/nmoinvaz/gzip-ng.git"

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(TOOLS_DIR, "src")
BIN = os.path.join(TOOLS_DIR, "bin")
JOBS = str(multiprocessing.cpu_count())


def sh(argv, cwd=None):
    print("  $ " + " ".join(argv))
    subprocess.run(argv, cwd=cwd, check=True)


def clone(repo, tag, dest):
    """Shallow clone of one ref, or of the default branch when tag is None,
    moving branches update in place."""
    if not os.path.exists(dest):
        branch = ["-b", tag] if tag else []
        sh(["git", "clone", "--depth", "1"] + branch + [repo, dest])
    else:
        sh(["git", "-C", dest, "fetch", "--depth", "1", "origin", tag or "HEAD"])
        sh(["git", "-C", dest, "checkout", "-f", "FETCH_HEAD"])
    return subprocess.check_output(
        ["git", "-C", dest, "rev-parse", "HEAD"], text=True).strip()


def install_bin(path, name):
    dest = os.path.join(BIN, name)
    shutil.copy2(path, dest)
    print(f"  -> {dest}")


def build_zlibng():
    print("zlib-ng")
    src = os.path.join(SRC, "zlib-ng")
    commit = clone(ZLIBNG_REPO, ZLIBNG_TAG, src)
    prefix = os.path.join(src, "install")
    sh(["cmake", "-S", src, "-B", os.path.join(src, "build"),
        "-DCMAKE_BUILD_TYPE=Release", "-DZLIB_COMPAT=ON",
        "-DBUILD_SHARED_LIBS=OFF", "-DWITH_GTEST=OFF", "-DINSTALL_UTILS=ON",
        f"-DCMAKE_INSTALL_PREFIX={prefix}"])
    sh(["cmake", "--build", os.path.join(src, "build"), "-j", JOBS])
    sh(["cmake", "--install", os.path.join(src, "build")])
    install_bin(os.path.join(prefix, "bin", "minigzip"), "minigzip")

    with open(os.path.join(prefix, "include", "zlib.h")) as f:
        m = re.search(r'#define ZLIBNG_VERSION\s+"([^"]+)"', f.read())
    version = m.group(1) if m else ""
    return prefix, {"version": version, "commit": commit, "branch": ZLIBNG_TAG,
                    "date": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def build_pigz(prefix):
    print("pigz")
    src = os.path.join(SRC, "pigz")
    clone(PIGZ_REPO, PIGZ_TAG, src)
    subprocess.run(["make", "clean"], cwd=src, capture_output=True)
    sh(["make", f"-j{JOBS}", "pigz",
        f"CFLAGS=-O3 -I{prefix}/include",
        f"LIBS=-lm -lpthread {prefix}/lib/libz.a"], cwd=src)
    install_bin(os.path.join(src, "pigz"), "pigz")


def build_bgzip(prefix):
    print(f"htslib {HTSLIB_VERSION} bgzip")
    src = os.path.join(SRC, f"htslib-{HTSLIB_VERSION}")
    if not os.path.exists(src):
        url = ("https://github.com/samtools/htslib/releases/download/"
               f"{HTSLIB_VERSION}/htslib-{HTSLIB_VERSION}.tar.bz2")
        print(f"  fetching {url}")
        tar = os.path.join(SRC, f"htslib-{HTSLIB_VERSION}.tar.bz2")
        urllib.request.urlretrieve(url, tar)
        with tarfile.open(tar) as t:
            t.extractall(SRC, filter="data")
        os.unlink(tar)
    sh(["./configure", "--disable-bz2", "--disable-lzma", "--disable-libcurl",
        "--disable-plugins", "--without-libdeflate",
        f"CPPFLAGS=-I{prefix}/include", f"LDFLAGS=-L{prefix}/lib"], cwd=src)
    sh(["make", f"-j{JOBS}", "bgzip"], cwd=src)
    install_bin(os.path.join(src, "bgzip"), "bgzip")


def build_pigzpp(commit):
    print("pigzpp")
    src = os.path.join(SRC, "pigzpp")
    if not os.path.exists(src):
        sh(["git", "clone", "--depth", "1", "--recursive",
            "https://github.com/thammegowda/pigzpp.git", src])
    else:
        sh(["git", "-C", src, "fetch", "--depth", "1", "origin", "HEAD"])
        sh(["git", "-C", src, "checkout", "-f", "FETCH_HEAD"])
        sh(["git", "-C", src, "submodule", "update", "--init", "--recursive",
            "--depth", "1"])
    # Pin the embedded zlib-ng to the same commit as everything else, fall
    # back to the submodule's own pin if that combination does not build
    sub = subprocess.check_output(
        ["git", "-C", src, "submodule", "status"], text=True)
    zn = next((line.split()[1] for line in sub.splitlines()
               if "zlib-ng" in line), None)
    if zn:
        subprocess.run(["git", "-C", os.path.join(src, zn), "fetch",
                        "origin", commit], check=True)
        sh(["git", "-C", os.path.join(src, zn), "checkout", "-f", commit])
    try:
        sh(["make", "build"], cwd=src)
    except subprocess.CalledProcessError:
        if not zn:
            raise
        print("  zlib-ng develop pin failed, rebuilding with the bundled pin")
        sh(["git", "-C", src, "submodule", "update", "--force", zn])
        sh(["make", "build"], cwd=src)
    install_bin(os.path.join(src, "build", "pigzpp"), "pigzpp")


def build_gzipng(commit):
    print("gzip-ng")
    src = os.environ.get("GZIPNG_SRC")
    if not src:
        src = os.path.join(SRC, "gzip-ng")
        clone(GZIPNG_REPO, None, src)
    build = os.path.join(SRC, "gzip-ng-build")
    sh(["cmake", "-S", src, "-B", build, "-DCMAKE_BUILD_TYPE=Release",
        f"-DZLIBNG_TAG={commit}"])
    sh(["cmake", "--build", build, "-j", JOBS])
    for root, _, files in os.walk(build):
        if "gzip-ng" in files and "_deps" not in root:
            path = os.path.join(root, "gzip-ng")
            if os.access(path, os.X_OK):
                install_bin(path, "gzip-ng")
                return
    sys.exit("gzip-ng binary not found in the build tree")


def main():
    os.makedirs(SRC, exist_ok=True)
    os.makedirs(BIN, exist_ok=True)
    prefix, manifest = build_zlibng()
    build_pigz(prefix)
    build_bgzip(prefix)
    build_pigzpp(manifest["commit"])
    build_gzipng(manifest["commit"])
    with open(os.path.join(TOOLS_DIR, "zlibng.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print(f"zlib-ng {manifest['version']} @ {manifest['commit'][:9]}, "
          f"manifest in tools/zlibng.json")
    print("gzip and MiGz do not link zlib, the system ones are used")


if __name__ == "__main__":
    main()
