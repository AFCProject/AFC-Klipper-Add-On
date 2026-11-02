#!/usr/bin/env python3
"""
Summarize a CFS trace (the JSON-lines file written when trace_file is set).

    python3 tools/cfs_trace_summary.py /usr/data/printer_data/logs/cfs_trace.jsonl
    python3 tools/cfs_trace_summary.py cfs_trace.jsonl --since "2026-10-22 19:00"
    python3 tools/cfs_trace_summary.py cfs_trace.jsonl --events load_end,fail

Rotated backups (cfs_trace.jsonl.1, .2, ...) next to the file are read too,
oldest first. Standard library only, so it runs on the printer itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict


def load(path):
    files = []
    i = 1
    while os.path.exists("{}.{}".format(path, i)):
        files.append("{}.{}".format(path, i))
        i += 1
    files = list(reversed(files))
    if os.path.exists(path):
        files.append(path)
    events, bad = [], 0
    for name in files:
        with open(name, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    bad += 1   # a line cut short by a power loss
    events.sort(key=lambda e: e.get("ts", 0))
    return events, files, bad


def when(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = min(len(values) - 1, max(0, int(round(p / 100.0 * (len(values) - 1)))))
    return values[k]


def stats(values):
    if not values:
        return "n/a"
    return "min {:.1f}  avg {:.1f}  max {:.1f}".format(
        min(values), sum(values) / len(values), max(values))


def summarize(events, out=sys.stdout):
    def p(*args):
        print(*args, file=out)

    if not events:
        p("No events.")
        return
    p("Span: {} -> {}  ({} events)".format(when(events[0]["ts"]), when(events[-1]["ts"]),
                                            len(events)))
    kinds = Counter(e["ev"] for e in events)

    starts = [e for e in events if e["ev"] in ("trace_start", "online")]
    versions = {e.get("version") for e in events if e["ev"] == "online" and e.get("version")}
    if versions:
        p("Box firmware: {}".format(", ".join(sorted(versions))))
    settings = [e["settings"] for e in starts if e["ev"] == "trace_start" and e.get("settings")]
    if settings:
        s = settings[-1]
        p("Settings: stage7_arg={stage7_arg} load_attempts={load_attempts} "
          "stage5_timeout={stage5_timeout} stage5_stall_time={stage5_stall_time}".format(**{
              k: s.get(k) for k in ("stage7_arg", "load_attempts", "stage5_timeout",
                                    "stage5_stall_time")}))

    prints = [e for e in events if e["ev"] == "print_state"]
    if prints:
        p("\nPrint jobs:")
        for e in prints:
            p("  {}  {:<10} {}".format(when(e["ts"]), e.get("state") or "", e.get("filename") or ""))

    # Loads
    loads = [e for e in events if e["ev"] == "load_end"]
    p("\nLoads: {} ok, {} failed".format(sum(1 for e in loads if e.get("ok")),
                                        sum(1 for e in loads if not e.get("ok"))))
    if loads:
        ok_secs = [e["seconds"] for e in loads if e.get("ok") and "seconds" in e]
        p("  duration (s):     {}".format(stats(ok_secs)))
        retried = [e for e in loads if e.get("ok") and e.get("attempts", 1) > 1]
        if retried:
            p("  needed a retry:   {}".format(len(retried)))
        by_slot = defaultdict(lambda: [0, 0])
        for e in loads:
            by_slot[e.get("slot", "?")][0 if e.get("ok") else 1] += 1
        p("  per slot:         " + "  ".join("{} {}/{}".format(k, v[0], v[0] + v[1])
                                             for k, v in sorted(by_slot.items())))
    s5 = [e for e in events if e["ev"] == "stage5_result"]
    if s5:
        reached = [e for e in s5 if e.get("reached")]
        p("  stage 5 (spool->toolhead sensor): {} reached, {} not".format(
            len(reached), len(s5) - len(reached)))
        p("    seconds:        {}".format(stats([e["seconds"] for e in reached if "seconds" in e])))
        p("    polls:          {}".format(stats([e["polls"] for e in reached if e.get("polls")])))
        wheels = [abs(e["wheel"]) for e in reached if isinstance(e.get("wheel"), (int, float))]
        if wheels:
            p("    wheel reading:  {}  (mm, box odometer at sensor)".format(stats(wheels)))
        timeouts = [e for e in s5 if not e.get("reached")]
        for e in timeouts:
            p("    ! {} {} attempt {}: {}".format(when(e["ts"]), e.get("lane"),
                                                  e.get("attempt"), e.get("detail")))
    handoffs = [e for e in events if e["ev"] == "handoff"]
    if handoffs:
        st7 = Counter("{} (arg {})".format(e.get("stage7"), e.get("stage7_arg")) for e in handoffs)
        p("  stage 7 replies:  " + ", ".join("{} x{}".format(k, v) for k, v in st7.most_common()))

    # Unloads
    unloads = [e for e in events if e["ev"] == "unload_end"]
    p("\nUnloads: {} ok, {} failed".format(sum(1 for e in unloads if e.get("ok")),
                                          sum(1 for e in unloads if not e.get("ok"))))
    if unloads:
        p("  duration (s):     {}".format(stats([e["seconds"] for e in unloads
                                                  if e.get("ok") and "seconds" in e])))
        details = Counter(e.get("detail") for e in unloads if e.get("ok"))
        for d, n in details.most_common(3):
            p("  {:>3} x {}".format(n, d))
    cuts = [e for e in events if e["ev"] == "cut"]
    if cuts:
        unreleased = sum(1 for e in cuts if e.get("verified") and not e.get("released"))
        p("  cuts: {}{}".format(len(cuts), ", {} where the blade did not release".format(
            unreleased) if unreleased else ""))

    # Link health
    tx = [e for e in events if e["ev"] == "tx"]
    rx = [e for e in events if e["ev"] == "rx"]
    p("\nLink: {} sent, {} replies, {} timeouts, {} crc errors, {} unsolicited".format(
        len(tx), len(rx), kinds["timeout"],
        max([e.get("total", 0) for e in events if e["ev"] == "crc_error"] or [0]),
        kinds["rx_unsolicited"]))
    lat = defaultdict(list)
    for e in rx:
        if "ms" in e:
            lat[e["fn"]].append(e["ms"])
    if lat:
        p("  reply latency (ms)              n     p50     p95     max")
        for fn, values in sorted(lat.items(), key=lambda kv: -len(kv[1])):
            p("  {:<30}{:>4} {:>7.1f} {:>7.1f} {:>7.1f}".format(
                fn, len(values), pct(values, 50), pct(values, 95), max(values)))
    errors = Counter((e["fn"], e["st"]) for e in rx if e.get("st") not in ("OK", None))
    if errors:
        p("  non-OK replies:")
        for (fn, st), n in errors.most_common():
            p("    {:<28} {:<14} x{}".format(fn, st, n))
    tmo = Counter(e["fn"] for e in events if e["ev"] == "timeout")
    if tmo:
        p("  timeouts by command: " + ", ".join("{} x{}".format(k, v) for k, v in tmo.most_common()))
    offline = [e for e in events if e["ev"] in ("offline", "port_error")]
    for e in offline:
        p("  ! {} {}: {}".format(when(e["ts"]), e["ev"], e.get("reason") or e.get("error")))

    # Problems and notes, in time order
    notable = [e for e in events if e["ev"] in ("fail", "runout", "load_attempt_failed", "mark",
                                                "trace_pause", "trace_resume")]
    if notable:
        p("\nTimeline:")
        for e in notable:
            text = e.get("message") or e.get("detail") or e.get("text") or ""
            p("  {}  {:<20} {:<6} {}".format(when(e["ts"]), e["ev"], e.get("lane", ""), text))
    presence = [e for e in events if e["ev"] == "presence"]
    if presence:
        p("\nSpool presence changes: {}".format(len(presence)))
    if kinds.get("trace_start", 0) > 1:
        p("Klipper restarts in this trace: {}".format(kinds["trace_start"] - 1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("trace", help="path to the trace file")
    ap.add_argument("--since", help='only events after "YYYY-MM-DD HH:MM", or after the '
                    'last AFC_CFS_TRACE MARK containing this text')
    ap.add_argument("--events", help="print these event types raw, comma separated")
    args = ap.parse_args(argv)

    events, files, bad = load(args.trace)
    if not files:
        ap.error("no trace at {}".format(args.trace))
    if args.since:
        try:
            cutoff = dt.datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp()
        except ValueError:
            marks = [e for e in events if e["ev"] == "mark"
                     and args.since.lower() in str(e.get("text", "")).lower()]
            if not marks:
                ap.error('--since: not a "YYYY-MM-DD HH:MM" time and no mark contains "{}"'
                         .format(args.since))
            cutoff = marks[-1].get("ts", 0)
        events = [e for e in events if e.get("ts", 0) >= cutoff]
    if args.events:
        wanted = set(args.events.split(","))
        for e in events:
            if e["ev"] in wanted:
                print(json.dumps(e))
        return 0
    print("Files: {}{}".format(", ".join(files), "  ({} unreadable lines)".format(bad) if bad else ""))
    summarize(events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
