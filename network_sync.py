import ntplib
import time
import json
import base64
import uuid
import threading
import paho.mqtt.client as mqtt

BROKER = "broker.hivemq.com"
PORT = 1883
BASE_TOPIC = "bpsr_bard/room"

class NetworkManager:
    def __init__(self, on_state_change=None, on_play_cmd=None, on_stop_cmd=None, on_midi_received=None):
        self.client_id = str(uuid.uuid4())
        self.nickname = "Player"
        self.room_code = None
        self.is_host = False
        
        self.ntp_offset = 0.0
        self._sync_ntp()
        
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        
        # Callbacks to UI
        self.on_state_change = on_state_change
        self.on_play_cmd = on_play_cmd
        self.on_stop_cmd = on_stop_cmd
        self.on_midi_received = on_midi_received
        
        # Room State (Host maintains this)
        self.room_state = {
            "players": [], # list of dicts: {"client_id": "", "nickname": "", "channels": [], "connected": True, "last_seen": 0, "ready": False}
            "filename": None
        }

        self.heartbeat_thread = None
        self.running = False

    def _sync_ntp(self):
        try:
            client = ntplib.NTPClient()
            response = client.request('pool.ntp.org', version=3, timeout=3)
            self.ntp_offset = response.offset
            print(f"NTP Sync successful. Offset: {self.ntp_offset:.3f}s")
        except Exception as e:
            print(f"NTP Sync failed. Relying on local clock. Error: {e}")
            self.ntp_offset = 0.0

    def get_global_time(self):
        return time.time() + self.ntp_offset

    def connect(self):
        self.running = True
        self.client.connect(BROKER, PORT, 60)
        self.client.loop_start()
        
        # Start heartbeat loop
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

    def disconnect(self):
        self.running = False
        self.client.loop_stop()
        self.client.disconnect()

    def host_room(self, room_code, nickname):
        self.room_code = room_code.upper()
        self.nickname = nickname
        self.is_host = True
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
            print(f"Failed to share MIDI: {e}")

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
        print(f"Connected to MQTT broker with result code {reason_code}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            msg_type = payload.get("type")

            if self.is_host:
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
            pass # Ignore malformed json
