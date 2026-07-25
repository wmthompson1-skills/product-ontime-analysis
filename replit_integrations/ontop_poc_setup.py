#!/usr/bin/env python3
"""
Ontop POC toolchain setup (Python port of poc/ontop-ontology-poc/setup.sh).
=============================================================================

Downloads the pinned, checksum-verified Ontop CLI + SQLite JDBC driver (used by
automated CI parity checks) + DuckDB JDBC driver (used for manual ``ontop query``
runs against ``Utilities/SQLMesh/db.db``) into ``poc/ontop-ontology-poc/tools/``.

All artifacts land under ``tools/`` and are gitignored.
Re-runnable / idempotent: nothing is re-downloaded if it is already present.

Lives in ``replit_integrations/`` so it can be shared alongside the other
integration tools, but it points back into the POC folder for everything it
touches. A Java runtime is required at run time (``java -version``).

Importable: call :func:`ensure_toolchain` from another script (e.g. the demo
runner) to guarantee the toolchain is present, or run this file directly.
"""
import hashlib
import os
import subprocess
import sys
import urllib.request
import zipfile

ONTOP_VERSION = "5.5.0"
SQLITE_JDBC_VERSION = "3.49.1.0"

# DuckDB JDBC — used for manual `ontop query` runs against Utilities/SQLMesh/db.db.
# The automated CI parity checks export a SQLite snapshot from the DuckDB source and
# use the SQLite JDBC driver instead, so there is no runtime DuckDB JDBC dependency in CI.
# NOTE: This JDBC version (1.1.3) is the latest stable release available on Maven Central.
# If the installed Python duckdb library is a newer major version, manually verify that the
# JDBC driver can open DuckDB files produced by that version before using it for manual runs.
DUCKDB_JDBC_VERSION = "1.1.3"

# SHA-256 checksums of the pinned downloads (reproducibility guard).
ONTOP_ZIP_SHA256 = "430dff312e68e8ad41d26e6113160ca2365c28c4fe911926193000a33299458f"
SQLITE_JDBC_SHA256 = "5c8609d2ca341deb8c6f71778974b5ba4995c7d32d7c7c89d9392a3e72c39291"
DUCKDB_JDBC_SHA256 = "7bdfe781bb101e2e807397c460c1840955b40aecbaacf2a5e6fc490d80e4f7cd"

INTEGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(INTEGRATIONS_DIR, ".."))
POC_DIR = os.path.join(REPO_ROOT, "poc", "ontop-ontology-poc")
TOOLS = os.path.join(POC_DIR, "tools")
ONTOP_DIR = os.path.join(TOOLS, f"ontop-cli-{ONTOP_VERSION}")
ONTOP_BIN = os.path.join(ONTOP_DIR, "ontop")

ONTOP_URL = (
    "https://github.com/ontop/ontop/releases/download/"
    f"ontop-{ONTOP_VERSION}/ontop-cli-{ONTOP_VERSION}.zip"
)
SQLITE_JDBC_URL = (
    "https://repo1.maven.org/maven2/org/xerial/sqlite-jdbc/"
    f"{SQLITE_JDBC_VERSION}/sqlite-jdbc-{SQLITE_JDBC_VERSION}.jar"
)
DUCKDB_JDBC_URL = (
    "https://repo1.maven.org/maven2/org/duckdb/duckdb_jdbc/"
    f"{DUCKDB_JDBC_VERSION}/duckdb_jdbc-{DUCKDB_JDBC_VERSION}.jar"
)


def _download(url, dest):
    """Download ``url`` to ``dest`` (follows redirects, like ``curl -fL``)."""
    req = urllib.request.Request(url, headers={"User-Agent": "ontop-poc-setup/1.0"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)


def _verify(path, expected_sha256):
    """Fail closed if ``path`` does not match the pinned checksum."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected_sha256:
        raise SystemExit(
            f"Checksum mismatch for {path}\n"
            f"  expected {expected_sha256}\n"
            f"  actual   {actual}"
        )


def _make_executable(path):
    mode = os.stat(path).st_mode
    os.chmod(path, mode | 0o111)


def ensure_toolchain():
    """Download + verify the Ontop CLI and SQLite JDBC driver if missing.

    Returns the path to the ``ontop`` executable. Idempotent.
    """
    os.makedirs(TOOLS, exist_ok=True)

    if not os.access(ONTOP_BIN, os.X_OK):
        print(f"Downloading Ontop CLI {ONTOP_VERSION}...")
        zip_path = os.path.join(TOOLS, f"ontop-cli-{ONTOP_VERSION}.zip")
        _download(ONTOP_URL, zip_path)
        print("Verifying checksum...")
        _verify(zip_path, ONTOP_ZIP_SHA256)
        os.makedirs(ONTOP_DIR, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(ONTOP_DIR)
        _make_executable(ONTOP_BIN)
    else:
        print(f"Ontop CLI {ONTOP_VERSION} already present.")

    jdbc_dir = os.path.join(ONTOP_DIR, "jdbc")
    os.makedirs(jdbc_dir, exist_ok=True)

    sqlite_jar = os.path.join(jdbc_dir, f"sqlite-jdbc-{SQLITE_JDBC_VERSION}.jar")
    if not os.path.isfile(sqlite_jar):
        print(f"Downloading sqlite-jdbc {SQLITE_JDBC_VERSION}...")
        _download(SQLITE_JDBC_URL, sqlite_jar)
        print("Verifying checksum...")
        _verify(sqlite_jar, SQLITE_JDBC_SHA256)
    else:
        print(f"sqlite-jdbc {SQLITE_JDBC_VERSION} already present.")

    duckdb_jar = os.path.join(jdbc_dir, f"duckdb_jdbc-{DUCKDB_JDBC_VERSION}.jar")
    if not os.path.isfile(duckdb_jar):
        print(f"Downloading duckdb-jdbc {DUCKDB_JDBC_VERSION} (for manual DuckDB runs)...")
        _download(DUCKDB_JDBC_URL, duckdb_jar)
        print("Verifying checksum...")
        _verify(duckdb_jar, DUCKDB_JDBC_SHA256)
    else:
        print(f"duckdb-jdbc {DUCKDB_JDBC_VERSION} already present.")

    return ONTOP_BIN


def _java_version_line():
    """Mirror ``java -version 2>&1 | head -1`` (java writes to stderr)."""
    try:
        res = subprocess.run(
            ["java", "-version"], capture_output=True, text=True
        )
    except FileNotFoundError:
        return "NOT FOUND \u2014 install a Java runtime (Replit module java-graalvm22.3)"
    combined = (res.stderr or "") + (res.stdout or "")
    lines = [ln for ln in combined.splitlines() if ln.strip()]
    return lines[0] if lines else "(unknown)"


def main():
    ensure_toolchain()
    print(f"Toolchain ready. Java runtime required (java -version): {_java_version_line()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
