#!/usr/bin/env python3
"""Step 1 of D2: prove the bottleneck exists.

Run:  sudo /usr/bin/python3 tests/bottleneck_test.py
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "topology"))

import lab
from topo import build
from mininet.log import setLogLevel


def ping(src, ip, count=5):
    out = src.cmd(f"ping -c {count} -i 0.2 -W 2 {ip}")
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/", out)
    return (loss.group(1) + "%" if loss else "?",
            f"min/avg/max = {'/'.join(rtt.groups())} ms" if rtt else "no replies")


def main():
    if os.geteuid() != 0:
        sys.exit("Run with sudo.")
    setLogLevel("warning")
    ryu = lab.start_ryu()
    net = None
    try:
        net = build()
        net.start()
        if not net.waitConnected(timeout=20):
            sys.exit("Switches did not connect to Ryu.")
        print("pingall loss:", net.pingAll(), "%")

        port = lab.apply_qos(net)
        print(f"\nQoS applied on {port}: 10 Mbit/s cap, queues 0 (default) and 1 (emergency)")
        print(subprocess.run(["tc", "class", "show", "dev", port],
                             capture_output=True, text=True).stdout)

        h1, h2, h5, h6 = (net.get(n) for n in ("h1", "h2", "h5", "h6"))

        print("1) Ping h1 -> h2 with an idle network:")
        print("   loss %s, %s" % ping(h1, "10.0.0.2"))

        print("\n2) Flooding h5 -> h6 with 50 Mbit/s of UDP for 8 s "
              "(the link only carries 10)...")
        srv_out = open(f"{lab.LOGDIR}/iperf_server.txt", "w")
        server = h6.popen(["iperf", "-s", "-u", "-i", "1"], stdout=srv_out,
                          stderr=subprocess.STDOUT)
        time.sleep(1)
        flood = h5.popen(["iperf", "-c", "10.0.0.6", "-u", "-b", "50M",
                          "-t", "8"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        time.sleep(3)
        print("   Ping h1 -> h2 while the link is congested:")
        print("   loss %s, %s" % ping(h1, "10.0.0.2"))
        flood.wait()
        time.sleep(2)
        server.terminate()
        srv_out.close()

        print("\n3) What the iperf receiver (h6) actually got:")
        lines = [l for l in open(f"{lab.LOGDIR}/iperf_server.txt")
                 if "Mbits/sec" in l]
        for l in lines[-2:]:
            print("   " + l.strip())
        print("\nExpected: about 9-10 Mbits/sec received and large loss; "
              "ping delay far above the idle value.")
    finally:
        if net:
            net.stop()
        ryu.terminate()


if __name__ == "__main__":
    main()
