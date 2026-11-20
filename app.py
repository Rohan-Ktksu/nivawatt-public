"""Start the NivaWatt local service: python app.py [--port 8780] [--host 127.0.0.1]"""
import argparse
import socket

import uvicorn


def lan_address():
    """This laptop's address on the local network (no packet is sent), or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET address: only selects the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to reach it from a phone on the LAN")
    ap.add_argument("--port", type=int, default=8780)
    a = ap.parse_args()
    from backend.main import ensure_manager_codes
    ensure_manager_codes()  # creates ignored config/local.json on first start; one manager code per campus building
    ip = lan_address() if a.host == "0.0.0.0" else None
    if ip:
        print(f"[nivawatt] phones on this network: http://{ip}:{a.port}/web/campus.html (operator), manager links are on that page")
    uvicorn.run("backend.main:app", host=a.host, port=a.port)
