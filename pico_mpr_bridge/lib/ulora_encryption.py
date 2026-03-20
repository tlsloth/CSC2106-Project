# ulora_encryption.py
# Minimal compatibility shim for uLoRa test usage.
# Provides the AES interface expected by ulora.py.


class AES:
    def __init__(self, dev_addr, app_key, net_key, frame_counter):
        self.dev_addr = dev_addr
        self.app_key = app_key
        self.net_key = net_key
        self.frame_counter = frame_counter

    def encrypt(self, payload):
        # Compatibility behavior for smoke tests: return payload unchanged.
        return payload

    def calculate_mic(self, lora_pkt, lora_pkt_len, mic):
        # Compatibility behavior for smoke tests: deterministic 4-byte MIC.
        # Replace with full LoRaWAN MIC implementation for production uplinks.
        mic[0] = 0x00
        mic[1] = 0x00
        mic[2] = 0x00
        mic[3] = 0x00
        return mic
