import json
import threading
from datetime import datetime

from flask import Flask, jsonify
import paho.mqtt.client as mqtt

# Dashboard MQTT settings
MQTT_BROKER = "192.168.1.9"
MQTT_PORT = 1883
MQTT_USER = ""
MQTT_PASSWORD = ""
MQTT_TOPIC_DATA = "mesh/data/+"
MQTT_TOPIC_LATEST = "mesh/latest/+"

# Web dashboard settings
WEB_HOST = "0.0.0.0"
WEB_PORT = 5050

app = Flask(__name__)
_state_lock = threading.Lock()
_state_by_node = {}
_mqtt_connected = False


HTML_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>MQTT Sensor Dashboard</title>
  <style>
    :root {
      --bg: #f7f5f1;
      --ink: #1e1e1e;
      --accent: #006d77;
      --card: #ffffff;
      --muted: #6b7280;
      --line: #d6d3d1;
    }
    body {
      margin: 0;
      font-family: \"Segoe UI\", Tahoma, Geneva, Verdana, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 10% 10%, #ffedd5 0%, transparent 35%),
        radial-gradient(circle at 90% 20%, #dbeafe 0%, transparent 35%),
        var(--bg);
      min-height: 100vh;
    }
    header {
      padding: 20px;
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(4px);
    }
    h1 {
      margin: 0;
      font-size: 24px;
      letter-spacing: 0.3px;
    }
    .sub {
      color: var(--muted);
      margin-top: 6px;
      font-size: 14px;
    }
    .wrap {
      max-width: 1100px;
      margin: 0 auto;
      padding: 18px;
    }
    .meta {
      margin-bottom: 14px;
      color: var(--muted);
      font-size: 14px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 14px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 8px 20px rgba(0, 0, 0, 0.05);
    }
    .node {
      font-size: 16px;
      font-weight: 700;
      margin-bottom: 10px;
      color: var(--accent);
    }
    .kv {
      display: grid;
      grid-template-columns: 1fr auto;
      row-gap: 6px;
      column-gap: 12px;
      font-size: 14px;
    }
    .k {
      color: var(--muted);
    }
    .empty {
      border: 1px dashed var(--line);
      border-radius: 14px;
      padding: 20px;
      text-align: center;
      color: var(--muted);
      background: #fff;
    }
  </style>
</head>
<body>
  <header>
    <div class=\"wrap\">
      <h1>LoRa MQTT Dashboard</h1>
      <div class=\"sub\">Live topic: mesh/data/+ | Retained topic: mesh/latest/+</div>
    </div>
  </header>

  <div class=\"wrap\">
    <div id=\"meta\" class=\"meta\">Loading...</div>
    <div id=\"grid\" class=\"grid\"></div>
  </div>

  <script>
    function fmt(v, suffix = \"\") {
      if (v === null || v === undefined) return \"-\";
      return `${v}${suffix}`;
    }

    function render(state) {
      const meta = document.getElementById('meta');
      const grid = document.getElementById('grid');
      const nodes = Object.keys(state).sort();

      meta.textContent = `Nodes online: ${nodes.length} | Last refresh: ${new Date().toLocaleTimeString()}`;

      if (!nodes.length) {
        grid.innerHTML = '<div class=\"empty\">No MQTT data yet. Keep bridge running and publishing.</div>';
        return;
      }

      grid.innerHTML = nodes.map((node) => {
        const d = state[node] || {};
        return `
          <div class=\"card\">
            <div class=\"node\">Node ${node}</div>
            <div class=\"kv\">
              <div class=\"k\">Temperature</div><div>${fmt(d.T, ' C')}</div>
              <div class=\"k\">Humidity</div><div>${fmt(d.H, ' %')}</div>
              <div class=\"k\">RSSI</div><div>${fmt(d.rssi, ' dBm')}</div>
              <div class=\"k\">Source topic</div><div>${fmt(d.topic)}</div>
              <div class=\"k\">Last update</div><div>${fmt(d.updated_at)}</div>
            </div>
          </div>
        `;
      }).join('');
    }

    async function refresh() {
      try {
        const res = await fetch('/api/nodes');
        const data = await res.json();
        render(data);
      } catch (e) {
        document.getElementById('meta').textContent = 'Dashboard fetch failed: ' + e;
      }
    }

    refresh();
    setInterval(refresh, 1500);
  </script>
</body>
</html>
"""


def _topic_node(topic, payload_node):
    if payload_node:
        return str(payload_node)
    parts = topic.split("/")
    if len(parts) >= 3:
        return parts[2]
    return "unknown"


def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_csv_sensor(text):
  """Parse strings like 'T:25.3,H:60.5' into numeric values."""
  if not isinstance(text, str):
    return None, None

  temp = None
  hum = None
  try:
    for item in text.split(","):
      if ":" not in item:
        continue
      key, value = item.split(":", 1)
      key = key.strip().upper()
      num = float(value.strip())
      if key in ("T", "TEMP", "TEMPERATURE"):
        temp = num
      elif key in ("H", "HUM", "HUMIDITY"):
        hum = num
  except Exception:
    return None, None

  return temp, hum


def _extract_dashboard_fields(topic, data):
  """Support legacy UART, standard MPR, and raw sensor string payloads."""
  node = _topic_node(topic, data.get("node") or data.get("src"))

  # 1) Legacy shape: {"node":"A","T":..,"H":..,"rssi":..}
  temp = data.get("T")
  hum = data.get("H")
  rssi = data.get("rssi")

  # 2) Standard shape: {"src":"A","data":{"temp":..,"humidity":..}}
  payload = data.get("data") if isinstance(data.get("data"), dict) else None
  if payload is not None:
    if temp is None:
      temp = payload.get("T", payload.get("temp", payload.get("temperature")))
    if hum is None:
      hum = payload.get("H", payload.get("humidity", payload.get("hum")))
    if rssi is None:
      rssi = payload.get("rssi")

    # 3) Raw sensor text inside standard payload: {"data":{"raw":"T:25.3,H:60.5"}}
    if (temp is None or hum is None) and isinstance(payload.get("raw"), str):
      parsed_t, parsed_h = _parse_csv_sensor(payload.get("raw"))
      if temp is None:
        temp = parsed_t
      if hum is None:
        hum = parsed_h

  # 4) Raw sensor text as top-level fallback: {"raw":"T:25.3,H:60.5"}
  if (temp is None or hum is None) and isinstance(data.get("raw"), str):
    parsed_t, parsed_h = _parse_csv_sensor(data.get("raw"))
    if temp is None:
      temp = parsed_t
    if hum is None:
      hum = parsed_h

  return node, temp, hum, rssi


def on_connect(client, userdata, flags, rc):
    global _mqtt_connected
    print("MQTT connect rc=", rc)
    if rc == 0:
        _mqtt_connected = True
        client.subscribe(MQTT_TOPIC_DATA)
        client.subscribe(MQTT_TOPIC_LATEST)
        print("Subscribed:", MQTT_TOPIC_DATA)
        print("Subscribed:", MQTT_TOPIC_LATEST)
    else:
        _mqtt_connected = False
        print("MQTT connect failed with rc=", rc)


def on_disconnect(client, userdata, rc):
    global _mqtt_connected
    _mqtt_connected = False
    print("MQTT disconnected rc=", rc)


def on_message(client, userdata, msg):
    topic = msg.topic if isinstance(msg.topic, str) else msg.topic.decode("utf-8", "ignore")
    payload_text = msg.payload.decode("utf-8", "ignore")

    try:
        data = json.loads(payload_text)
    except json.JSONDecodeError:
        print("Skipping non-JSON payload from", topic)
        return

    node, temp, hum, rssi = _extract_dashboard_fields(topic, data)
    row = {
        "node": node,
      "T": temp,
      "H": hum,
      "rssi": rssi,
        "topic": topic,
        "updated_at": _now_text(),
    }

    with _state_lock:
        _state_by_node[node] = row

    print("MQTT RX", topic, row)


@app.route("/")
def index():
    return HTML_PAGE


@app.route("/api/nodes")
def api_nodes():
    with _state_lock:
        return jsonify(dict(_state_by_node))


@app.route("/api/health")
def api_health():
    return jsonify({
        "mqtt_connected": _mqtt_connected,
        "nodes": len(_state_by_node),
    })


def start_mqtt():
    client = mqtt.Client(client_id="python_dashboard")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=10)

    print("Starting MQTT background connect to {}:{}".format(MQTT_BROKER, MQTT_PORT))
    client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    return client


if __name__ == "__main__":
    mqtt_client = start_mqtt()
    print("Starting dashboard on http://{}:{}".format(WEB_HOST, WEB_PORT))
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)
