import json
import select
import socket
import threading
import time

REG_PORT = 5000   # clients -> server: REGISTER / UNREGISTER
ACK_PORT = 5006   # clients -> server: ACK
NOTIFY_PORTS = {"EMERGENCY": 5005, "ROUTINE": 5010}   # server -> clients

ACK_TIMEOUT = 0.5   # seconds to wait for ACKs before retransmitting
MAX_RETRIES = 3

registry = {}     # subscriber ip -> name
pending = {}      # seq -> set of ips that haven't ACKed yet
seq_counter = 0
lock = threading.Lock()
send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def parse(data):
    """Return a dict, or None if the datagram isn't valid JSON."""
    try:
        msg = json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


def listener(reg_sock, ack_sock):
    """Runs in its own thread: handles REGISTER, UNREGISTER and ACK."""
    socks = [reg_sock, ack_sock]
    while True:
        ready, _, _ = select.select(socks, [], [])
        for s in ready:
            data, (ip, _port) = s.recvfrom(2048)
            msg = parse(data)
            if msg is None:
                continue                      # ignore junk
            kind = msg.get("type")
            with lock:
                if kind == "REGISTER":
                    registry[ip] = msg.get("name", ip)
                    print(f"\n[+] registered {registry[ip]} ({ip})")
                elif kind == "UNREGISTER":
                    registry.pop(ip, None)
                    print(f"\n[-] unregistered {ip}")
                elif kind == "ACK":
                    waiting = pending.get(msg.get("seq"))
                    if waiting is not None:
                        waiting.discard(ip)


def notify(level, text):
    """Send one notification to every subscriber.
    EMERGENCY messages need an ACK and are retransmitted; ROUTINE are fire-and-forget."""
    global seq_counter
    needs_ack = (level == "EMERGENCY")
    port = NOTIFY_PORTS[level]

    with lock:
        if not registry:
            print("no subscribers registered")
            return
        seq_counter += 1
        seq = seq_counter
        targets = set(registry)
        if needs_ack:
            pending[seq] = set(targets)

    first_sent = time.time()    # kept fixed so latency includes retransmit delay
    for attempt in range(1 + MAX_RETRIES):
        with lock:
            todo = set(pending[seq]) if needs_ack else targets
        if not todo:
            break
        pkt = json.dumps({"type": "NOTIFY", "seq": seq, "ts": first_sent,
                          "level": level, "msg": text,
                          "attempt": attempt}).encode()
        for ip in todo:
            send_sock.sendto(pkt, (ip, port))
        if not needs_ack:
            print(f"sent ROUTINE seq={seq} to {len(todo)} subscriber(s)")
            return
        time.sleep(ACK_TIMEOUT)

    with lock:
        missing = pending.pop(seq)
    if missing:
        print(f"EMERGENCY seq={seq}: no ACK from {sorted(missing)} "
              f"after {MAX_RETRIES} retries")
    else:
        print(f"EMERGENCY seq={seq}: all {len(targets)} subscriber(s) ACKed")


def make_socket(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    return s


if __name__ == "__main__":
    reg_sock = make_socket(REG_PORT)
    ack_sock = make_socket(ACK_PORT)
    threading.Thread(target=listener, args=(reg_sock, ack_sock),
                     daemon=True).start()
    print("server up. commands:  e <text> = emergency | r <text> = routine "
          "| list | quit")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        cmd, _, text = line.partition(" ")
        if cmd == "e":
            notify("EMERGENCY", text)
        elif cmd == "r":
            notify("ROUTINE", text)
        elif cmd == "list":
            with lock:
                print(dict(registry))
        elif cmd == "quit":
            break
        else:
            print("unknown command")
