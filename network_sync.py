import time
import json
import base64
import uuid
import os
import threading
import paho.mqtt.client as mqtt

BROKER = "broker.hivemq.com"
PORT = 1883
BASE_TOPIC = "bpsr_bard/room"

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
        self.client_id = str(uuid.uuid4())
        self.nickname = "Player"
        self.room_code = None
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
        self.running = False
        self.client.loop_stop()
        self.client.disconnect()

    def _reset_room_state(self):
        """Return to the not-in-a-room state (keeps the MQTT connection so the
        user can host or join again)."""
        self.room_code = None
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
        topic = f"{BASE_TOPIC}/{self.room_code}"
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
            self.client.unsubscribe(f"{BASE_TOPIC}/{self.room_code}")
        except Exception as e:
            _log(f"Error disbanding room: {e}")
        self._reset_room_state()

    def host_room(self, room_code, nickname):
        self.room_code = room_code.upper()
        self.nickname = nickname
        self.is_host = True
        self._room_joined_at = time.time()
        self._sync_stall_reported = False
        self.room_state = {
            "players": [{"client_id": self.client_id, "nickname": self.nickname, "channels": [], "connected": True, "last_seen": time.time(), "ready": True}],
            "filename": None
        }
        self._subscribe()
        self._broadcast_state()

    def join_room(self, room_code, nickname):
        self.room_code = room_code.upper()
        self.nickname = nickname
        self.is_host = False
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
            with open(file_path, "rb") as f:
                data = base64.b64encode(f.read()).decode('utf-8')
            
            self._publish({
                "type": "midi_file",
                "filename": filename,
                "data": data
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
        topic = f"{BASE_TOPIC}/{self.room_code}"
        self.client.subscribe(topic)

    def _publish(self, payload_dict):
        topic = f"{BASE_TOPIC}/{self.room_code}"
        self.client.publish(topic, json.dumps(payload_dict))

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
            payload = json.loads(msg.payload.decode('utf-8'))
            msg_type = payload.get("type")

            # --- Peer clock sync handshake (handled before everything else) ---
            if msg_type == "sync_ping":
                if self.is_host:
                    t1 = time.time()
                    self._publish({"type": "sync_pong", "to": payload["from"],
                                   "id": payload["id"], "t0": payload["t0"],
                                   "t1": t1, "t2": time.time()})
                return
            if msg_type == "sync_pong":
                if (not self.is_host) and payload.get("to") == self.client_id:
                    t3 = time.time()
                    rtt, offset = compute_offset(payload["t0"], payload["t1"],
                                                 payload["t2"], t3)
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
                    self._reset_room_state()
                    if self.on_disband:
                        self.on_disband()
                return

            # Host kicked a specific player -> only that client resets.
            if msg_type == "kick":
                if (self.room_code and not self.is_host
                        and payload.get("client_id") == self.client_id):
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
                    exists = False
                    for p in self.room_state["players"]:
                        if p["client_id"] == payload["client_id"]:
                            p["connected"] = True
                            p["last_seen"] = time.time()
                            exists = True
                            break
                    if not exists:
                        self.room_state["players"].append({
                            "client_id": payload["client_id"],
                            "nickname": payload["nickname"],
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
                    for p in self.room_state["players"]:
                        if p["client_id"] == payload["client_id"]:
                            p["ready"] = payload["ready"]
                            self._broadcast_state()
                            break

            else:
                # Client processing
                if msg_type == "state":
                    self.room_state = payload["state"]
                    if self.on_state_change:
                        self.on_state_change(self.room_state)
                elif msg_type == "midi_file":
                    data = base64.b64decode(payload["data"])
                    filename = payload["filename"]
                    if self.on_midi_received:
                        self.on_midi_received(filename, data)

            # Both host and client handle 'play' and 'stop'
            if msg_type == "play":
                start_time = payload["start_time"]
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
