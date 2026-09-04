#!/usr/bin/env python3
"""Fetch the pinned MiGz jars from Maven Central and compile the pipe CLI.

Populates tools/migz/, after which bench.py picks MiGz up automatically.
MIGZ_VERSION, CONCURRENTLI_VERSION, and JAVAC override the defaults.
"""
import os
import shutil
import subprocess
import sys
import urllib.request

MIGZ_VERSION = os.environ.get("MIGZ_VERSION", "2.0.beta-1")
CONCURRENTLI_VERSION = os.environ.get("CONCURRENTLI_VERSION", "1.3.2")
MAVEN = "https://repo1.maven.org/maven2"

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(TOOLS_DIR, "migz")


def find_javac():
    """The stub at /usr/bin/javac errors without a JDK, so probe candidates."""
    for c in (os.environ.get("JAVAC"), shutil.which("javac"),
              "/opt/homebrew/opt/openjdk/bin/javac",
              "/usr/local/opt/openjdk/bin/javac"):
        if c and os.path.exists(c):
            probe = subprocess.run([c, "-version"], capture_output=True)
            if probe.returncode == 0:
                return c
    sys.exit("javac not found, install a JDK or set JAVAC")


def fetch(group, jar):
    path = os.path.join(DEST, jar)
    if not os.path.exists(path):
        urllib.request.urlretrieve(f"{MAVEN}/{group}/{jar}", path)
    print(f"  {jar}")


def main():
    os.makedirs(DEST, exist_ok=True)
    migz_jar = f"migz-{MIGZ_VERSION}.jar"
    fetch(f"com/linkedin/migz/migz/{MIGZ_VERSION}", migz_jar)
    fetch(f"com/concurrentli/concurrentli/{CONCURRENTLI_VERSION}",
          f"concurrentli-{CONCURRENTLI_VERSION}.jar")
    subprocess.run([find_javac(), "-cp", os.path.join(DEST, migz_jar),
                    "-d", DEST, os.path.join(TOOLS_DIR, "MigzCli.java")],
                   check=True)
    print("  MigzCli.class")


if __name__ == "__main__":
    main()
