#!/usr/bin/env python3
"""One-command demo: Ryu + Mininet + server + 3 clients + interactive menu.

Run from the repo folder:   sudo /usr/bin/python3 demo_app.py
"""
import csv
import os
import signal
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, "topology"))
LOGDIR = "/tmp/emergency_demo"
HOSTS = ("h2", "h3", "h4")


class Tail:
    """Returns only the text added to a file since the last call."""
    def __init__(self, path):
        self.path, self.pos = path, 0

    def new(self):
        try:
            with open(self.path) as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
            return data
        except FileNotFoundError:
            return ""


def clean(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        while line.startswith(">"):
            line = line[1:].strip()
        if line:
            out.append(line)
    return out


def find_ryu():
    user = os.environ.get("SUDO_USER") or "root"
    home = os.path.expanduser(f"~{user}")
    path = os.path.join(home, "ryu-env", "bin", "ryu-manager")
    return path if os.path.exists(path) else None


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.3)
        finally:
            s.close()
    return False


def title(text):
    print("\n" + "=" * 68 + f"\n{text}\n" + "=" * 68)


def main():
    if os.geteuid() != 0:
        print("Run with sudo:  sudo /usr/bin/python3 demo_app.py")
        sys.exit(1)

    from mininet.log import setLogLevel
    from topo import build

    ryu_bin = find_ryu()
    if not ryu_bin:
        print("Could not find ~/ryu-env/bin/ryu-manager. Is the Ryu venv set up?")
        sys.exit(1)

    os.makedirs(LOGDIR, exist_ok=True)
    setLogLevel("warning")

    title("Starting up (cleanup, controller, network)")
    print("1/5 cleaning old Mininet state (this also stops any running Ryu)...")
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)

    print("2/5 starting the Ryu controller...")
    ryu_log = open(f"{LOGDIR}/ryu.log", "w")
    ryu = subprocess.Popen(
        [ryu_bin, "--ofp-tcp-listen-port", "6653",
         os.path.join(REPO, "controller", "emergency_controller.py")],
        cwd=REPO, stdout=ryu_log, stderr=subprocess.STDOUT)
    if not wait_port(6653) or ryu.poll() is not None:
        print("Ryu failed to start. Its log:")
        print(open(f"{LOGDIR}/ryu.log").read()[-1500:])
        ryu.terminate()
        sys.exit(1)

    net = server = None
    clients = {}
    frozen = set()
    try:
        print("3/5 starting Mininet (2 switches, 6 hosts)...")
        net = build()
        net.start()
        if not net.waitConnected(timeout=20):
            print("Switches did not connect to Ryu.")
            return
        loss = net.pingAll()
        print(f"    pingall packet loss: {loss}%")

        print("4/5 starting the server on h1 and clients on h2, h3, h4...")
        h1 = net.get("h1")
        server_log = open(f"{LOGDIR}/server.log", "w")
        server = h1.popen([sys.executable, "-u", "app/server.py"], cwd=REPO,
                          stdin=subprocess.PIPE, stdout=server_log,
                          stderr=subprocess.STDOUT)
        time.sleep(1)
        for name in HOSTS:
            log = open(f"{LOGDIR}/{name}.log", "w")
            clients[name] = net.get(name).popen(
                [sys.executable, "-u", "app/client.py", "10.0.0.1", name,
                 f"{LOGDIR}/{name}.csv"],
                cwd=REPO, stdout=log, stderr=subprocess.STDOUT)

        print("5/5 waiting for subscribers to register...")
        time.sleep(6)

        server_tail = Tail(f"{LOGDIR}/server.log")
        ryu_tail = Tail(f"{LOGDIR}/ryu.log")
        client_tails = {n: Tail(f"{LOGDIR}/{n}.log") for n in HOSTS}
        ryu_tail.new()          # skip startup noise

        def to_server(line, marker, timeout=5):
            server.stdin.write((line + "\n").encode())
            server.stdin.flush()
            text, end = "", time.time() + timeout
            while time.time() < end:
                time.sleep(0.2)
                text += server_tail.new()
                if marker in text:
                    break
            return text

        def report(server_text):
            print("\n--- server (h1) ---")
            for l in clean(server_text):
                print("  " + l)
            time.sleep(0.4)
            for n in HOSTS:
                lines = clean(client_tails[n].new())
                if lines:
                    print(f"--- subscriber {n} ---")
                    for l in lines:
                        print("  " + l)
                elif n in frozen:
                    print(f"--- subscriber {n} --- (frozen, received nothing)")
            cl = [l for l in clean(ryu_tail.new()) if "CLASSIFIED" in l]
            print("--- controller (Ryu) ---")
            if cl:
                for l in cl:
                    print("  " + l)
            else:
                print("  (no CLASSIFIED lines: not emergency traffic, "
                      "or flows already installed)")

        def show_subscribers():
            report_text = to_server("list", "{")
            print("\nRegistered subscribers (ip -> name):")
            for l in clean(report_text):
                print("  " + l)

        def show_flows():
            for sw in ("s1", "s2"):
                out = subprocess.run(
                    ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", sw],
                    capture_output=True, text=True).stdout
                print(f"\n--- {sw}: priority 100 (traps) and 200 (emergency flows) ---")
                found = False
                for line in out.splitlines():
                    if "priority=200" in line or "priority=100" in line:
                        print("  " + line.strip().replace("cookie=0x0, ", ""))
                        found = True
                if not found:
                    print("  (none; emergency flows expire after 30 s idle, "
                          "send an emergency first)")

        def latency_summary():
            print("\nLatency per subscriber (from the client CSV logs):")
            for n in HOSTS:
                try:
                    rows = list(csv.DictReader(open(f"{LOGDIR}/{n}.csv")))
                except FileNotFoundError:
                    rows = []
                for level in ("EMERGENCY", "ROUTINE"):
                    vals = [float(r["latency_ms"]) for r in rows
                            if r["level"] == level]
                    if vals:
                        print(f"  {n} {level:9s}: n={len(vals)}  "
                              f"avg={sum(vals)/len(vals):.2f} ms  "
                              f"max={max(vals):.2f} ms")

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
                print(f"{name} frozen (simulates a dead subscriber). "
                      "Send an emergency to see retransmissions and the "
                      "missing-ACK report.")

        menu = """
  1) Send EMERGENCY message
  2) Send ROUTINE message
  3) Show registered subscribers
  4) Show emergency flow rules in the switches
  5) Show latency summary
  6) Freeze / unfreeze a subscriber (simulate a failure)
  7) Run pingall
  q) Quit"""

        title("READY. All components are running.")
        while True:
            print(menu)
            choice = input("\nchoice> ").strip().lower()
            if choice == "1":
                text = input("message [evacuate zone B]> ").strip() \
                    or "evacuate zone B"
                report(to_server("e " + text, "EMERGENCY seq=", timeout=6))
            elif choice == "2":
                text = input("message [lunch at 1pm]> ").strip() \
                    or "lunch at 1pm"
                report(to_server("r " + text, "sent ROUTINE"))
            elif choice == "3":
                show_subscribers()
            elif choice == "4":
                show_flows()
            elif choice == "5":
                latency_summary()
            elif choice == "6":
                toggle(input("which subscriber (h2/h3/h4)> ").strip())
            elif choice == "7":
                print(f"packet loss: {net.pingAll()}%")
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
        for p in list(clients.values()) + ([server] if server else []):
            if p.poll() is None:
                p.terminate()
        if net:
            net.stop()
        ryu.terminate()
        print(f"Done. Logs and latency CSVs are in {LOGDIR}")


if __name__ == "__main__":
    main()
