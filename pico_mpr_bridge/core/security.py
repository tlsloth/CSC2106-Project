try:
    import utime as time
except ImportError:
    import time


def fnv1a32_hex(key):
    h = 2166136261
    for c in key:
        h ^= ord(c)
        h = (h * 16777619) & 0xFFFFFFFF
    return "{:08x}".format(h)


def xor_token_hex(token_hex, key):
    """XOR a hex token string with key bytes; output length matches input."""
    if not token_hex or not key:
        return token_hex
    key_b = [ord(c) for c in key]
    result = ""
    for i in range(0, len(token_hex), 2):
        byte_val = int(token_hex[i:i + 2], 16)
        result += "{:02x}".format(byte_val ^ key_b[(i // 2) % len(key_b)])
    return result


def check_join_auth(msg, expected_network, join_key):
    network = msg.get("network") or msg.get("network_name")
    if network != expected_network:
        return False, "network_mismatch"

    if join_key:
        provided_hash = str(msg.get("auth") or msg.get("key") or "")
        if fnv1a32_hex(join_key) != provided_hash:
            return False, "auth_failed"

    return True, "ok"


def generate_join_token(token_bytes=8, entropy_hint=0):
    if token_bytes < 4:
        token_bytes = 4
    if token_bytes > 24:
        token_bytes = 24

    try:
        import ubinascii
        import urandom

        raw = bytes([urandom.getrandbits(8) for _ in range(token_bytes)])
        return ubinascii.hexlify(raw).decode("utf-8")
    except Exception:
        # Fallback for runtimes without urandom/ubinascii.
        return "{:08x}{:04x}".format(
            int(time.ticks_ms()) & 0xFFFFFFFF,
            int(entropy_hint) & 0xFFFF,
        )


def check_node_token(msg, expected_token, join_key):
    if not expected_token:
        return False, "token_not_issued"

    # Token is XOR-encrypted over the air; decrypt before comparing.
    provided_enc = str(msg.get("token") or "")
    provided = xor_token_hex(provided_enc, join_key)
    if provided != expected_token:
        return False, "token_invalid_or_missing"

    return True, "ok"
