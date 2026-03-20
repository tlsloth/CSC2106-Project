from machine import Pin, time_pulse_us
import utime
import bluetooth
from micropython import const
import struct
import uasyncio as asyncio

# ─── Ultrasonic Config ───────────────────────────────────────────
TRIG = Pin(0, Pin.OUT)
ECHO = Pin(1, Pin.IN)

def read_distance():
    TRIG.value(0)
    utime.sleep_us(5)
    TRIG.value(1)
    utime.sleep_us(10)
    TRIG.value(0)
    duration = time_pulse_us(ECHO, 1, 30000)
    if duration < 0:
        return None
    return (duration * 0.0343) / 2

# ─── BLE Config ──────────────────────────────────────────────────
_ADV_INTERVAL_MS        = const(100)
_IRQ_CENTRAL_CONNECT    = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE        = const(3)

_FLAG_READ   = const(0x0002)
_FLAG_NOTIFY = const(0x0010)

_DIST_UUID = bluetooth.UUID(0xFFF0)
_DIST_CHAR = (bluetooth.UUID(0xFFF1),
              _FLAG_READ | _FLAG_NOTIFY,)
_DIST_SVC  = (_DIST_UUID, (_DIST_CHAR,),)

_DEVICE_NAME = "PicoUltrasonic"
class BLEUltrasonicPeripheral:
    def __init__(self, ble):
        self._ble = ble
        self._ble.active(True)
        self._ble.irq(self._irq)
        ((self._dist_handle,),) = self._ble.gatts_register_services((_DIST_SVC,))
        self._connections = set()
        self._advertise()

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            self._connections.add(conn_handle)
            print("Connected handle:", conn_handle)
        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn_handle, _, _ = data
            self._connections.discard(conn_handle)
            print("Disconnected, restarting advert...")
            self._advertise()

    def _advertise(self):
        name_bytes = _DEVICE_NAME.encode()
        svc_uuid_bytes = struct.pack("<H", 0xFFF0)
        adv_data = (
            bytes([0x02, 0x01, 0x06]) +
            bytes([len(name_bytes) + 1, 0x09]) + name_bytes +
            bytes([len(svc_uuid_bytes) + 1, 0x03]) + svc_uuid_bytes
        )
        self._ble.gap_advertise(_ADV_INTERVAL_MS * 1000, adv_data=adv_data)
        print("Advertising as:", _DEVICE_NAME)

    def update_distance(self, dist_cm):
        val = struct.pack("<H", int(dist_cm))
        self._ble.gatts_write(self._dist_handle, val)
        for conn in self._connections:
            self._ble.gatts_notify(conn, self._dist_handle)
        print("Distance:", int(dist_cm), "cm -> BLE updated")

    def update_out_of_range(self):
        # 0xFFFF sentinel means ultrasonic measurement unavailable.
        val = struct.pack("<H", 0xFFFF)
        self._ble.gatts_write(self._dist_handle, val)
        for conn in self._connections:
            self._ble.gatts_notify(conn, self._dist_handle)
        print("Distance: out of range -> BLE updated (0xFFFF)")


async def main():
    ble = bluetooth.BLE()
    peripheral = BLEUltrasonicPeripheral(ble)
    print("Pico W BLE Ultrasonic peripheral running...")

    while True:
        d = read_distance()
        if d is not None and d < 400:
            peripheral.update_distance(d)
        else:
            peripheral.update_out_of_range()
        await asyncio.sleep_ms(500)


asyncio.run(main())