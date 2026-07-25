#!/usr/bin/env python3
"""
Live read-only SPARQL HTTP endpoint for the Ontop POC.
=============================================================================

Starts Ontop in its ``endpoint`` (server) mode so an outside system can issue
standard SPARQL over HTTP against the virtual knowledge graph — the same
ontology + mapping the parity checks use, answered by rewriting SPARQL to SQL
on the fly. No triplestore, no data movement.

Read-only by design (identical guarantee to ``parity_check.py``):
  * The live WAL-mode database is opened ONLY to take a read-only backup
    snapshot. The endpoint is pointed at the snapshot via generated runtime
    properties, so the live file is never opened by the server at all.
  * A guard refuses to start unless those properties point at the snapshot and
    do NOT reference the live database path.
  * Ontop's endpoint has no bind-host option, so it listens on all interfaces
    inside the container; run it locally/manually and stop it when done (do not
    forward its port publicly). Reach it at http://127.0.0.1:<port>/sparql.
    Nothing here is wired into the Flask / HF Space app, Gradio, ArangoDB, or
    SolderEngine.

This module is also imported by ``endpoint_smoke_test.py`` for the start /
ready / query / teardown helpers, so the lifecycle logic lives in one place.

Usage::

    python3 poc/ontop-ontology-poc/sparql_endpoint.py            # port 8090
    python3 poc/ontop-ontology-poc/sparql_endpoint.py --port 9999
    ONTOP_ENDPOINT_PORT=9999 python3 .../sparql_endpoint.py

Press Ctrl-C to stop; the JVM is torn down cleanly (no orphan process).
"""
import argparse
import csv
import io
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POC_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(POC_DIR, "..", ".."))
INTEGRATIONS_DIR = os.path.join(REPO_ROOT, "replit_integrations")

ONTOP = os.path.join(POC_DIR, "tools", "ontop-cli-5.5.0", "ontop")
ONTOLOGY = os.path.join(POC_DIR, "ontology", "on_time_delivery.ttl")
MAPPING = os.path.join(POC_DIR, "mapping", "on_time_delivery.obda")
QUERIES = os.path.join(POC_DIR, "queries")
RESULTS = os.path.join(POC_DIR, "results")

DEFAULT_PORT = int(os.environ.get("ONTOP_ENDPOINT_PORT", "8090"))

# A tiny but real query that touches the full stack (Jetty + Ontop mapping +
# SQLite snapshot). Used only to decide the server is actually ready to serve.
READINESS_QUERY = (
    "PREFIX : <http://example.org/manufacturing/ontime#>\n"
    "SELECT ?score WHERE { ?d :opsOnTimeScore ?score } LIMIT 1"
)


def ensure_toolchain():
    """Download + verify the Ontop CLI / JDBC driver if missing (idempotent)."""
    if INTEGRATIONS_DIR not in sys.path:
        sys.path.insert(0, INTEGRATIONS_DIR)
    import ontop_poc_setup  # noqa: E402

    return ontop_poc_setup.ensure_toolchain()


def _guard_props_snapshot_only(props, snap):
    """Fail closed unless the JDBC properties point at the read-only snapshot and
    never at the DuckDB source database. This is the endpoint's read-only guarantee."""
    import parity_check

    text = open(props).read()
    if snap not in text:
        raise SystemExit(
            "Refusing to start: runtime properties do not point at the snapshot."
        )
    # DUCKDB_DB is the source file; runtime props must point at the SQLite
    # snapshot copy, not directly at the DuckDB source.
    if parity_check.DUCKDB_DB in text:
        raise SystemExit(
            "Refusing to start: runtime properties reference the DuckDB source "
            "database directly. Use the SQLite snapshot copy instead."
        )


def build_snapshot_and_props():
    """Take a read-only snapshot of the live DB and write JDBC properties that
    point Ontop at the snapshot. Returns ``(snapshot_path, properties_path)``."""
    import parity_check

    snap = parity_check.make_snapshot()
    props = parity_check.write_runtime_properties(snap)
    _guard_props_snapshot_only(props, snap)
    return snap, props


def endpoint_url(port):
    return f"http://127.0.0.1:{port}/sparql"


def find_free_port():
    """Ask the OS for an unused localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_endpoint(port, props, log_path):
    """Launch ``ontop endpoint`` in its own session (process group) so the whole
    JVM tree can be signalled at once. Output is redirected to ``log_path`` (a
    long-running JVM would otherwise fill an undrained PIPE and hang)."""
    os.makedirs(RESULTS, exist_ok=True)
    cmd = [
        ONTOP, "endpoint",
        "-m", MAPPING,
        "-t", ONTOLOGY,
        "-p", props,
        "--port", str(port),
    ]
    log = open(log_path, "w")
    try:
        proc = subprocess.Popen(
            cmd, cwd=POC_DIR, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()  # the child keeps its own copy of the fd
    return proc


def sparql_request(port, query, accept="text/csv", timeout=30):
    """POST a SPARQL query to the endpoint. Returns ``(status, body_text)``."""
    data = urllib.parse.urlencode({"query": query}).encode()
    req = urllib.request.Request(
        endpoint_url(port),
        data=data,
        headers={
            "Accept": accept,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def wait_until_ready(port, proc, timeout=120.0, interval=1.0):
    """Poll the endpoint with a real query until it answers 200, the process
    dies, or we time out. A real query confirms Jetty is up AND Ontop loaded the
    ontology/mapping AND the SQLite snapshot opened."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(
                f"Endpoint process exited early (code {proc.returncode}). "
                "See the endpoint log for details."
            )
        try:
            status, _ = sparql_request(port, READINESS_QUERY, timeout=5)
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last = exc  # not up yet (connection refused / warmup 5xx)
        time.sleep(interval)
    raise SystemExit(f"Endpoint not ready within {timeout:.0f}s (last error: {last}).")


def stop_endpoint(proc, timeout=10.0):
    """Terminate the endpoint's whole process group: SIGTERM, wait, then SIGKILL
    as a fallback. Treats an already-gone process as success (no orphan)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            continue


def parse_csv_first_value(body):
    """First data cell of a SPARQL CSV result, as a float."""
    rows = [r for r in csv.reader(io.StringIO(body)) if r]
    if len(rows) < 2:
        raise ValueError("no data rows in SPARQL CSV result")
    val = rows[1][0].strip()
    if "^^" in val:
        val = val.split("^^")[0]
    return float(val.strip().strip('"'))


def csv_data_row_count(body):
    """Number of data rows (excluding the header) in a SPARQL CSV result."""
    rows = [r for r in csv.reader(io.StringIO(body)) if r]
    return max(0, len(rows) - 1)


def _print_usage_examples(port):
    url = endpoint_url(port)
    on_time = (
        "PREFIX : <http://example.org/manufacturing/ontime#> "
        "SELECT (AVG(?s) AS ?onTimeRate) (COUNT(?s) AS ?deliveries) "
        "WHERE { ?d :opsOnTimeScore ?s }"
    )
    print("=" * 72)
    print(f"  Live SPARQL endpoint ready at  {url}")
    print("=" * 72)
    print("  On-time delivery rate over HTTP:")
    print(f'    curl -s "{url}" \\')
    print(f'      --data-urlencode "query={on_time}" \\')
    print('      -H "Accept: text/csv"')
    print()
    print("  Every supplier with its OPTIONAL (LEFT-JOIN) deliveries:")
    print(f'    curl -s "{url}" \\')
    print(f'      --data-urlencode "query@{os.path.join(QUERIES, "suppliers_optional_deliveries.rq")}" \\')
    print('      -H "Accept: text/csv"')
    print("=" * 72)
    print("  Press Ctrl-C to stop the server.")
    print("=" * 72, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live read-only SPARQL endpoint (Ontop).")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"TCP port to serve SPARQL on (default {DEFAULT_PORT}; "
             "env ONTOP_ENDPOINT_PORT).",
    )
    args = parser.parse_args(argv)

    ensure_toolchain()
    if not os.path.exists(ONTOP):
        raise SystemExit(
            "Ontop CLI not found. Run: python3 replit_integrations/ontop_poc_setup.py"
        )

    print("Building read-only snapshot of the live database...")
    snap, props = build_snapshot_and_props()
    log_path = os.path.join(RESULTS, f"endpoint_{args.port}.log")

    print(f"Starting Ontop SPARQL endpoint on port {args.port} (log: {log_path})...")
    proc = start_endpoint(args.port, props, log_path)

    # SIGTERM (e.g. workflow stop) -> KeyboardInterrupt so the finally tears down.
    def _term(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)

    try:
        wait_until_ready(args.port, proc)
        _print_usage_examples(args.port)
        proc.wait()
    except KeyboardInterrupt:
        print("\nStopping endpoint...")
    finally:
        stop_endpoint(proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
