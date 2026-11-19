#!/usr/bin/env python3
"""D2 step 3: baseline vs static priority vs dynamic, under several loads.

Run:  sudo /usr/bin/python3 tests/exp_dynamic.py     (takes ~5 minutes)
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

N, SPACING, WARMUP = 20, 0.5, 2
SUBS = ("h2", "h3", "h4")
RUNS = [("idle", None, "baseline"), ("idle", None, "dynamic"),
        ("moderate", "15M", "baseline"), ("moderate", "15M", "priority"),
        ("moderate", "15M", "dynamic"),
        ("heavy", "50M", "baseline"), ("heavy", "50M", "priority"),
        ("heavy", "50M", "dynamic")]


def pct(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def run(load, rate, mode):
    tag = f"{load}_{mode}"
    print(f"\n--- {tag} ---")
    ryu = lab.start_ryu("emergency_controller_d3.py", mode)
    net, procs, clients = None, [], []
    try:
        net = build()
        net.start()
        if not net.waitConnected(timeout=20):
            raise SystemExit("switches did not connect")
        net.pingAll()
        lab.apply_qos(net)
        h1, h5, h6 = net.get("h1"), net.get("h5"), net.get("h6")
        srv_log = open(f"{lab.LOGDIR}/{tag}_server.log", "w")
        server = h1.popen([sys.executable, "-u", "app/server.py"], cwd=lab.REPO,
                          stdin=subprocess.PIPE, stdout=srv_log,
                          stderr=subprocess.STDOUT)
        procs.append(server)
        for name in SUBS:
            log = open(f"{lab.LOGDIR}/{tag}_{name}.log", "w")
            c = net.get(name).popen(
                [sys.executable, "-u", "app/client.py", "10.0.0.1", name,
                 f"{lab.LOGDIR}/{tag}_{name}.csv"],
                cwd=lab.REPO, stdout=log, stderr=subprocess.STDOUT)
            clients.append(c)
            procs.append(c)
        time.sleep(6)
        if rate:
            procs.append(h6.popen(["iperf", "-s", "-u"], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL))
            time.sleep(1)
            procs.append(h5.popen(["iperf", "-c", "10.0.0.6", "-u", "-b", rate,
                                   "-t", str(int(N * SPACING + 14))],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL))
            time.sleep(3)
        for i in range(1, N + 1):
            server.stdin.write(f"e test {i}\n".encode())
            server.stdin.flush()
            time.sleep(SPACING)
        time.sleep(3)

        lat, got = [], 0
        for name in SUBS:
            try:
                rows = list(csv.DictReader(open(f"{lab.LOGDIR}/{tag}_{name}.csv")))
            except FileNotFoundError:
                rows = []
            em = [r for r in rows if r["level"] == "EMERGENCY"]
            got += len(em)
            lat += [float(r["latency_ms"]) for r in em if int(r["seq"]) > WARMUP]
        res = {"load": load, "mode": mode,
               "avg_ms": sum(lat) / len(lat) if lat else float("nan"),
               "p95_ms": pct(lat, 95) if lat else float("nan"),
               "max_ms": max(lat) if lat else float("nan"),
               "delivered_pct": 100.0 * got / (N * len(SUBS))}
        print("  avg %.2f ms  p95 %.2f ms  max %.2f ms  delivered %.0f%%" % (
            res["avg_ms"], res["p95_ms"], res["max_ms"], res["delivered_pct"]))
        if mode == "dynamic":
            for line in open(f"{lab.LOGDIR}/ryu.log"):
                if "PRIORITY QUEUE" in line or "CONGESTION" in line:
                    print("  controller:", line.strip().split("] ")[-1][:90])
        return res
    finally:
        for c in clients:
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
    results = [run(*r) for r in RUNS]
    os.makedirs(os.path.join(lab.REPO, "results"), exist_ok=True)
    out = os.path.join(lab.REPO, "results", "d2_dynamic.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)
    print("\n" + "=" * 66)
    print("%-9s %-9s %9s %9s %9s %10s" % ("load", "mode", "avg ms", "p95 ms",
                                          "max ms", "delivered"))
    for r in results:
        print("%-9s %-9s %9.2f %9.2f %9.2f %9.0f%%" % (
            r["load"], r["mode"], r["avg_ms"], r["p95_ms"], r["max_ms"],
            r["delivered_pct"]))
    print("=" * 66 + "\nsaved to", out)


if __name__ == "__main__":
    main()
