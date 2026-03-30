import json
import threading
from datetime import datetime

from flask import Flask, jsonify
import paho.mqtt.client as mqtt

# Dashboard MQTT settings
MQTT_BROKER = "10.196.168.251" # Ensure this matches your laptop's IP
MQTT_PORT = 1883
MQTT_USER = ""
MQTT_PASSWORD = ""
MQTT_TOPIC_DATA = "mesh/data/+"

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
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Mesh Disaster Recovery Dashboard</title>
  <style>
    :root {
      --bg: #121212;
      --ink: #e0e0e0;
      --accent: #4ade80;
      --card: #1e1e1e;
      --muted: #a1a1aa;
      --line: #3f3f46;
    }
    body {
      margin: 0;
      font-family: "Courier New", Courier, monospace; /* Tech presentation vibe */
      color: var(--ink);
      background: var(--bg);
      min-height: 100vh;
    }
    header {
      padding: 20px;
      border-bottom: 1px solid var(--line);
      background: #000;
    }
    h1 { margin: 0; font-size: 24px; color: var(--accent); }
    .sub { color: var(--muted); margin-top: 6px; font-size: 14px; }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 18px; }
    .meta { margin-bottom: 14px; color: var(--muted); font-size: 14px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }
    .card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 18px; }
    .node { font-size: 18px; font-weight: bold; margin-bottom: 12px; color: #60a5fa; border-bottom: 1px solid var(--line); padding-bottom: 8px;}
    .kv { display: grid; grid-template-columns: 1fr auto; row-gap: 8px; column-gap: 12px; font-size: 15px; }
    .k { color: var(--muted); }
    .route { margin-top: 15px; padding: 10px; background: #27272a; border-radius: 4px; font-size: 13px; color: #facc15;}
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <h1>Mesh Disaster Recovery Dashboard</h1>
      <div class="sub">Live telemetry and routing visualization</div>
    </div>
  </header>

  <div class="wrap">
    <div id="meta" class="meta">Loading...</div>
    <div id="grid" class="grid"></div>
  </div>

  <script>
    function fmt(v, suffix = "") { return (v === null || v === undefined) ? "-" : `${v}${suffix}`; }

    function render(state) {
      const meta = document.getElementById('meta');
      const grid = document.getElementById('grid');
      const nodes = Object.keys(state).sort();

      meta.textContent = `Nodes online: ${nodes.length} | Last refresh: ${new Date().toLocaleTimeString()}`;

      if (!nodes.length) {
        grid.innerHTML = '<div style="color:var(--muted)">Waiting for mesh data...</div>';
        return;
      }

      grid.innerHTML = nodes.map((node) => {
        const d = state[node];
        return `
          <div class="card">
            <div class="node">Sensor: ${node}</div>
            <div class="kv">
              <div class="k">Temperature</div><div>${fmt(d.T, ' °C')}</div>
              <div class="k">Humidity</div><div>${fmt(d.H, ' %')}</div>
              <div class="k">Link Quality (RSSI)</div><div>${fmt(d.rssi, ' dBm')}</div>
              <div class="k">Last Update</div><div>${fmt(d.updated_at)}</div>
            </div>
            <div class="route">Last Hop: ${fmt(d.last_hop)} ➔ Dashboard</div>
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
        console.error('Dashboard fetch failed: ' + e);
      }
    }

    refresh();
    setInterval(refresh, 1000); // Faster refresh for live demo
  </script>
</body>
</html>
"""

def _extract_dashboard_fields(data):
    """Extracts data from the standard Mesh routing envelope."""
    # Our translated packet shape
    node = data.get("src", "unknown")
    payload = data.get("data", {})
    last_hop = data.get("hop_dst", "unknown_bridge") # Shows which bridge handed it to MQTT

    # Extract sensor values
    temp = payload.get("temp", payload.get("T"))
    hum = payload.get("humidity", payload.get("H"))
    rssi = payload.get("rssi")

    return node, temp, hum, rssi, last_hop

def on_connect(client, userdata, flags, rc):
    global _mqtt_connected
    if rc == 0:
        _mqtt_connected = True
        client.subscribe(MQTT_TOPIC_DATA)
        print("Connected to MQTT Broker!")
    else:
        print("MQTT connect failed:", rc)

def on_message(client, userdata, msg):
    try:
        payload_text = msg.payload.decode("utf-8")
        data = json.loads(payload_text)
    except Exception:
        return # Ignore garbage data

    node, temp, hum, rssi, last_hop = _extract_dashboard_fields(data)
    
    # Only update if we have valid sensor data
    if temp is not None:
        row = {
            "node": node,
            "T": temp,
            "H": hum,
            "rssi": rssi,
            "last_hop": last_hop,
            "updated_at": datetime.now().strftime("%H:%M:%S")
        }

        with _state_lock:
            _state_by_node[node] = row

        print(f"[{row['updated_at']}] RX from {node} via {last_hop} | T:{temp}C H:{hum}%")

@app.route("/")
def index(): return HTML_PAGE

@app.route("/api/nodes")
def api_nodes():
    with _state_lock: return jsonify(dict(_state_by_node))

def start_mqtt():
    client = mqtt.Client(client_id="python_dashboard")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    return client

if __name__ == "__main__":
    mqtt_client = start_mqtt()
    print(f"Starting dashboard on http://{WEB_HOST}:{WEB_PORT}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)