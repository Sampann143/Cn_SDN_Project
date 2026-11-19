"""Helpers shared by the D2 experiments: start Ryu, apply the bottleneck QoS."""
import os
import socket
import subprocess
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = "/tmp/emergency_demo"
RATE = 10_000_000          # bottleneck capacity in bit/s


def find_ryu():
    user = os.environ.get("SUDO_USER") or "root"
    path = os.path.join(os.path.expanduser(f"~{user}"), "ryu-env", "bin",
                        "ryu-manager")
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


def start_ryu(app="emergency_controller.py", mode="priority"):
    """Clean old state (this kills any running Ryu), then start Ryu."""
    os.makedirs(LOGDIR, exist_ok=True)
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    ryu_bin = find_ryu()
    if not ryu_bin:
        raise SystemExit("Could not find ~/ryu-env/bin/ryu-manager")
    log = open(f"{LOGDIR}/ryu.log", "w")
    proc = subprocess.Popen(
        [ryu_bin, "--ofp-tcp-listen-port", "6653",
         os.path.join(REPO, "controller", app)],
        cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
        env=dict(os.environ, EMERGENCY_MODE=mode))
    if not wait_port(6653) or proc.poll() is not None:
        print(open(f"{LOGDIR}/ryu.log").read()[-1500:])
        raise SystemExit("Ryu failed to start")
    return proc


def uplink_port(net):
    """Name of the s1 interface that faces s2 (the bottleneck port)."""
    return net.get("s1").connectionsTo(net.get("s2"))[0][0].name


def apply_qos(net, rate=RATE):
    """Cap the s1->s2 port at `rate` and create two HTB queues:
       queue 0 = default traffic, queue 1 = emergency (higher priority)."""
    port = uplink_port(net)
    cmd = ["ovs-vsctl", "--", "set", "port", port, "qos=@q", "--",
           "--id=@q", "create", "qos", "type=linux-htb",
           f"other-config:max-rate={rate}",
           "queues:0=@q0", "queues:1=@q1", "--",
           "--id=@q0", "create", "queue",
           "other-config:min-rate=1000000",
           f"other-config:max-rate={rate}", "other-config:priority=1", "--",
           "--id=@q1", "create", "queue",
           "other-config:min-rate=2000000",
           f"other-config:max-rate={rate}", "other-config:priority=0"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("QoS setup failed:\n" + r.stderr)
    return port
