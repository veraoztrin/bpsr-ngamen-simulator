import time
import json
import base64
import uuid
import os
import threading
import hashlib
import hmac
import ntpath
import math
import secrets
import binascii
from collections import deque
import paho.mqtt.client as mqtt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

BROKER = os.environ.get("BPSR_MQTT_BROKER", "broker.hivemq.com")
PORT = int(os.environ.get("BPSR_MQTT_PORT", "8883"))
BASE_TOPIC = "bpsr_bard/room"
MAX_MIDI_BYTES = 5 * 1024 * 1024
MAX_MIDI_B64_CHARS = ((MAX_MIDI_BYTES + 2) // 3) * 4
MAX_MESSAGE_BYTES = MAX_MIDI_B64_CHARS + 64 * 1024
MAX_CONTROL_MESSAGE_BYTES = 128 * 1024
MAX_PLAYERS = 16
MAX_NICKNAME_LENGTH = 32
MAX_FILENAME_LENGTH = 180
MIN_ROOM_CREDENTIAL_LENGTH = 16
ROOM_CREDENTIAL_PREFIX = "bpsr2"
PROTOCOL_VERSION = 2
HOST_ONLY_TYPES = {
    "sync_pong", "state", "midi_file", "play", "stop", "disband", "kick",
}

# The release build runs with PyInstaller's --windowed flag (no console), so
# print() output disappears into the void for every real user. Mirror it to a
# small log file next to the user's home dir so a connection problem can
# actually be diagnosed after the fact instead of just looking like "stuck".
_LOG_PATH = os.path.join(os.path.expanduser("~"), ".bpsr_midi_player", "network.log")


def _log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # Cheap cap so the log can't grow forever across long sessions.
        if os.path.getsize(_LOG_PATH) > 512_000:
            with open(_LOG_PATH, "r+", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-2000:]
                f.seek(0)
                f.writelines(lines)
                f.truncate()
    except Exception:
        pass  # Logging must never be the thing that breaks the app.


def compute_offset(t0, t1, t2, t3):
    """NTP-style clock offset between a client and the host, measured over a
    single ping/pong round trip. All times are seconds.

      t0 = client's local time when it sent the ping
      t1 = host's local time when it received the ping
      t2 = host's local time when it sent the pong
      t3 = client's local time when it received the pong

    Returns (round_trip_delay, offset) where
      offset = how far the HOST clock is ahead of the CLIENT clock, i.e.
               host_time  ~=  client_time + offset.
    """
    rtt = (t3 - t0) - (t2 - t1)
    offset = ((t1 - t0) + (t2 - t3)) / 2.0
    return rtt, offset


def select_offset(samples, k=5):
    """Pick a stable offset estimate from recent ping/pong samples.

    samples: list of (local_ts, rtt, offset).
    Uses the median offset among the k lowest-RTT samples. Low RTT means the
    round trip was clean (little queuing), which also means the least
    path-asymmetry bias, so those samples are the most trustworthy; the median
    across several of them removes single-sample jitter.

    Returns (display_rtt, offset). display_rtt is the best-case RTT seen.
    """
    if not samples:
        return None, 0.0
    by_rtt = sorted(samples, key=lambda s: s[1])[:max(1, k)]
    offs = sorted(s[2] for s in by_rtt)
    m = len(offs)
    if m % 2:
        offset = offs[m // 2]
    else:
        offset = (offs[m // 2 - 1] + offs[m // 2]) / 2.0
    return by_rtt[0][1], offset


class NetworkManager:
    def __init__(self, on_state_change=None, on_play_cmd=None, on_stop_cmd=None,
                 on_midi_received=None, on_sync_update=None, on_disband=None,
                 on_connection_status=None, on_sync_stalled=None, on_kicked=None):
        self._identity_private = Ed25519PrivateKey.generate()
        self._identity_public_raw = self._identity_private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        self._identity_public_text = self._b64url(self._identity_public_raw)
        self.client_id = hashlib.sha256(self._identity_public_raw).hexdigest()[:32]
        self.nickname = "Player"
        self.room_code = None
        self._room_secret = None
        self._room_topic_id = None
        self._expected_host_public_raw = None
        self.host_id = None
        self._seen_message_ids = set()
        self._seen_message_order = deque()
        self.is_host = False

        # Peer-to-peer clock sync (replaces the old one-shot external NTP,
        # which used to be blocked by the same kind of firewalls that can
        # also get in the way of this MQTT connection - see on_connection_status).
        self.host_offset = 0.0         # host_time ~= local_time + host_offset
        self.sync_rtt = None           # best round-trip delay seen (seconds)
        self.is_synced = False
        self._sync_samples = []        # list of (local_ts, rtt, offset)
        self._sync_id = 0
        self._room_joined_at = None    # monotonic time we joined/hosted, for the stall watchdog
        self._sync_stall_reported = False
        self.sync_thread = None

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        # The broker transport must be confidential and server-authenticated.
        # Paho uses the operating system CA store and validates the hostname.
        self.client.tls_set()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        # Auto-reconnect (paho already retries in the loop_start() thread) but
        # with a much shorter cap than the 120s default - a live jam session
        # shouldn't wait two minutes to notice the wifi blinked.
        self.client.reconnect_delay_set(min_delay=1, max_delay=8)

        # Callbacks to UI
        self.on_state_change = on_state_change
        self.on_play_cmd = on_play_cmd
        self.on_stop_cmd = on_stop_cmd
        self.on_midi_received = on_midi_received
        self.on_sync_update = on_sync_update
        self.on_disband = on_disband
        self.on_connection_status = on_connection_status  # ("connected"|"reconnecting"|"disconnected", detail)
        self.on_sync_stalled = on_sync_stalled             # fired once if no sync_pong arrives for a while
        self.on_kicked = on_kicked                         # fired on the removed client when the host kicks them

        # Room State (Host maintains this)
        self.room_state = {
            "players": [], # list of dicts: {"client_id": "", "nickname": "", "channels": [], "connected": True, "last_seen": 0, "ready": False}
            "filename": None
        }

        self.heartbeat_thread = None
        self.running = False

    def get_global_time(self):
        # The shared reference frame IS the host's clock.
        # Host: its own clock. Client: local clock corrected by measured offset.
        if self.is_host:
            return time.time()
        return time.time() + self.host_offset

    def connect(self):
        if self.running:
            return
        self.running = True
        try:
            self.client.connect(BROKER, PORT, 60)
        except Exception as e:
            # A blocked/unreachable broker (firewalled port 1883, no network,
            # DNS failure, ...) used to raise straight out of a GUI button
            # handler with nothing shown to the user. Surface it instead.
            _log(f"Could not reach {BROKER}:{PORT}: {e}")
            self.running = False
            if self.on_connection_status:
                self.on_connection_status("disconnected", str(e))
            raise
        self.client.loop_start()

        # Start heartbeat loop
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

        # Start peer clock-sync loop (only pings while joined as a client)
        self.sync_thread = threading.Thread(target=self._client_sync_loop, daemon=True)
        self.sync_thread.start()

    def _client_sync_loop(self):
        """Continuously measure this client's clock offset against the host.
        A quick burst on join for a fast initial lock, then steady refreshes
        so drift never accumulates before a SYNC PLAY."""
        while self.running:
            if self.room_code and not self.is_host:
                # Burst until we have a few samples, then a steady trickle.
                n = 10 if len(self._sync_samples) < 5 else 1
                for _ in range(n):
                    if not (self.running and self.room_code and not self.is_host):
                        break
                    self._send_sync_ping()
                    time.sleep(0.12)
                time.sleep(2.0)

                # Watchdog: if we've been in the room for a while with zero
                # sync_pong replies, the "Syncing clock..." lock is otherwise
                # silent and permanent (no timeout existed before this). Tell
                # the UI once so it can show *something* actionable instead of
                # hanging forever with no explanation.
                if (not self.is_synced and not self._sync_stall_reported
                        and self._room_joined_at is not None
                        and time.time() - self._room_joined_at > 8.0):
                    self._sync_stall_reported = True
                    _log("No sync_pong received after 8s - host unreachable "
                         "over the MQTT topic, or the broker connection is stuck.")
                    if self.on_sync_stalled:
                        self.on_sync_stalled()
            else:
                time.sleep(0.5)

    def _send_sync_ping(self):
        self._sync_id += 1
        # Capture t0 as close to publish as possible.
        self._publish({"type": "sync_ping", "from": self.client_id,
                       "id": self._sync_id, "t0": time.time()})

    def disconnect(self):
        if not self.running:
            return
        self.running = False
        self.client.loop_stop()
        self.client.disconnect()

    def _reset_room_state(self):
        """Return to the not-in-a-room state (keeps the MQTT connection so the
        user can host or join again)."""
        self.room_code = None
        self._room_secret = None
        self._room_topic_id = None
        self._expected_host_public_raw = None
        self.host_id = None
        self._seen_message_ids = set()
        self._seen_message_order = deque()
        self.is_host = False
        self.room_state = {"players": [], "filename": None}
        self.host_offset = 0.0
        self.sync_rtt = None
        self.is_synced = False
        self._sync_samples = []
        self._room_joined_at = None
        self._sync_stall_reported = False

    def leave_room(self):
        """A client (or host) leaves the current room. Clients tell the host so
        they're removed from the roster; the room itself stays open."""
        if not self.room_code:
            return
        topic = self._room_topic()
        try:
            if not self.is_host:
                self._publish({"type": "leave", "client_id": self.client_id})
            self.client.unsubscribe(topic)
        except Exception as e:
            _log(f"Error leaving room: {e}")
        self._reset_room_state()

    def disband_room(self):
        """Host closes the room for everyone. All clients are notified and reset
        to the disconnected state."""
        if not self.is_host or not self.room_code:
            return
        try:
            self._publish({"type": "disband"})
            self.client.unsubscribe(self._room_topic())
        except Exception as e:
            _log(f"Error disbanding room: {e}")
        self._reset_room_state()

    def host_room(self, room_code, nickname):
        room_code = self.create_room_credential(room_code)
        self._configure_room(room_code)
        self.nickname = self._clean_nickname(nickname, "Host")
        self.is_host = True
        self.host_id = self.client_id
        self._room_joined_at = time.time()
        self._sync_stall_reported = False
        self.room_state = {
            "players": [{"client_id": self.client_id, "nickname": self.nickname, "channels": [], "connected": True, "last_seen": time.time(), "ready": True}],
            "filename": None
        }
        self._subscribe()
        self._broadcast_state()
        return room_code

    def join_room(self, room_code, nickname):
        self._configure_room(room_code)
        self.nickname = self._clean_nickname(nickname, "Player")
        self.is_host = False
        self.host_id = hashlib.sha256(
            self._expected_host_public_raw).hexdigest()[:32]
        self._room_joined_at = time.time()
        self._sync_stall_reported = False
        self._subscribe()

        # Send join request
        self._publish({
            "type": "join",
            "client_id": self.client_id,
            "nickname": self.nickname
        })

    def assign_channels(self, target_client_id, channels):
        if not self.is_host:
            return
        for p in self.room_state["players"]:
            if p["client_id"] == target_client_id:
                p["channels"] = channels
        self._broadcast_state()

    def kick_player(self, target_client_id):
        """Host removes a player from the room. Mirrors leave/disband:
        drop them from the roster here, and tell that specific client
        (over the shared topic, filtered by client_id) to reset itself."""
        if not self.is_host or not self.room_code:
            return
        if target_client_id == self.client_id:
            return  # can't kick yourself
        self.room_state["players"] = [
            p for p in self.room_state["players"]
            if p["client_id"] != target_client_id
        ]
        self._publish({"type": "kick", "client_id": target_client_id})
        self._broadcast_state()

    def send_ready_status(self, is_ready):
        self._publish({
            "type": "ready",
            "client_id": self.client_id,
            "ready": is_ready
        })

    def share_midi(self, file_path, filename):
        if not self.is_host:
            return
        self.room_state["filename"] = filename
        try:
            size = os.path.getsize(file_path)
            if size > MAX_MIDI_BYTES:
                raise ValueError(
                    f"MIDI is too large to share ({size} bytes; "
                    f"limit is {MAX_MIDI_BYTES})")
            with open(file_path, "rb") as f:
                raw = f.read()
            if not raw.startswith(b"MThd"):
                raise ValueError("File does not contain a Standard MIDI header")
            safe_name = ntpath.basename(str(filename))[:MAX_FILENAME_LENGTH]
            if os.path.splitext(safe_name)[1].lower() not in {".mid", ".midi"}:
                raise ValueError("Shared file must use the .mid or .midi extension")
            data = base64.b64encode(raw).decode('ascii')
            
            self._publish({
                "type": "midi_file",
                "filename": safe_name,
                "data": data,
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
            self._broadcast_state()
        except Exception as e:
            _log(f"Failed to share MIDI: {e}")

    def send_play(self, delay_seconds=4.0):
        if not self.is_host:
            return
        start_time = self.get_global_time() + delay_seconds
        
        # Reset ready status
        for p in self.room_state["players"]:
            if p["client_id"] != self.client_id:
                p["ready"] = False
        self._broadcast_state()
        
        self._publish({
            "type": "play",
            "start_time": start_time
        })
        
    def send_stop(self):
        if not self.is_host:
            return
        self._publish({
            "type": "stop"
        })

    def _subscribe(self):
        self.client.subscribe(self._room_topic())

    def _publish(self, payload_dict):
        if not self.room_code:
            return
        payload = dict(payload_dict)
        payload["_sender"] = self.client_id
        payload["_msg_id"] = uuid.uuid4().hex
        payload["_sender_pub"] = self._identity_public_text
        payload["_proto"] = PROTOCOL_VERSION
        secret = getattr(self, "_room_secret", None)
        if secret:
            payload["_sig"] = self._sign(payload)
            payload["_sender_sig"] = self._sign_sender(payload)
        qos = 1 if payload.get("type") in HOST_ONLY_TYPES else 0
        self.client.publish(
            self._room_topic(),
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            qos=qos)

    @staticmethod
    def _b64url(data):
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    @staticmethod
    def _b64url_decode(text):
        if not isinstance(text, str):
            raise ValueError("Expected base64 text")
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    @staticmethod
    def _clean_nickname(value, default):
        value = str(value or "").strip()
        return (value or default)[:MAX_NICKNAME_LENGTH]

    def create_room_credential(self, seed=None):
        """Create a room invitation that pins this host's signing identity."""
        seed = str(seed or "").strip()
        if seed.startswith(f"{ROOM_CREDENTIAL_PREFIX}."):
            # Never host from an invitation created by another identity.
            seed = ""
        if "." in seed or len(seed) > 128:
            raise ValueError(
                "Custom room secrets must be at most 128 characters and contain no dots")
        secret = seed or secrets.token_urlsafe(24)
        if len(secret) < MIN_ROOM_CREDENTIAL_LENGTH:
            raise ValueError(
                f"Room secret must be at least {MIN_ROOM_CREDENTIAL_LENGTH} characters")
        return f"{ROOM_CREDENTIAL_PREFIX}.{secret}.{self._identity_public_text}"

    def _configure_room(self, credential):
        credential = credential.strip()
        parts = credential.split(".")
        if len(parts) != 3 or parts[0] != ROOM_CREDENTIAL_PREFIX:
            raise ValueError(
                "Use the complete bpsr2 room invitation generated by the host")
        if not MIN_ROOM_CREDENTIAL_LENGTH <= len(parts[1]) <= 128:
            raise ValueError("Room invitation contains a weak room secret")
        try:
            host_public = self._b64url_decode(parts[2])
            Ed25519PublicKey.from_public_bytes(host_public)
        except (ValueError, TypeError, binascii.Error):
            raise ValueError("Room invitation contains an invalid host identity")
        if len(host_public) != 32:
            raise ValueError("Room invitation contains an invalid host identity")
        self.room_code = credential
        self._expected_host_public_raw = host_public
        self._room_secret = hashlib.sha256(
            ("bpsr-room-secret:" + credential).encode("utf-8")).digest()
        # The secret itself never appears in the public MQTT topic.
        self._room_topic_id = hashlib.sha256(
            ("bpsr-room-topic:" + credential).encode("utf-8")
        ).hexdigest()[:24]
        self._seen_message_ids = set()
        self._seen_message_order = deque()

    def _room_topic(self):
        topic_id = getattr(self, "_room_topic_id", None)
        if not topic_id:
            # Compatibility for old in-process test doubles. Real rooms always
            # go through _configure_room().
            topic_id = self.room_code
        return f"{BASE_TOPIC}/{topic_id}"

    def _sign(self, payload):
        unsigned = {
            k: v for k, v in payload.items()
            if k not in {"_sig", "_sender_sig"}
        }
        encoded = json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")
        return hmac.new(self._room_secret, encoded, hashlib.sha256).hexdigest()

    def _sign_sender(self, payload):
        unsigned = {k: v for k, v in payload.items() if k != "_sender_sig"}
        encoded = json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")
        return self._b64url(self._identity_private.sign(encoded))

    def _verify(self, payload):
        secret = getattr(self, "_room_secret", None)
        if not secret:
            return True
        supplied = payload.get("_sig")
        return isinstance(supplied, str) and hmac.compare_digest(
            supplied, self._sign(payload))

    def _verify_sender(self, payload):
        try:
            public_raw = self._b64url_decode(payload.get("_sender_pub"))
            supplied = self._b64url_decode(payload.get("_sender_sig"))
            sender = payload.get("_sender")
            if (len(public_raw) != 32 or len(supplied) != 64
                    or sender != hashlib.sha256(public_raw).hexdigest()[:32]):
                return False
            unsigned = {k: v for k, v in payload.items() if k != "_sender_sig"}
            encoded = json.dumps(
                unsigned, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False).encode("utf-8")
            Ed25519PublicKey.from_public_bytes(public_raw).verify(supplied, encoded)
            return True
        except (ValueError, TypeError, binascii.Error, InvalidSignature):
            return False

    def _accept_message_id(self, payload):
        """Reject replayed signed packets while keeping bounded memory."""
        msg_id = payload.get("_msg_id")
        if not isinstance(msg_id, str) or len(msg_id) != 32:
            return False
        try:
            int(msg_id, 16)
        except ValueError:
            return False
        if msg_id in self._seen_message_ids:
            return False
        self._seen_message_ids.add(msg_id)
        self._seen_message_order.append(msg_id)
        while len(self._seen_message_order) > 4096:
            expired = self._seen_message_order.popleft()
            self._seen_message_ids.discard(expired)
        return True

    @staticmethod
    def _finite_number(value, low=None, high=None):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if not math.isfinite(value):
            return None
        if low is not None and value < low:
            return None
        if high is not None and value > high:
            return None
        return value

    @staticmethod
    def _validate_channels(channels):
        if not isinstance(channels, list) or len(channels) > 16:
            return None
        if any(isinstance(ch, bool) or not isinstance(ch, int)
               or not 0 <= ch <= 15 for ch in channels):
            return None
        return sorted(set(channels))

    def _validate_state(self, state):
        if not isinstance(state, dict) or set(state) - {"players", "filename"}:
            return None
        players = state.get("players")
        if not isinstance(players, list) or not 1 <= len(players) <= MAX_PLAYERS:
            return None
        cleaned = []
        seen = set()
        for player in players:
            if not isinstance(player, dict):
                return None
            client_id = player.get("client_id")
            nickname = player.get("nickname")
            channels = self._validate_channels(player.get("channels"))
            if (not isinstance(client_id, str) or len(client_id) != 32
                    or client_id in seen or not isinstance(nickname, str)
                    or not nickname.strip()
                    or len(nickname) > MAX_NICKNAME_LENGTH
                    or channels is None
                    or not isinstance(player.get("ready"), bool)):
                return None
            seen.add(client_id)
            cleaned.append({
                "client_id": client_id,
                "nickname": nickname.strip(),
                "channels": channels,
                "connected": bool(player.get("connected", True)),
                "last_seen": self._finite_number(
                    player.get("last_seen", 0), 0) or 0,
                "ready": player["ready"],
            })
        filename = state.get("filename")
        if filename is not None:
            filename = ntpath.basename(str(filename))[:MAX_FILENAME_LENGTH]
        return {"players": cleaned, "filename": filename}

    @staticmethod
    def _reject_json_constant(value):
        raise ValueError(f"Invalid JSON numeric constant: {value}")

    def _unsubscribe_current(self):
        if not self.room_code:
            return
        try:
            self.client.unsubscribe(self._room_topic())
        except Exception as e:
            _log(f"Error unsubscribing from room: {e}")

    def _broadcast_state(self):
        if self.is_host:
            self._publish({
                "type": "state",
                "state": self.room_state
            })
            if self.on_state_change:
                self.on_state_change(self.room_state)

    def _heartbeat_loop(self):
        while self.running:
            if self.room_code:
                # Send my heartbeat
                self._publish({"type": "heartbeat", "client_id": self.client_id})
                
                # If host, check for timeouts
                if self.is_host:
                    changed = False
                    current_time = time.time()
                    for p in self.room_state["players"]:
                        if p["client_id"] != self.client_id:
                            if p["connected"] and (current_time - p["last_seen"] > 12.0):
                                p["connected"] = False
                                changed = True
                    if changed:
                        self._broadcast_state()
            time.sleep(5)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        failed = bool(getattr(reason_code, "is_failure", reason_code != 0))
        if failed:
            _log(f"MQTT connect rejected by broker: {reason_code}")
            if self.on_connection_status:
                self.on_connection_status("disconnected", str(reason_code))
            return

        _log(f"Connected to MQTT broker (result: {reason_code})")
        if self.on_connection_status:
            self.on_connection_status("connected", None)

        # If we were already in a room, this on_connect fires again after an
        # automatic reconnect (paho retries the transport on its own, but it
        # does NOT re-subscribe our topic or re-announce us to the host).
        # Without this, a single dropped wifi packet silently and permanently
        # kills the subscription: the app still "looks" connected but never
        # gets another state/sync_pong message again - exactly the "stuck on
        # Syncing clock forever" failure mode this fixes.
        if self.room_code:
            self._subscribe()
            if self.is_host:
                self._broadcast_state()
            else:
                self._publish({
                    "type": "join",
                    "client_id": self.client_id,
                    "nickname": self.nickname
                })

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        _log(f"Disconnected from MQTT broker: {reason_code}")
        if self.on_connection_status:
            self.on_connection_status("reconnecting" if self.running else "disconnected",
                                      str(reason_code))

    def _on_message(self, client, userdata, msg):
        try:
            if not self.room_code:
                return
            if hasattr(msg, "topic") and msg.topic != self._room_topic():
                return
            if len(msg.payload) > MAX_MESSAGE_BYTES:
                _log("Ignored an oversized room message")
                return
            payload = json.loads(
                msg.payload.decode("utf-8"),
                parse_constant=self._reject_json_constant)
            if not isinstance(payload, dict) or not self._verify(payload):
                _log("Ignored an unsigned or invalidly signed room message")
                return
            if not self._verify_sender(payload):
                _log("Ignored a room message with an invalid sender identity")
                return
            msg_type = payload.get("type")
            if (not isinstance(msg_type, str)
                    or payload.get("_proto") != PROTOCOL_VERSION):
                return
            if msg_type != "midi_file" and len(msg.payload) > MAX_CONTROL_MESSAGE_BYTES:
                _log("Ignored an oversized control message")
                return
            sender = payload.get("_sender")
            if getattr(self, "_room_secret", None) and not isinstance(sender, str):
                return
            if (getattr(self, "_room_secret", None)
                    and not self._accept_message_id(payload)):
                return

            # Bind claimed client IDs to the authenticated sender identity.
            claimed = payload.get("client_id")
            if (msg_type in {"join", "leave", "heartbeat", "ready"}
                    and sender and claimed != sender):
                return
            if msg_type == "sync_ping" and sender and payload.get("from") != sender:
                return

            if msg_type in HOST_ONLY_TYPES:
                try:
                    sender_public = self._b64url_decode(payload.get("_sender_pub"))
                except (ValueError, TypeError):
                    return
                if not hmac.compare_digest(
                        sender_public, self._expected_host_public_raw or b""):
                    return
                expected_host = self.client_id if self.is_host else self.host_id
                if expected_host and sender and sender != expected_host:
                    return

            # --- Peer clock sync handshake (handled before everything else) ---
            if msg_type == "sync_ping":
                if self.is_host:
                    sync_id = payload.get("id")
                    t0 = self._finite_number(payload.get("t0"), 0)
                    if (isinstance(sync_id, bool) or not isinstance(sync_id, int)
                            or not 0 <= sync_id <= 2**31 or t0 is None):
                        return
                    t1 = time.time()
                    self._publish({"type": "sync_pong", "to": payload["from"],
                                   "id": sync_id, "t0": t0,
                                   "t1": t1, "t2": time.time()})
                return
            if msg_type == "sync_pong":
                if (not self.is_host) and payload.get("to") == self.client_id:
                    t3 = time.time()
                    t0 = self._finite_number(payload.get("t0"), 0)
                    t1 = self._finite_number(payload.get("t1"), 0)
                    t2 = self._finite_number(payload.get("t2"), 0)
                    if None in (t0, t1, t2):
                        return
                    rtt, offset = compute_offset(t0, t1, t2, t3)
                    if rtt >= 0:
                        now = time.time()
                        self._sync_samples.append((now, rtt, offset))
                        cutoff = now - 25.0
                        self._sync_samples = [s for s in self._sync_samples
                                              if s[0] >= cutoff][-40:]
                        # Median offset of the lowest-RTT samples: cleaner and
                        # steadier than trusting one single fastest round trip.
                        self.sync_rtt, self.host_offset = select_offset(self._sync_samples)
                        self.is_synced = True
                        if self.on_sync_update:
                            self.on_sync_update(self.sync_rtt, self.host_offset)
                return

            # Host closed the room -> clients reset to disconnected.
            if msg_type == "disband":
                if self.room_code and not self.is_host:
                    self._unsubscribe_current()
                    self._reset_room_state()
                    if self.on_disband:
                        self.on_disband()
                return

            # Host kicked a specific player -> only that client resets.
            if msg_type == "kick":
                if (self.room_code and not self.is_host
                        and payload.get("client_id") == self.client_id):
                    self._unsubscribe_current()
                    self._reset_room_state()
                    if self.on_kicked:
                        self.on_kicked()
                return

            if self.is_host:
                if msg_type == "leave":
                    self.room_state["players"] = [
                        p for p in self.room_state["players"]
                        if p["client_id"] != payload.get("client_id")
                    ]
                    self._broadcast_state()
                    return
                if msg_type == "join":
                    nickname = payload.get("nickname")
                    if (not isinstance(nickname, str) or not nickname.strip()
                            or len(nickname) > MAX_NICKNAME_LENGTH):
                        return
                    exists = False
                    for p in self.room_state["players"]:
                        if p["client_id"] == payload["client_id"]:
                            p["connected"] = True
                            p["last_seen"] = time.time()
                            exists = True
                            break
                    if not exists:
                        if len(self.room_state["players"]) >= MAX_PLAYERS:
                            return
                        self.room_state["players"].append({
                            "client_id": payload["client_id"],
                            "nickname": nickname.strip(),
                            "channels": [],
                            "connected": True,
                            "last_seen": time.time(),
                            "ready": False
                        })
                    self._broadcast_state()
                
                elif msg_type == "heartbeat":
                    for p in self.room_state["players"]:
                        if p["client_id"] == payload["client_id"]:
                            if not p["connected"]:
                                p["connected"] = True
                                self._broadcast_state()
                            p["last_seen"] = time.time()
                            break

                elif msg_type == "ready":
                    if not isinstance(payload.get("ready"), bool):
                        return
                    for p in self.room_state["players"]:
                        if p["client_id"] == payload["client_id"]:
                            p["ready"] = payload["ready"]
                            self._broadcast_state()
                            break

            else:
                # Client processing
                if msg_type == "state":
                    state = self._validate_state(payload.get("state"))
                    if state is None or state["players"][0]["client_id"] != self.host_id:
                        return
                    self.room_state = state
                    if self.on_state_change:
                        self.on_state_change(self.room_state)
                elif msg_type == "midi_file":
                    encoded = payload["data"]
                    if not isinstance(encoded, str) or len(encoded) > MAX_MIDI_B64_CHARS:
                        raise ValueError("Received MIDI exceeds the size limit")
                    data = base64.b64decode(encoded, validate=True)
                    if len(data) > MAX_MIDI_BYTES:
                        raise ValueError("Received MIDI exceeds the size limit")
                    filename = ntpath.basename(str(payload["filename"]))
                    if (not filename
                            or len(filename) > MAX_FILENAME_LENGTH
                            or os.path.splitext(filename)[1].lower()
                            not in {".mid", ".midi"}):
                        raise ValueError("Received file is not a MIDI file")
                    expected_hash = payload.get("sha256")
                    if (not isinstance(expected_hash, str)
                            or len(expected_hash) != 64
                            or not hmac.compare_digest(
                                hashlib.sha256(data).hexdigest(), expected_hash)
                            or not data.startswith(b"MThd")):
                        raise ValueError("Received MIDI failed integrity checks")
                    if self.on_midi_received:
                        self.on_midi_received(filename, data)

            # Both host and client handle 'play' and 'stop'
            if msg_type == "play":
                start_time = self._finite_number(payload.get("start_time"), 0)
                now = self.get_global_time()
                if start_time is None or not now - 5.0 <= start_time <= now + 30.0:
                    return
                my_channels = []
                for p in self.room_state["players"]:
                    if p["client_id"] == self.client_id:
                        my_channels = p["channels"]
                        break
                
                if self.on_play_cmd:
                    self.on_play_cmd(start_time, my_channels)
            
            elif msg_type == "stop":
                if self.on_stop_cmd:
                    self.on_stop_cmd()

        except Exception as e:
            # Still non-fatal (a stray malformed/foreign payload on a shared
            # public topic shouldn't take the app down), but log it now
            # instead of swallowing it completely - this used to hide real
            # bugs in the sync/state handling with no trace anywhere.
            _log(f"Error handling message on {msg.topic if hasattr(msg, 'topic') else '?'}: {e}")
