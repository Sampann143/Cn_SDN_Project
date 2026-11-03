import csv
import json
import os
import select
import socket
import sys
import threading
import time

SERVER_REG_PORT = 5000
SERVER_ACK_PORT = 5006
LISTEN_PORTS = {5005: "EMERGENCY", 5010: "ROUTINE"}
REREGISTER_EVERY = 5   # seconds


def make_socket(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    return s


def register_loop(server_ip, name, stop):
    """Send REGISTER now and then every few seconds (heartbeat)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pkt = json.dumps({"type": "REGISTER", "name": name}).encode()
    while not stop.is_set():
        s.sendto(pkt, (server_ip, SERVER_REG_PORT))
        stop.wait(REREGISTER_EVERY)
    s.sendto(json.dumps({"type": "UNREGISTER"}).encode(),
             (server_ip, SERVER_REG_PORT))


def main():
    if len(sys.argv) < 3:
        print("usage: client.py <server_ip> <name> [logfile.csv]")
        sys.exit(1)
    server_ip, name = sys.argv[1], sys.argv[2]
    logfile = sys.argv[3] if len(sys.argv) > 3 else f"results/{name}.csv"
    os.makedirs(os.path.dirname(logfile) or ".", exist_ok=True)

    socks = {make_socket(p): lvl for p, lvl in LISTEN_PORTS.items()}
    ack_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stop = threading.Event()
    threading.Thread(target=register_loop, args=(server_ip, name, stop),
                     daemon=True).start()

    seen = set()   # (level, seq) already logged, so retransmits aren't double-counted
    with open(logfile, "w", newline="") as f:
        log = csv.writer(f)
        log.writerow(["seq", "level", "attempt", "latency_ms", "recv_time"])
        print(f"[{name}] listening, logging to {logfile} (Ctrl-C to quit)")
        try:
            while True:
                ready, _, _ = select.select(list(socks), [], [])
                for s in ready:
                    data, _addr = s.recvfrom(2048)
                    now = time.time()
                    try:
                        msg = json.loads(data.decode())
                        seq = msg["seq"]
                        ts = msg["ts"]
                    except (UnicodeDecodeError, json.JSONDecodeError,
                            KeyError, TypeError):
                        continue                  # not a valid NOTIFY, ignore
                    level = socks[s]
                    if level == "EMERGENCY":      # ACK every copy, even duplicates
                        ack = json.dumps({"type": "ACK", "seq": seq}).encode()
                        ack_sock.sendto(ack, (server_ip, SERVER_ACK_PORT))
                    if (level, seq) in seen:
                        continue
                    seen.add((level, seq))
                    latency_ms = (now - ts) * 1000
                    log.writerow([seq, level, msg.get("attempt", 0),
                                  f"{latency_ms:.2f}", f"{now:.3f}"])
                    f.flush()
                    print(f"[{name}] {level} seq={seq} "
                          f"\"{msg.get('msg')}\" ({latency_ms:.1f} ms)")
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            time.sleep(0.2)   # give the UNREGISTER a moment to go out


if __name__ == "__main__":
    main()
