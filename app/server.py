import json
import select
import socket
import threading
import time

REG_PORT = 5000   # clients -> server: REGISTER / UNREGISTER
ACK_PORT = 5006   # clients -> server: ACK

registry = {}     # subscriber ip -> name
pending = {}      # seq -> set of ips that haven't ACKed yet
lock = threading.Lock()


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
                    print(f"[+] registered {registry[ip]} ({ip})")
                elif kind == "UNREGISTER":
                    registry.pop(ip, None)
                    print(f"[-] unregistered {ip}")
                elif kind == "ACK":
                    waiting = pending.get(msg.get("seq"))
                    if waiting is not None:
                        waiting.discard(ip)


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
    print("server up, waiting for subscribers (Ctrl-C to quit)")
    try:
        while True:
            time.sleep(1)       # sender logic goes here next
    except KeyboardInterrupt:
        pass
