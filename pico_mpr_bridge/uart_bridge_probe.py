import time

import config


def main():
    from machine import Pin, UART

    uart = UART(
        config.UART_LORA_ID,
        baudrate=config.UART_LORA_BAUD,
        tx=Pin(config.UART_LORA_TX_PIN),
        rx=Pin(config.UART_LORA_RX_PIN),
        timeout=config.UART_LORA_TIMEOUT_MS,
    )

    print("=== UART LoRa Bridge Probe ===")
    print("UART{} TX=GP{} RX=GP{} @ {}".format(
        config.UART_LORA_ID,
        config.UART_LORA_TX_PIN,
        config.UART_LORA_RX_PIN,
        config.UART_LORA_BAUD,
    ))

    status_count = 0
    rx_count = 0
    err_count = 0
    unknown_count = 0

    deadline = time.ticks_add(time.ticks_ms(), 60000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if uart.any():
            raw = uart.readline()
            if not raw:
                time.sleep_ms(20)
                continue

            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                time.sleep_ms(20)
                continue

            print(line)
            if line.startswith("LORA_STATUS|"):
                status_count += 1
            elif line.startswith("LORA_RX|"):
                rx_count += 1
            elif line.startswith("LORA_ERR|"):
                err_count += 1
            else:
                unknown_count += 1

        time.sleep_ms(20)

    print("--- Summary ---")
    print("status lines:", status_count)
    print("rx lines:", rx_count)
    print("error lines:", err_count)
    print("unknown lines:", unknown_count)


main()
