#!/usr/bin/env python3
"""D2 step 2: emergency latency idle vs flood, baseline vs priority queue.

Run:  sudo /usr/bin/python3 tests/exp_priority.py
"""
import csv
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "topology"))

import lab
from topo import build
from mininet.log import setLogLevel

N = 20              # emergencies per run
SPACING = 0.5       # seconds between them
WARMUP = 2          # first seqs skipped in latency stats (controller detour)
SUBS = ("h2", "h3", "h4")


def pct(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def run(tag, mode, flood):
    print(f"\n--- run: {tag} (mode={mode}, flood={flood}) ---")
    ryu = lab.start_ryu("emergency_controller_d2.py", mode)
    net, procs = None, []
    try:
        net = build()
        net.start()
        if not net.waitConnected(timeout=20):
            raise SystemExit("switches did not connect")
        net.pingAll()
        port = lab.apply_qos(net)
        h1, h5, h6 = net.get("h1"), net.get("h5"), net.get("h6")

        srv_log = open(f"{lab.LOGDIR}/{tag}_server.log", "w")
        server = h1.popen([sys.executable, "-u", "app/server.py"],
                          cwd=lab.REPO, stdin=subprocess.PIPE,
                          stdout=srv_log, stderr=subprocess.STDOUT)
        procs.append(server)
        clients = []
        for name in SUBS:
            log = open(f"{lab.LOGDIR}/{tag}_{name}.log", "w")
            c = net.get(name).popen(
                [sys.executable, "-u", "app/client.py", "10.0.0.1", name,
                 f"{lab.LOGDIR}/{tag}_{name}.csv"],
                cwd=lab.REPO, stdout=log, stderr=subprocess.STDOUT)
            clients.append(c)
            procs.append(c)
        time.sleep(6)                       # registration

        if flood:
            procs.append(h6.popen(["iperf", "-s", "-u"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL))
            time.sleep(1)
            dur = int(N * SPACING + 12)
            procs.append(h5.popen(["iperf", "-c", "10.0.0.6", "-u", "-b",
                                   "50M", "-t", str(dur)],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL))
            time.sleep(3)                   # let the queue fill

        for i in range(1, N + 1):
            server.stdin.write(f"e test {i}\n".encode())
            server.stdin.flush()
            time.sleep(SPACING)
        time.sleep(3)

        if flood:
            out = subprocess.run(["tc", "-s", "class", "show", "dev", port],
                                 capture_output=True, text=True).stdout
            print("  tc class counters on", port)
            for line in out.splitlines():
                if line.startswith("class htb") or "Sent" in line:
                    print("   ", line.strip())

        lat, got = [], 0
        for name in SUBS:
            try:
                rows = list(csv.DictReader(
                    open(f"{lab.LOGDIR}/{tag}_{name}.csv")))
            except FileNotFoundError:
                rows = []
            em = [r for r in rows if r["level"] == "EMERGENCY"]
            got += len(em)
            lat += [float(r["latency_ms"]) for r in em
                    if int(r["seq"]) > WARMUP]
        res = {"condition": tag, "mode": mode,
               "avg_ms": sum(lat) / len(lat) if lat else float("nan"),
               "p95_ms": pct(lat, 95) if lat else float("nan"),
               "max_ms": max(lat) if lat else float("nan"),
               "delivered_pct": 100.0 * got / (N * len(SUBS))}
        print("  avg %.2f ms  p95 %.2f ms  max %.2f ms  delivered %.0f%%" % (
            res["avg_ms"], res["p95_ms"], res["max_ms"],
            res["delivered_pct"]))
        return res
    finally:
        for c in procs[1:4]:
            try:
                c.send_signal(signal.SIGINT)
            except Exception:
                pass
        time.sleep(1)
        for p in procs:
            if p.poll() is None:
                p.terminate()
        if net:
            net.stop()
        ryu.terminate()
        time.sleep(1)


def main():
    if os.geteuid() != 0:
        sys.exit("Run with sudo.")
    setLogLevel("warning")
    results = [run("idle", "baseline", False),
               run("flood", "baseline", True),
               run("flood", "priority", True)]

    os.makedirs(os.path.join(lab.REPO, "results"), exist_ok=True)
    out = os.path.join(lab.REPO, "results", "d2_priority.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)

    print("\n" + "=" * 66)
    print("%-8s %-9s %9s %9s %9s %10s" % ("load", "mode", "avg ms", "p95 ms",
                                          "max ms", "delivered"))
    for r in results:
        print("%-8s %-9s %9.2f %9.2f %9.2f %9.0f%%" % (
            r["condition"], r["mode"], r["avg_ms"], r["p95_ms"],
            r["max_ms"], r["delivered_pct"]))
    print("=" * 66)
    print("saved to", out)


if __name__ == "__main__":
    main()
