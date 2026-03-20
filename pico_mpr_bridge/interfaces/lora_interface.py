# interfaces/lora_interface.py — LoRa RX/TX tasks, SX127x wrapper

import json
import time
import config
from utils import logger
from core import packet
from core.neighbour import create_hello_payload, parse_hello

TAG = "LoRa"

_sx = None  # SX127x driver instance

REG_VERSION = 0x42


def _read_reg(spi, cs_pin, reg):
    """Read one SX127x register using SPI full-duplex transaction."""
    tx_buf = bytearray([reg & 0x7F, 0x00])
    rx_buf = bytearray(2)
    cs_pin.value(0)
    spi.write_readinto(tx_buf, rx_buf)
    cs_pin.value(1)
    return rx_buf[1], rx_buf


def _diagnose_spi_link(spi, cs_pin, reset_pin):
    """Probe RF96/SX127x link at multiple baud rates and return best observed version."""
    best_version = None
    baudrates = (100000, 500000, 1000000)

    for baud in baudrates:
        try:
            spi.init(baudrate=baud, polarity=0, phase=0)
            logger.debug(TAG, "SPI probe at {} Hz".format(baud))

            reset_pin.value(0)
            time.sleep_ms(20)
            reset_pin.value(1)
            time.sleep_ms(80)

            reads = []
            for _ in range(5):
                ver, raw = _read_reg(spi, cs_pin, REG_VERSION)
                reads.append(ver)
                logger.debug(TAG, "  REG_VERSION read: 0x{:02X} raw=[0x{:02X},0x{:02X}]".format(
                    ver, raw[0], raw[1]))
                time.sleep_ms(10)

            if 0x12 in reads:
                return 0x12

            unique_reads = sorted(set(reads))
            logger.warn(TAG, "  Unexpected version values at {} Hz: {}".format(
                baud, ["0x{:02X}".format(v) for v in unique_reads]))

            # Keep highest non-trivial observation for final summary.
            non_trivial = [v for v in reads if v not in (0x00, 0xFF)]
            if non_trivial:
                best_version = non_trivial[0]
            elif best_version is None and reads:
                best_version = reads[0]

        except Exception as e:
            logger.warn(TAG, "  SPI probe failed at {} Hz: {}".format(baud, e))

    return best_version


def _diagnose_miso_gpio(Pin, miso_pin):
    """Check whether MISO behaves like a driven line, floating line, or stuck rail."""
    try:
        p_up = Pin(miso_pin, Pin.IN, Pin.PULL_UP)
        time.sleep_ms(2)
        up_val = p_up.value()

        p_down = Pin(miso_pin, Pin.IN, Pin.PULL_DOWN)
        time.sleep_ms(2)
        down_val = p_down.value()

        # Restore to plain input after test.
        Pin(miso_pin, Pin.IN)

        if up_val == 0 and down_val == 0:
            verdict = "stuck_low"
        elif up_val == 1 and down_val == 1:
            verdict = "stuck_high"
        elif up_val == 1 and down_val == 0:
            verdict = "floating_or_weakly_driven"
        else:
            verdict = "unexpected"

        return up_val, down_val, verdict
    except Exception as e:
        logger.warn(TAG, "MISO GPIO diagnostic failed: {}".format(e))
        return None, None, "diag_error"


def init():
    """Initialise the SX1276 LoRa transceiver over SPI."""
    global _sx
    try:
        from machine import SPI, Pin
        from sx127x import SX127x
        import time

        logger.debug(TAG, "Initializing SX127x with SPI{}, CS={}, RESET={}, DIO0={}".format(
            config.LORA_SPI_ID, config.LORA_PIN_CS, config.LORA_PIN_RESET, config.LORA_PIN_DIO0))

        logger.debug(TAG, "Running MISO pull diagnostic on GP{}...".format(config.LORA_PIN_MISO))
        miso_up, miso_down, miso_state = _diagnose_miso_gpio(Pin, config.LORA_PIN_MISO)
        logger.debug(TAG, "MISO pull test: pull-up={}, pull-down={}, verdict={}".format(
            miso_up, miso_down, miso_state))
        
        # Set up SPI bus with explicit pins (alternate SPI0 pins: GP16/18/19)
        spi = SPI(
            config.LORA_SPI_ID,
            baudrate=500000,
            polarity=0,
            phase=0,
            sck=Pin(config.LORA_PIN_SCK),
            mosi=Pin(config.LORA_PIN_MOSI),
            miso=Pin(config.LORA_PIN_MISO),
        )
        
        logger.debug(TAG, "SPI bus created on SCK={}, MOSI={}, MISO={}".format(
            config.LORA_PIN_SCK, config.LORA_PIN_MOSI, config.LORA_PIN_MISO))
        
        # Pre-init GPIO pins
        cs_pin = Pin(config.LORA_PIN_CS, Pin.OUT)
        reset_pin = Pin(config.LORA_PIN_RESET, Pin.OUT)
        dio0_pin = Pin(config.LORA_PIN_DIO0, Pin.IN)

        # Verify we can drive CS and RESET pins locally.
        cs_pin.value(1)
        time.sleep_ms(1)
        cs_hi = cs_pin.value()
        cs_pin.value(0)
        time.sleep_ms(1)
        cs_lo = cs_pin.value()

        reset_pin.value(1)
        time.sleep_ms(1)
        rst_hi = reset_pin.value()
        reset_pin.value(0)
        time.sleep_ms(1)
        rst_lo = reset_pin.value()
        reset_pin.value(1)

        logger.debug(TAG, "Control pin readback: CS(high,low)=({},{}) RESET(high,low)=({},{})".format(
            cs_hi, cs_lo, rst_hi, rst_lo))
        
        # Test MISO readback (coarse indicator only)
        logger.debug(TAG, "Testing SPI MISO line...")
        cs_pin.value(1)  # Deselect
        for i in range(3):
            rx = bytearray(1)
            spi.readinto(rx)  # Read with no data
            logger.debug(TAG, "Idle MISO read {}: 0x{:02X}".format(i, rx[0]))

        logger.debug(TAG, "Probing REG_VERSION (0x42) across baud rates...")
        version = _diagnose_spi_link(spi, cs_pin, reset_pin)
        logger.debug(TAG, "Best observed version: {}".format(
            "None" if version is None else "0x{:02X}".format(version)))

        if version != 0x12:
            if miso_state == "stuck_low":
                logger.warn(TAG, "MISO appears stuck LOW at GPIO level. Check GP16 continuity and shorts to GND.")
            elif miso_state == "stuck_high":
                logger.warn(TAG, "MISO appears stuck HIGH at GPIO level. Check GP16 shorts to 3V3 and CS routing.")
            elif miso_state == "floating_or_weakly_driven":
                logger.warn(TAG, "MISO appears floating/weakly driven. This usually means module not actively driving MISO.")

            if version == 0x00:
                logger.warn(TAG, "Version stuck at 0x00: likely MISO low, CS not reaching module, or module not powered")
            elif version == 0xFF:
                logger.warn(TAG, "Version stuck at 0xFF: likely MISO floating/high or module not selected")
            elif version is None:
                logger.warn(TAG, "Version probe failed: SPI transaction errors")
            else:
                logger.warn(TAG, "Unexpected version 0x{:02X}: chip mismatch or bus corruption".format(version))
            logger.warn(TAG, "Check: 3.3V/GND, CS GP17, SCK GP18, MOSI GP19, MISO GP16, RST GP20, DIO0 GP21")
            _sx = None
            return False

        # Create SX127x instance with pins dictionary
        pins = {
            "ss": config.LORA_PIN_CS,
            "reset": config.LORA_PIN_RESET,
            "dio_0": config.LORA_PIN_DIO0,
        }
        
        # Configure LoRa parameters
        parameters = {
            "frequency": int(config.LORA_FREQ * 1e6),  # Convert MHz to Hz
            "tx_power_level": config.LORA_TX_POWER,
            "signal_bandwidth": config.LORA_BW * 1e3,  # Convert kHz to Hz
            "spreading_factor": config.LORA_SF,
            "coding_rate": config.LORA_CR,
            "preamble_length": 8,
            "implicitHeader": False,
            "sync_word": config.LORA_SYNC_WORD,
            "enable_CRC": True,
        }
        
        _sx = SX127x(spi, pins, parameters)
        
        # Give chip time to fully initialize after reset
        time.sleep_ms(200)

        logger.info(TAG, "LoRa initialised: freq={} MHz, SF={}, BW={} kHz".format(
            config.LORA_FREQ, config.LORA_SF, config.LORA_BW))
        return True
    except Exception as e:
        logger.error(TAG, "LoRa init failed: {}".format(e))
        _sx = None
        return False


def is_available():
    return _sx is not None


async def rx_task(ingress_queue, neighbour_table):
    """Async task: continuously receive LoRa packets and push to ingress queue."""
    import uasyncio as asyncio
    from core.translator import translate_lora_payload

    logger.info(TAG, "LoRa RX task started")
    
    # Put radio in continuous RX mode
    if _sx:
        _sx.receive()
    
    while True:
        try:
            if _sx is None:
                await asyncio.sleep(5)
                continue

            # Check if packet received
            if _sx.receivedPacket():
                data = _sx.readPayload()
                rssi = _sx.packetRssi()
                logger.debug(TAG, "RX: {} bytes, RSSI={}".format(len(data), rssi))

                # Check if it's a Hello message
                hello = parse_hello(data)
                if hello:
                    neighbour_table.update(
                        hello["node_id"],
                        protocols=["LoRa"],
                        rssi=rssi,
                        capabilities=hello.get("capabilities", ["LoRa"]),
                    )
                    continue

                # Translate and enqueue
                pkt = translate_lora_payload(data)
                if pkt:
                    ingress_queue.push(pkt.get("priority", packet.PRIORITY_NORMAL), pkt)

        except Exception as e:
            logger.error(TAG, "RX error: {}".format(e))

        await asyncio.sleep_ms(100)


async def tx_task(egress_queue):
    """Async task: drain the LoRa egress queue and transmit."""
    import uasyncio as asyncio

    logger.info(TAG, "LoRa TX task started")
    while True:
        try:
            if _sx is not None and not egress_queue.is_empty():
                pkt = egress_queue.pop()
                if pkt:
                    data = packet.encode_packet(pkt)
                    _sx.println(data, implicitHeader=False)
                    logger.debug(TAG, "TX: {} bytes to {}".format(len(data), pkt.get("dst", "?")))
                    # Put back in RX mode after transmission
                    _sx.receive()
        except Exception as e:
            logger.error(TAG, "TX error: {}".format(e))

        await asyncio.sleep_ms(200)


async def hello_task(neighbour_table):
    """Async task: periodically broadcast Hello messages via LoRa."""
    import uasyncio as asyncio

    logger.info(TAG, "LoRa Hello task started")
    while True:
        try:
            if _sx is not None:
                hello = create_hello_payload()
                data = json.dumps(hello).encode("utf-8")
                _sx.println(data, implicitHeader=False)
                logger.debug(TAG, "Sent LoRa Hello broadcast")
                # Put back in RX mode after transmission
                _sx.receive()
        except Exception as e:
            logger.error(TAG, "Hello broadcast error: {}".format(e))

        await asyncio.sleep(config.HELLO_INTERVAL)
