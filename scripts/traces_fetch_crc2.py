"""Fetch the 19 CRC-2 SPEC CPU2006 traces ArchAgent used into $AFAR_ASSETS (M3).

The 2nd Cache Replacement Championship (CRC-2, 2017) traces are one shared
Dropbox folder, linked from ChampSim's README as http://bit.ly/2t2nkUj.
Dropbox serves a shared folder only as one generated zip (CRC2_trace.zip,
about 28 GB); links to single files return HTML. So this script:

  1. downloads the zip once into $AFAR_ASSETS/traces/crc2/ (on /data, never /tmp);
  2. lists every member in configs/traces/crc2-folder-index.txt;
  3. extracts only the 19 traces (zipfile checks each member's CRC-32),
     pins each by sha256 and instruction count in configs/traces/crc2-archagent19.json;
  4. deletes the zip, unless --keep-zip or a trace is missing from it.

    make traces-fetch      (coe-hpc1 only: needs internet; about 10 minutes)

Safe to re-run: when every pinned trace is present at its pinned size, it does
nothing. The zip is written under a .partial name, so an interrupted download
never looks complete. Dropbox generates the zip per request, so a failed
download starts over rather than resuming.
"""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "https://www.dropbox.com/sh/hh09tt8myuz0jbp/AACAS5zMWHL7coVuS-RbpUksa?dl=1"
PIN = ROOT / "configs" / "traces" / "crc2-archagent19.json"
INDEX = ROOT / "configs" / "traces" / "crc2-folder-index.txt"
RECORD_BYTES = 64  # sizeof(input_instr), external/ChampSim/inc/trace_instruction.h

# The memory-intensive SPEC CPU2006 SimPoints ArchAgent evaluated
# (arXiv 2602.22425, following Mockingjay: LLC MPKI > 1 under LRU).
ARCHAGENT19 = [
    "GemsFDTD_109B", "astar_163B", "bwaves_1861B", "bzip2_183B", "cactusADM_734B",
    "calculix_2670B", "gcc_13B", "lbm_94B", "leslie3d_1116B", "libquantum_1210B",
    "mcf_46B", "milc_360B", "omnetpp_340B", "soplex_66B", "sphinx3_2520B",
    "tonto_2834B", "wrf_1212B", "xalancbmk_748B", "zeusmp_600B",
]


def log(message):
    print(f"{datetime.datetime.now():%H:%M:%S} {message}", flush=True)


def gb(n):
    return f"{n / 1e9:.2f} GB"


def expected_size():
    """The zip's Content-Length after redirects, or None (headers only, no body)."""
    out = subprocess.run(["curl", "--silent", "--show-error", "--head", "--location", "--max-time", "120", URL],
                         capture_output=True, text=True)
    sizes = [line.split(":", 1)[1].strip() for line in out.stdout.splitlines()
             if line.lower().startswith("content-length:")]
    return int(sizes[-1]) if sizes and sizes[-1].isdigit() else None


def download(zip_path):
    partial = zip_path.with_name(zip_path.name + ".partial")
    partial.unlink(missing_ok=True)
    total = expected_size()
    log(f"downloading {URL} -> {partial} ({gb(total) if total else 'size unknown'})")
    # --speed-limit/--speed-time abort a stalled transfer instead of hanging.
    cmd = ["curl", "--fail", "--location", "--silent", "--show-error",
           "--speed-limit", "1048576", "--speed-time", "300", "--output", str(partial), URL]
    started = time.monotonic()
    proc = subprocess.Popen(cmd)
    while proc.poll() is None:
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            size = partial.stat().st_size if partial.exists() else 0
            rate = size / max(time.monotonic() - started, 1) / 1e6
            share = f" of {gb(total)}" if total else ""
            log(f"  {gb(size)}{share} at {rate:.0f} MB/s")
    size = partial.stat().st_size if partial.exists() else 0
    if proc.returncode != 0 or (total and size != total):
        partial.unlink(missing_ok=True)
        sys.exit(f"traces_fetch_crc2: download failed (curl exit {proc.returncode}, {gb(size)}"
                 f"{f' of {gb(total)}' if total else ''}); nothing kept. Re-run to start over.")
    os.replace(partial, zip_path)
    log(f"downloaded {gb(size)} in {time.monotonic() - started:.0f}s")


def xz_instructions(path):
    """Instruction count from the xz index (uncompressed bytes / record size); no decompression."""
    out = subprocess.run(["xz", "--robot", "--list", str(path)], capture_output=True, text=True, check=True)
    totals = [line.split("\t") for line in out.stdout.splitlines() if line.startswith("totals\t")]
    uncompressed = int(totals[0][4])
    if uncompressed % RECORD_BYTES:
        sys.exit(f"traces_fetch_crc2: {path} holds {uncompressed} bytes, not a whole number of {RECORD_BYTES}-byte records")
    return uncompressed // RECORD_BYTES


def extract(zip_path, dest, assets):
    with zipfile.ZipFile(zip_path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        INDEX.parent.mkdir(parents=True, exist_ok=True)
        INDEX.write_text("# Members of CRC2_trace.zip (" + URL + "), as listed on "
                         + f"{datetime.date.today()}: name, bytes, CRC-32\n"
                         + "".join(f"{m.filename}\t{m.file_size}\t{m.CRC:08x}\n"
                                   for m in sorted(members, key=lambda m: m.filename)))
        log(f"{len(members)} members listed in {INDEX.relative_to(ROOT)}")

        by_name = {}
        for m in members:
            by_name.setdefault(Path(m.filename).name, []).append(m)
        wanted = {name: by_name.get(f"{name}.trace.xz", []) for name in ARCHAGENT19}
        missing = [n for n, found in wanted.items() if len(found) != 1]
        if missing:
            sys.exit(f"traces_fetch_crc2: missing or duplicated in the zip: {', '.join(missing)}; "
                     f"zip kept at {zip_path} for inspection")

        traces = []
        for name in ARCHAGENT19:
            member = wanted[name][0]
            out = dest / f"{name}.trace.xz"
            tmp = out.with_name(out.name + ".partial")
            digest = hashlib.sha256()
            # Written by our own name, never the member's path, so the zip
            # can't place files outside dest. Reading to the end checks the CRC-32.
            with archive.open(member) as src, open(tmp, "wb") as dst:
                while block := src.read(1 << 20):
                    digest.update(block)
                    dst.write(block)
            os.replace(tmp, out)
            traces.append({"id": name, "path": str(out.relative_to(assets)), "bytes": member.file_size,
                           "sha256": digest.hexdigest(), "instructions": xz_instructions(out)})
            log(f"  {name}: {gb(member.file_size)}, {traces[-1]['instructions'] / 1e6:.0f}M instructions")
    return traces, len(members)


def pinned_present(assets):
    if not PIN.is_file():
        return False
    pins = json.loads(PIN.read_text())["traces"]
    return all((assets / t["path"]).is_file() and (assets / t["path"]).stat().st_size == t["bytes"] for t in pins)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep-zip", action="store_true", help="keep CRC2_trace.zip after extracting")
    args = parser.parse_args()

    assets = Path(os.environ.get("AFAR_ASSETS") or sys.exit("traces_fetch_crc2: source env/site.sh first"))
    dest = assets / "traces" / "crc2"
    if pinned_present(assets):
        log(f"all traces in {PIN.relative_to(ROOT)} are present in {dest}; nothing to do")
        return 0
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if not os.access(dest, os.W_OK):
            raise PermissionError
    except PermissionError:
        sys.exit(f"traces_fetch_crc2: {dest} is not writable; set AFAR_ASSETS to a directory you own")

    zip_path = dest / ".CRC2_trace.zip"
    if not zip_path.is_file():
        download(zip_path)
    traces, members = extract(zip_path, dest, assets)
    PIN.write_text(json.dumps({
        "source": "CRC-2 (2nd Cache Replacement Championship, 2017) traces: the Dropbox folder "
                  "linked from ChampSim's README as http://bit.ly/2t2nkUj",
        "url": URL,
        "archive": {"name": "CRC2_trace.zip", "bytes": zip_path.stat().st_size, "members": members},
        "retrieved": str(datetime.date.today()),
        "selection": "the 19 memory-intensive SPEC CPU2006 SimPoints evaluated by ArchAgent (arXiv 2602.22425)",
        "record_bytes": RECORD_BYTES,
        "traces": traces,
    }, indent=2) + "\n")
    log(f"pinned {len(traces)} traces in {PIN.relative_to(ROOT)}")
    if args.keep_zip:
        log(f"zip kept at {zip_path}")
    else:
        zip_path.unlink()
        log("zip deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
