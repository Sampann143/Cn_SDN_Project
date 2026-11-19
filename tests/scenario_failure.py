#!/usr/bin/env python3
"""D2 challenging scenario: heavy congestion + one dead subscriber (dynamic mode).

Run:  sudo /usr/bin/python3 tests/scenario_failure.py
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

N, WARMUP, DEAD = 8, 2, "h3"
SUBS = ("h2", "h3", "h4")


def main():
    if os.geteuid() != 0:
        sys.exit("Run with sudo.")
    setLogLevel("warning")
    ryu = lab.start_ryu("emergency_controller_d3.py", "dynamic")
    net, procs, clients = None, [], {}
    try:
        net = build()
        net.start()
        if not net.waitConnected(timeout=20):
            raise SystemExit("switches did not connect")
        net.pingAll()
        lab.apply_qos(net)
        h1, h5, h6 = net.get("h1"), net.get("h5"), net.get("h6")

        srv_log = open(f"{lab.LOGDIR}/scn_server.log", "w")
        server = h1.popen([sys.executable, "-u", "app/server.py"], cwd=lab.REPO,
                          stdin=subprocess.PIPE, stdout=srv_log,
                          stderr=subprocess.STDOUT)
        procs.append(server)
        for name in SUBS:
            log = open(f"{lab.LOGDIR}/scn_{name}.log", "w")
            c = net.get(name).popen(
                [sys.executable, "-u", "app/client.py", "10.0.0.1", name,
                 f"{lab.LOGDIR}/scn_{name}.csv"],
                cwd=lab.REPO, stdout=log, stderr=subprocess.STDOUT)
            clients[name] = c
            procs.append(c)
        time.sleep(6)

        print("1) heavy UDP flood h5 -> h6 (50 Mbit/s into a 10 Mbit/s link)")
        procs.append(h6.popen(["iperf", "-s", "-u"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL))
        time.sleep(1)
        procs.append(h5.popen(["iperf", "-c", "10.0.0.6", "-u", "-b", "50M",
                               "-t", str(N * 3 + 20)],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL))
        time.sleep(3)

        print(f"2) subscriber {DEAD} dies (process frozen)")
        os.kill(clients[DEAD].pid, signal.SIGSTOP)

        print(f"3) server sends {N} emergencies "
              f"(each waits ~2 s for the dead subscriber's ACK)...")
        for i in range(1, N + 1):
            server.stdin.write(f"e alert {i}\n".encode())
            server.stdin.flush()
            time.sleep(0.3)
        time.sleep(N * 2.2 + 3)

        print("\n--- server report ---")
        lines = [l.strip().lstrip("> ") for l in open(f"{lab.LOGDIR}/scn_server.log")
                 if "EMERGENCY seq=" in l]
        for l in lines:
            print("  " + l)

        print("\n--- delivery and latency per subscriber ---")
        for name in SUBS:
            try:
                rows = list(csv.DictReader(
                    open(f"{lab.LOGDIR}/scn_{name}.csv")))
            except FileNotFoundError:
                rows = []
            em = [r for r in rows if r["level"] == "EMERGENCY"]
            lat = [float(r["latency_ms"]) for r in em if int(r["seq"]) > WARMUP]
            avg = "%.2f ms" % (sum(lat) / len(lat)) if lat else "n/a"
            mx = "%.2f ms" % max(lat) if lat else "n/a"
            tag = "  (dead)" if name == DEAD else ""
            print("  %s: received %d/%d, avg %s, max %s%s"
                  % (name, len(em), N, avg, mx, tag))

        print("\n--- controller ---")
        for l in open(f"{lab.LOGDIR}/ryu.log"):
            if "CONGESTION" in l or "PRIORITY QUEUE" in l:
                print("  " + l.strip().split("] ")[-1][:90])
    finally:
        try:
            os.kill(clients[DEAD].pid, signal.SIGCONT)
        except Exception:
            pass
        for c in clients.values():
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


if __name__ == "__main__":
    main()
