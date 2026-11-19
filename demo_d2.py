#!/usr/bin/env python3
"""Interactive D2 demo: dynamic SDN under congestion.

Run from the repo folder:   sudo /usr/bin/python3 demo_d2.py
"""
import csv
import os
import re
import signal
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, "tests"))
sys.path.insert(0, os.path.join(REPO, "topology"))

HOSTS = ("h2", "h3", "h4")
CTRL_SOCK = "/tmp/emergency_ctrl.sock"
QNAME = {"1": "queue 0 (default traffic)", "2": "queue 1 (emergency)"}


def title(text):
    print("\n" + "=" * 68 + f"\n{text}\n" + "=" * 68)


def pctl(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def main():
    if os.geteuid() != 0:
        print("Run with sudo:  sudo /usr/bin/python3 demo_d2.py")
        sys.exit(1)

    import lab
    from topo import build
    from mininet.log import setLogLevel
    setLogLevel("warning")
    L = lab.LOGDIR
    os.makedirs(L, exist_ok=True)

    title("Starting up (cleanup, controller, network, queues)")
    print("1/5 starting the Ryu controller (dynamic mode)...")
    ryu = lab.start_ryu("emergency_controller_d3.py", "dynamic")

    net = server = iperf_srv = None
    clients, frozen = {}, set()
    st = {"mode": "dynamic", "flood": None, "rate": None, "prev": {}}
    msock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    try:
        print("2/5 starting Mininet (2 switches, 6 hosts)...")
        net = build()
        net.start()
        if not net.waitConnected(timeout=20):
            print("Switches did not connect to Ryu.")
            return
        print(f"    pingall packet loss: {net.pingAll()}%")

        print("3/5 applying the 10 Mbit/s bottleneck with two queues...")
        port = lab.apply_qos(net)
        h1, h5, h6 = net.get("h1"), net.get("h5"), net.get("h6")
        iperf_srv = h6.popen(["iperf", "-s", "-u"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

        print("4/5 starting the server (h1) and subscribers (h2, h3, h4)...")
        srv_log = open(f"{L}/d2demo_server.log", "w")
        server = h1.popen([sys.executable, "-u", "app/server.py"], cwd=REPO,
                          stdin=subprocess.PIPE, stdout=srv_log,
                          stderr=subprocess.STDOUT)
        time.sleep(1)
        for name in HOSTS:
            log = open(f"{L}/d2demo_{name}.log", "w")
            clients[name] = net.get(name).popen(
                [sys.executable, "-u", "app/client.py", "10.0.0.1", name,
                 f"{L}/d2demo_{name}.csv"],
                cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
        print("5/5 waiting for registration and warming up the flows...")
        time.sleep(6)

        def send(line):
            server.stdin.write((line + "\n").encode())
            server.stdin.flush()

        send("e warmup")
        time.sleep(2)

        def server_count():
            try:
                return sum("EMERGENCY seq=" in l
                           for l in open(f"{L}/d2demo_server.log"))
            except FileNotFoundError:
                return 0

        def read_rows(name):
            try:
                return list(csv.DictReader(open(f"{L}/d2demo_{name}.csv")))
            except FileNotFoundError:
                return []

        def events(n=10):
            keys = ("CONGESTION", "PRIORITY QUEUE", "MODE set")
            try:
                lines = [l for l in open(f"{L}/ryu.log")
                         if any(k in l for k in keys)]
            except FileNotFoundError:
                lines = []
            return [l.strip().split("] ")[-1][:100] for l in lines[-n:]]

        def burst(n=10):
            start = {h: len(read_rows(h)) for h in HOSTS}
            base = server_count()
            for i in range(1, n + 1):
                send(f"e demo {i}")
                time.sleep(0.3)
            limit = n * (3.2 if frozen else 1.2) + 6
            end = time.time() + limit
            while time.time() < end and server_count() - base < n:
                time.sleep(0.3)
            time.sleep(1)
            new = []
            for h in HOSTS:
                new += [r for r in read_rows(h)[start[h]:]
                        if r["level"] == "EMERGENCY"]
            lat = []
            if new:
                first = min(int(r["seq"]) for r in new)
                lat = [float(r["latency_ms"]) for r in new
                       if int(r["seq"]) > first]
            nan = float("nan")
            return {"avg": sum(lat) / len(lat) if lat else nan,
                    "p95": pctl(lat, 95) if lat else nan,
                    "max": max(lat) if lat else nan,
                    "delivered": 100.0 * len(new) / (n * len(HOSTS))}

        def show_result(label, r):
            print(f"  {label}: avg {r['avg']:.2f} ms   p95 {r['p95']:.2f} ms   "
                  f"max {r['max']:.2f} ms   delivered {r['delivered']:.0f}%")

        def set_mode(m):
            try:
                msock.sendto(f"MODE {m}".encode(), CTRL_SOCK)
            except OSError:
                print("could not reach the controller socket")
                return
            st["mode"] = m
            time.sleep(1.2)
            print(f"controller mode -> {m}")
            for e in events(2):
                print("  controller:", e)

        def start_flood(rate):
            st["flood"] = h5.popen(["iperf", "-c", "10.0.0.6", "-u", "-b", rate,
                                    "-t", "900"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            st["rate"] = rate
            print(f"UDP flood h5 -> h6 started at {rate} (link capacity 10M)")

        def stop_flood():
            if st["flood"]:
                st["flood"].terminate()
                st["flood"].wait()
            st["flood"] = None
            print("flood stopped")

        def queue_counters():
            out = subprocess.run(["tc", "-s", "class", "show", "dev", port],
                                 capture_output=True, text=True).stdout
            cur, res = None, {}
            for line in out.splitlines():
                m = re.match(r"class htb 1:(\w+)", line)
                if m:
                    cur = m.group(1)
                m = re.search(r"Sent (\d+) bytes (\d+) pkt", line)
                if m and cur:
                    res[cur] = (int(m.group(1)), int(m.group(2)))
            print(f"\nPackets sent through the bottleneck port ({port}):")
            for k in ("1", "2"):
                if k in res:
                    b, p = res[k]
                    pb, pp = st["prev"].get(k, (0, 0))
                    print(f"  {QNAME[k]:28s} total {p:7d} pkts ({b/1e6:6.2f} MB)"
                          f"   since last view +{p - pp} pkts")
            st["prev"] = res

        def toggle(name):
            if name not in clients:
                print("Pick one of:", ", ".join(HOSTS))
                return
            if name in frozen:
                os.kill(clients[name].pid, signal.SIGCONT)
                frozen.discard(name)
                print(f"{name} resumed.")
            else:
                os.kill(clients[name].pid, signal.SIGSTOP)
                frozen.add(name)
                print(f"{name} frozen (simulated failure). Bursts get slower "
                      "because the server retransmits to it.")

        def compare():
            title("GUIDED COMPARISON: same flood, controller off vs dynamic")
            if st["flood"] is None:
                start_flood("50M")
            print("waiting 4 s for the bottleneck queue to fill...")
            time.sleep(4)
            res = {}
            for m in ("baseline", "dynamic"):
                set_mode(m)
                print(f"sending 10 emergencies in {m} mode...")
                res[m] = burst(10)
                show_result(m, res[m])
            title("RESULT (emergency latency during the flood)")
            for m in ("baseline", "dynamic"):
                show_result(f"{m:9s}", res[m])
            if res["dynamic"]["avg"] > 0:
                print("\n  speed-up: about %.0fx lower latency with the "
                      "dynamic controller" % (res["baseline"]["avg"]
                                              / res["dynamic"]["avg"]))
            print("\nController events:")
            for e in events(6):
                print("  ", e)
            stop_flood()

        menu = """
  1) Send 10 emergencies and measure latency
  2) Start / stop the UDP flood (congestion)
  3) Change controller mode (baseline / priority / dynamic)
  4) Show queue counters on the bottleneck port
  5) Show controller events (congestion, priority queue, mode)
  6) Freeze / unfreeze a subscriber (simulate a failure)
  7) GUIDED COMPARISON (baseline vs dynamic under flood)
  q) Quit"""

        title("READY. Bottleneck: s1-s2 link at 10 Mbit/s with two queues.")
        while True:
            print(f"\n[mode: {st['mode']} | flood: "
                  f"{('ON ' + st['rate']) if st['flood'] else 'off'} | "
                  f"frozen: {', '.join(sorted(frozen)) or 'none'}]")
            print(menu)
            choice = input("\nchoice> ").strip().lower()
            if choice == "1":
                show_result("this burst", burst(10))
            elif choice == "2":
                if st["flood"]:
                    stop_flood()
                else:
                    rate = input("flood rate [50M]> ").strip() or "50M"
                    start_flood(rate)
            elif choice == "3":
                m = input("mode (baseline/priority/dynamic)> ").strip().lower()
                if m in ("baseline", "priority", "dynamic"):
                    set_mode(m)
                else:
                    print("unknown mode")
            elif choice == "4":
                queue_counters()
            elif choice == "5":
                print("\nController events:")
                for e in events(10) or ["(none yet)"]:
                    print("  ", e)
            elif choice == "6":
                toggle(input("which subscriber (h2/h3/h4)> ").strip())
            elif choice == "7":
                compare()
            elif choice in ("q", "quit", "exit"):
                break
            else:
                print("unknown choice")

    except (KeyboardInterrupt, EOFError):
        print("\ninterrupted")
    finally:
        title("Shutting down")
        for name in list(frozen):
            try:
                os.kill(clients[name].pid, signal.SIGCONT)
            except Exception:
                pass
        for p in clients.values():
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass
        time.sleep(1)
        extra = [st["flood"], iperf_srv, server]
        for p in list(clients.values()) + [x for x in extra if x]:
            if p.poll() is None:
                p.terminate()
        if net:
            net.stop()
        ryu.terminate()
        print("Done.")


if __name__ == "__main__":
    main()
