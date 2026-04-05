# main.py — HC-SR04 BLE Mesh Sensor
# Protocol: Sensor is CENTRAL-ONLY. No advertising.
# join_ack is read back from the bridge's own TX char (0x3333) after writing join_req.
#
# Connection flow per send_mesh_packet():
#   1. scan → find bridge_*
#   2. connect
#   3. write payload  → bridge's RX char (0x2222)
#   4. read response  ← bridge's TX char (0x3333)  [only for join_req / hello]
#   5. disconnect
#   (1 connection slot used at a time, no advertising ever)

import sys
import json
from machine import Pin, time_pulse_us
import time
import uasyncio as asyncio
import aioble
import bluetooth
import gc

# --- Config ---
NODE_ID           = "ble_sensor_A"
MESH_JOIN_KEY     = "mesh_key_v1"
MESH_NETWORK_NAME = "CSC2106_MESH"
MESH_TARGET_DST   = "dashboard_main"

MESH_SERVICE_UUID = bluetooth.UUID(0x1111)
MESH_RX_CHAR_UUID = bluetooth.UUID(0x2222)   # bridge RX  — sensor writes here
MESH_TX_CHAR_UUID = bluetooth.UUID(0x3333)   # bridge TX  — sensor reads ack here

# --- HC-SR04 pins ---
TRIG = Pin(0, Pin.OUT)
ECHO = Pin(1, Pin.IN)

# --- BLE State ---
_dynamic_gateway  = None
_ble_active       = False
_is_joined        = False
_session_token    = ""
_seq_num          = 0
_fail_count       = 0
MAX_FAILS         = 3

_radio_lock = None

def _get_lock():
    global _radio_lock
    if _radio_lock is None:
        _radio_lock = asyncio.Lock()
    return _radio_lock


def _generate_auth_hash(key):
    h = 0x811c9dc5
    for c in key:
        h ^= ord(c)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return "{:08x}".format(h)


def read_hcsr04():
    TRIG.value(0)
    time.sleep_us(5)
    TRIG.value(1)
    time.sleep_us(10)
    TRIG.value(0)
    duration = time_pulse_us(ECHO, 1, 30000)
    if duration < 0:
        return None
    distance_cm = (duration * 0.0343) / 2
    if distance_cm > 400:
        return None
    return {"distance": round(distance_cm, 1)}


def init():
    global _ble_active
    try:
        # No GATT server, no advertising — pure central
        _ble_active = True
        print("[BLE_EDGE] BLE initialised (central-only, no advertising)")
        return True
    except Exception as e:
        print("[BLE_EDGE] BLE init failed: {}".format(e))
        return False


async def _safe_disconnect(connection):
    if connection is None:
        return
    try:
        await asyncio.wait_for_ms(connection.disconnect(), 3000)
    except Exception:
        pass
    finally:
        await asyncio.sleep_ms(200)
        gc.collect()


async def send_mesh_packet(pkt_dict, expect_ack=False):
    """
    Scan → connect → discover (robust) → write → optional read → disconnect
    """
    global _dynamic_gateway, _seq_num, _is_joined, _session_token

    if not _ble_active:
        return False, None

    pkt_dict["src"]     = NODE_ID
    pkt_dict["node_id"] = NODE_ID
    pkt_dict["token"]   = _session_token
    pkt_dict["seq"]     = _seq_num
    _seq_num += 1

    if _dynamic_gateway:
        pkt_dict["hop_dst"] = _dynamic_gateway

    payload_bytes = json.dumps(pkt_dict).encode("utf-8")

    async with _get_lock():
        gc.collect()

        # ───── 1. SCAN ─────
        target_addr      = None
        target_addr_type = None
        target_name      = None

        try:
            async with aioble.scan(
                duration_ms=5000, interval_us=30000, window_us=30000, active=True
            ) as scanner:
                async for result in scanner:
                    name = result.name()

                    if not name or not name.startswith("bridge_"):
                        continue

                    if _is_joined and _dynamic_gateway and name != _dynamic_gateway:
                        continue

                    target_addr_type = result.device.addr_type
                    target_addr      = bytes(result.device.addr)
                    target_name      = name

                    print("[BLE_EDGE] Found {} rssi={}".format(name, result.rssi))
                    break

        except Exception as e:
            print("[BLE_EDGE] Scan error:", e)
            return False, None

        if not target_addr or target_addr_type is None:
            print("[BLE_EDGE] No valid mesh bridges found.")
            return False, None

        # ───── 2. CONNECT + DISCOVER + WRITE ─────
        await asyncio.sleep_ms(500)

        for attempt in range(1, 4):
            connection = None
            try:
                gc.collect()
                dev = aioble.Device(target_addr_type, target_addr)
                
                print("[BLE_EDGE] Connecting attempt {}/3...".format(attempt))
                connection = await dev.connect(timeout_ms=5000)
                
                # 🔥 KEY FIX: Wait significantly longer for MTU exchange to finish
                print("[BLE_EDGE] Connected. Waiting for GATT settle...")
                await asyncio.sleep_ms(3000) 

                service = None
                rx_char = None
                tx_char = None

                # 🔥 ROBUST DISCOVERY: Manually iterate to avoid Error 13
                print("[BLE_EDGE] Discovering services...")
                async for s in connection.services():
                    if s.uuid == MESH_SERVICE_UUID:
                        service = s
                        break
                
                if service:
                    print("[BLE_EDGE] Discovering characteristics...")
                    async for c in service.characteristics():
                        if c.uuid == MESH_RX_CHAR_UUID:
                            rx_char = c
                        elif c.uuid == MESH_TX_CHAR_UUID:
                            tx_char = c

                if not service or not rx_char:
                    print("[BLE_EDGE] Discovery failed (Svc/Char not found)")
                    await _safe_disconnect(connection)
                    continue

                # ───── WRITE ─────
                await rx_char.write(payload_bytes)
                print("[BLE_EDGE] Delivered to {}".format(target_name))

                # ... (rest of your existing ACK reading code) ...

                await _safe_disconnect(connection)
                return True, ack_msg

            except (asyncio.TimeoutError, OSError) as e:
                # Error 13 is often caught as an OSError
                print("[BLE_EDGE] Attempt {} failed: {}".format(attempt, e))
                if connection:
                    await _safe_disconnect(connection)
                await asyncio.sleep_ms(2000) # Wait for radio to clear

        print("[BLE_EDGE] All attempts to {} failed.".format(target_name))
        return False, None

def _apply_ack(ack_msg):
    """Process a join_ack or command received inline after a write."""
    global _is_joined, _session_token
    if not ack_msg:
        return
    msg_type = ack_msg.get("type", "")
    if msg_type == "join_ack":
        if ack_msg.get("accepted"):
            _session_token = ack_msg.get("token", "")
            _is_joined     = True
            print("[BLE_EDGE] Joined via {}. Token={}".format(
                ack_msg.get("bridge_id"), _session_token[:8]))
        else:
            print("[BLE_EDGE] Join rejected: {}".format(ack_msg.get("reason")))
    elif msg_type == "command":
        print("[BLE_EDGE] Command received: {}".format(ack_msg))


def _handle_tx_result(success):
    global _fail_count, _is_joined, _dynamic_gateway
    if success:
        _fail_count = 0
    else:
        _fail_count += 1
        print("[BLE_EDGE] TX failed. Strike {}/{}".format(_fail_count, MAX_FAILS))
        if _fail_count >= MAX_FAILS:
            print("[BLE_EDGE] Gateway lost — resetting session.")
            _is_joined       = False
            _dynamic_gateway = None
            _fail_count      = 0


# ──────────────────────────────────────────────
# Tasks
# ──────────────────────────────────────────────

async def join_network_task():
    while True:
        if not _is_joined:
            join_req = {
                "kind":    "control",
                "type":    "join_req",
                "node_id": NODE_ID,
                "src":     NODE_ID,
                "network": MESH_NETWORK_NAME,
                "auth":    _generate_auth_hash(MESH_JOIN_KEY),
            }
            print("[BLE_EDGE] Sending join_req...")
            ok, ack = await send_mesh_packet(join_req, expect_ack=True)
            if ok:
                _apply_ack(ack)
                if not _is_joined:
                    # Bridge accepted but ack was empty — retry sooner
                    await asyncio.sleep(5)
                    continue
            await asyncio.sleep(10)
        else:
            await asyncio.sleep(1)


async def hello_task():
    while True:
        await asyncio.sleep(30)
        if _is_joined:
            hello_pkt = {
                "kind":    "control",
                "type":    "hello",
                "node_id": NODE_ID,
                "src":     NODE_ID,
                "hop_dst": _dynamic_gateway,
            }
            ok, _ = await send_mesh_packet(hello_pkt, expect_ack=False)
            _handle_tx_result(ok)


async def telemetry_task():
    while True:
        await asyncio.sleep(15)
        if not _is_joined:
            continue
        sensor_data = read_hcsr04()
        if sensor_data is None:
            print("[BLE_EDGE] Sensor read failed, skipping")
            continue
        data_pkt = {
            "kind":     "data",
            "type":     "sensor_data",
            "node_id":  NODE_ID,
            "src":      NODE_ID,
            "hop_dst":  _dynamic_gateway,
            "dst":      MESH_TARGET_DST,
            "payload":  sensor_data,
            "priority": 5,
        }
        print("[BLE_EDGE] Sending telemetry: {}".format(sensor_data))
        ok, _ = await send_mesh_packet(data_pkt, expect_ack=False)
        _handle_tx_result(ok)


async def run():
    if not init():
        print("[BLE_EDGE] Init failed, aborting")
        return
    await asyncio.gather(
        asyncio.create_task(join_network_task()),
        asyncio.create_task(hello_task()),
        asyncio.create_task(telemetry_task()),
    )

asyncio.run(run())
