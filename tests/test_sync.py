# Tests for the peer-to-peer clock offset math in network_sync.compute_offset.
# Run from the repo root:  python tests\test_sync.py
# Pure math, no MQTT / network needed.

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# network_sync imports mqtt/ntplib at module load; stub them so the pure
# function is importable without those packages in the sandbox.
import types
for name in ("ntplib", "paho", "paho.mqtt", "paho.mqtt.client"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
sys.modules["paho.mqtt"].client = sys.modules["paho.mqtt.client"]
sys.modules["paho.mqtt.client"].Client = object
sys.modules["paho.mqtt.client"].CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)

from network_sync import compute_offset

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok  {name}")
    else:
        FAIL += 1; print(f" FAIL {name} {detail}")


def simulate(true_offset, delay_out, delay_back, client_send=1000.0):
    """Model one ping/pong.
    true_offset = host clock minus client clock (host ahead by this many s).
    delay_out   = network delay client->host (s)
    delay_back  = network delay host->client (s)
    """
    t0 = client_send                              # client local send
    host_recv_client_frame = t0 + delay_out
    t1 = host_recv_client_frame + true_offset     # in host frame
    t2 = t1                                        # host replies instantly
    client_recv_client_frame = host_recv_client_frame + delay_back
    t3 = client_recv_client_frame                  # client local recv
    return t0, t1, t2, t3


def test_symmetric():
    print("[symmetric delay]")
    for true_off in (-2.0, -0.4, 0.0, 0.75, 3.3):
        t0, t1, t2, t3 = simulate(true_off, 0.05, 0.05)
        rtt, off = compute_offset(t0, t1, t2, t3)
        check(f"offset recovered (true={true_off:+.2f})", abs(off - true_off) < 1e-9,
              f"got {off}")
        check(f"rtt correct (true={true_off:+.2f})", abs(rtt - 0.10) < 1e-9, f"got {rtt}")


def test_host_reply_gap():
    print("[host processing gap]")
    # Host takes 3 ms to turn the pong around; must not distort offset.
    t0, t1, t2, t3 = simulate(0.5, 0.04, 0.04)
    t2 = t2 + 0.003
    t3 = t3 + 0.003  # client receives 3ms later because host sent 3ms later
    rtt, off = compute_offset(t0, t1, t2, t3)
    check("offset unaffected by host gap", abs(off - 0.5) < 1e-9, f"got {off}")
    check("rtt excludes host gap", abs(rtt - 0.08) < 1e-9, f"got {rtt}")


def test_asymmetric_bounded():
    print("[asymmetric delay]")
    # Asymmetric paths bias the estimate by at most half the asymmetry.
    t0, t1, t2, t3 = simulate(0.0, 0.10, 0.02)  # 80ms asymmetry
    rtt, off = compute_offset(t0, t1, t2, t3)
    check("error <= half asymmetry", abs(off) <= 0.04 + 1e-9, f"got {off}")
    check("rtt is total path", abs(rtt - 0.12) < 1e-9, f"got {rtt}")


def test_best_sample_selection():
    print("[best-of-samples]")
    # Mimic the client's 'pick lowest rtt' policy across noisy samples.
    samples = []
    for dout, dback in [(0.30, 0.30), (0.05, 0.05), (0.20, 0.02)]:
        t0, t1, t2, t3 = simulate(1.0, dout, dback)
        samples.append(compute_offset(t0, t1, t2, t3))
    best = min(samples, key=lambda s: s[0])
    check("lowest-rtt sample is the clean one", abs(best[0] - 0.10) < 1e-9, f"got {best[0]}")
    check("best sample offset is accurate", abs(best[1] - 1.0) < 1e-9, f"got {best[1]}")


if __name__ == "__main__":
    test_symmetric()
    test_host_reply_gap()
    test_asymmetric_bounded()
    test_best_sample_selection()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
