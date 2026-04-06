import json
import threading
from datetime import datetime
import time

from flask import Flask, jsonify
import paho.mqtt.client as mqtt

# Dashboard MQTT settings
MQTT_BROKER = "192.168.137.1" # Ensure this matches your laptop's IP
MQTT_PORT = 1883
MQTT_USER = ""
MQTT_PASSWORD = ""
MQTT_TOPIC_DATA = "mesh/#"
NODE_ID = "dashboard_main"
HELLO_INTERVAL = 15

# Web dashboard settings
WEB_HOST = "0.0.0.0"
WEB_PORT = 5050

app = Flask(__name__)
_state_lock = threading.Lock()
_state_by_node = {}
_topo_lock = threading.Lock()
_topo_by_node = {}          # node_id  -> { neighbours: {…}, updated_at: str }
_mqtt_connected = False


HTML_PAGE = r"""
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
      font-family: "Courier New", Courier, monospace;
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

    /* -- Tabs -- */
    .tabs { display: flex; gap: 0; margin-bottom: 18px; border-bottom: 2px solid var(--line); }
    .tab {
      padding: 10px 22px; cursor: pointer; font-size: 15px; font-family: inherit;
      color: var(--muted); background: none; border: none; border-bottom: 2px solid transparent;
      margin-bottom: -2px; transition: color .2s, border-color .2s;
    }
    .tab:hover { color: var(--ink); }
    .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }

    /* -- Topology canvas -- */
    #topoWrap { position: relative; }
    #topoCanvas {
      width: 100%; height: 600px; background: var(--card); border: 1px solid var(--line);
      border-radius: 8px; display: block;
    }
    #topoTooltip {
      position: absolute; display: none; pointer-events: none;
      background: #27272a; border: 1px solid var(--line); border-radius: 6px;
      padding: 10px 14px; font-size: 13px; color: var(--ink); z-index: 10;
      max-width: 260px;
    }
    #topoTooltip .tt-title { font-weight: bold; margin-bottom: 4px; font-size: 14px; }
    #topoTooltip .tt-row { color: var(--muted); }
    #topoLegend {
      display: flex; gap: 24px; margin-top: 10px; font-size: 13px; color: var(--muted);
    }
    #topoLegend span { display: flex; align-items: center; gap: 6px; }
    .leg-swatch {
      display: inline-block; width: 14px; height: 14px; border-radius: 50%;
    }
    .leg-swatch.bridge { background: #f59e0b; border-radius: 3px; }
    .leg-swatch.node   { background: #60a5fa; }
    .leg-swatch.dash   { background: #4ade80; }
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

    <div class="tabs">
      <button class="tab active" data-tab="telemetry">Telemetry</button>
      <button class="tab" data-tab="topology">Topology</button>
    </div>

    <!-- Telemetry tab -->
    <div id="tab-telemetry" class="tab-content active">
      <div id="grid" class="grid"></div>
    </div>

    <!-- Topology tab -->
    <div id="tab-topology" class="tab-content">
      <div id="topoWrap">
        <canvas id="topoCanvas"></canvas>
        <div id="topoTooltip"></div>
      </div>
      <div id="topoLegend">
        <span><i class="leg-swatch bridge"></i> Bridge</span>
        <span><i class="leg-swatch node"></i> Node</span>
        <span><i class="leg-swatch dash"></i> Dashboard</span>
      </div>
    </div>
  </div>

  <script>
    /* -- Tab switching -- */
    document.querySelectorAll('.tab').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
        if (btn.dataset.tab === 'topology') { resizeCanvas(); drawGraph(); }
      });
    });

    /* -- Telemetry -- */
    function fmt(v, suffix) { suffix = suffix || ""; return (v === null || v === undefined) ? "-" : v + suffix; }

    function renderTelemetry(state) {
      var grid = document.getElementById('grid');
      var nodes = Object.keys(state).sort();
      if (!nodes.length) {
        grid.innerHTML = '<div style="color:var(--muted)">Waiting for mesh data...</div>';
        return;
      }
      grid.innerHTML = nodes.map(function(node) {
        var d = state[node];
        return '<div class="card">' +
          '<div class="node">Sensor: ' + node + '</div>' +
          '<div class="kv">' +
            '<div class="k">Temperature</div><div>' + fmt(d.T, ' \u00b0C') + '</div>' +
            '<div class="k">Humidity</div><div>' + fmt(d.H, ' %') + '</div>' +
            '<div class="k">Link Quality (RSSI)</div><div>' + fmt(d.rssi, ' dBm') + '</div>' +
            '<div class="k">Distance</div><div>' + fmt(d.distance, ' m') + '</div>' +
            '<div class="k">Last Update</div><div>' + fmt(d.updated_at) + '</div>' +
          '</div>' +
          '<div class="route">Last Hop: ' + fmt(d.last_hop) + ' \u27a4 Dashboard</div>' +
        '</div>';
      }).join('');
    }

    /* -- Topology -- */
    var canvas  = document.getElementById('topoCanvas');
    var ctx     = canvas.getContext('2d');
    var tooltip = document.getElementById('topoTooltip');
    var topoNodes = [];
    var topoEdges = [];
    var hoveredNode = null;

    var NODE_RADIUS  = 22;
    var BRIDGE_RADIUS = 28;
    var COLORS = {
      bridge: '#f59e0b',
      node:   '#60a5fa',
      dash:   '#4ade80',
      edge:   '#555555',
      edgeVia:'rgba(245,158,11,0.27)',
      text:   '#e0e0e0',
      muted:  '#a1a1aa'
    };

    function nodeType(id) {
      var lower = id.toLowerCase();
      if (lower.indexOf('bridge') !== -1) return 'bridge';
      if (lower.indexOf('dashboard') !== -1) return 'dash';
      return 'node';
    }
    function nodeColor(id)  { return COLORS[nodeType(id)]; }
    function nodeRadius(id) { return nodeType(id) === 'bridge' ? BRIDGE_RADIUS : NODE_RADIUS; }

    function buildGraph(topo) {
      var nodeMap = {};
      var edgeSet = {};
      var edges   = [];

      Object.keys(topo).forEach(function(reporter) {
        var info = topo[reporter];
        var nb   = info.neighbours || {};
        if (!nodeMap[reporter]) nodeMap[reporter] = { id: reporter, caps: [], protos: [] };

        Object.keys(nb).forEach(function(nid) {
          var meta = nb[nid];
          if (!nodeMap[nid]) nodeMap[nid] = { id: nid, caps: (meta.capabilities || []).slice(), protos: (meta.protocols || []).slice() };
          // Merge capabilities
          (meta.capabilities || []).forEach(function(c) {
            if (nodeMap[nid].caps.indexOf(c) === -1) nodeMap[nid].caps.push(c);
          });
          (meta.protocols || []).forEach(function(p) {
            if (nodeMap[nid].protos.indexOf(p) === -1) nodeMap[nid].protos.push(p);
          });

          if (meta.via) {
            // Indirect: reporter knows nid via a bridge
            var ek1 = [reporter, meta.via].sort().join('|');
            if (!edgeSet[ek1]) {
              edgeSet[ek1] = true;
              edges.push({ from: reporter, to: meta.via, rssi: null, proto: '', via: false });
            }
            var ek2 = [meta.via, nid].sort().join('|');
            if (!edgeSet[ek2]) {
              edgeSet[ek2] = true;
              edges.push({ from: meta.via, to: nid, rssi: meta.rssi, proto: (meta.protocols || [])[0] || '', via: true });
            }
          } else {
            var ek = [reporter, nid].sort().join('|');
            if (!edgeSet[ek]) {
              edgeSet[ek] = true;
              edges.push({ from: reporter, to: nid, rssi: meta.rssi, proto: (meta.protocols || [])[0] || '', via: false });
            }
          }
        });
      });

      // Force-directed layout
      var ids = Object.keys(nodeMap);
      var W = canvas.width, H = canvas.height;
      var positions = {};
      ids.forEach(function(id, i) {
        var angle = (2 * Math.PI * i) / ids.length;
        var r = Math.min(W, H) * 0.32;
        positions[id] = { x: W/2 + r * Math.cos(angle), y: H/2 + r * Math.sin(angle) };
      });

      for (var iter = 0; iter < 300; iter++) {
        var forces = {};
        ids.forEach(function(id) { forces[id] = { x: 0, y: 0 }; });

        // Repulsion
        for (var i = 0; i < ids.length; i++) {
          for (var j = i + 1; j < ids.length; j++) {
            var a = positions[ids[i]], b = positions[ids[j]];
            var dx = a.x - b.x, dy = a.y - b.y;
            var dist = Math.sqrt(dx*dx + dy*dy) || 1;
            var force = 30000 / (dist * dist);
            forces[ids[i]].x += (dx / dist) * force;
            forces[ids[i]].y += (dy / dist) * force;
            forces[ids[j]].x -= (dx / dist) * force;
            forces[ids[j]].y -= (dy / dist) * force;
          }
        }

        // Attraction along edges
        edges.forEach(function(e) {
          var a = positions[e.from], b = positions[e.to];
          if (!a || !b) return;
          var dx = a.x - b.x, dy = a.y - b.y;
          var dist = Math.sqrt(dx*dx + dy*dy) || 1;
          var force = (dist - 200) * 0.03;
          forces[e.from].x -= (dx / dist) * force;
          forces[e.from].y -= (dy / dist) * force;
          forces[e.to].x   += (dx / dist) * force;
          forces[e.to].y   += (dy / dist) * force;
        });

        // Center gravity
        ids.forEach(function(id) {
          forces[id].x += (W/2 - positions[id].x) * 0.005;
          forces[id].y += (H/2 - positions[id].y) * 0.005;
        });

        // Apply with damping
        ids.forEach(function(id) {
          positions[id].x += forces[id].x * 0.85;
          positions[id].y += forces[id].y * 0.85;
          positions[id].x = Math.max(60, Math.min(W - 60, positions[id].x));
          positions[id].y = Math.max(60, Math.min(H - 60, positions[id].y));
        });
      }

      topoNodes = ids.map(function(id) {
        return {
          id: id, type: nodeType(id),
          x: positions[id].x, y: positions[id].y,
          caps: nodeMap[id].caps, protos: nodeMap[id].protos
        };
      });
      topoEdges = edges;
    }

    function drawGraph() {
      var W = canvas.width, H = canvas.height;
      ctx.clearRect(0, 0, W, H);

      if (!topoNodes.length) {
        ctx.fillStyle = COLORS.muted;
        ctx.font = '15px "Courier New", monospace';
        ctx.textAlign = 'center';
        ctx.fillText('Waiting for topology data\u2026', W/2, H/2);
        return;
      }

      var posMap = {};
      topoNodes.forEach(function(n) { posMap[n.id] = n; });

      // Edges
      topoEdges.forEach(function(e) {
        var a = posMap[e.from], b = posMap[e.to];
        if (!a || !b) return;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.strokeStyle = e.via ? COLORS.edgeVia : COLORS.edge;
        ctx.lineWidth = e.via ? 2.5 : 1.5;
        if (e.via) { ctx.setLineDash([6, 4]); } else { ctx.setLineDash([]); }
        ctx.stroke();
        ctx.setLineDash([]);

        // Edge label — offset perpendicular to edge to avoid overlap
        var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        var label = '';
        if (e.proto) label += e.proto;
        if (e.rssi !== null && e.rssi !== 0) label += (label ? ' ' : '') + e.rssi + ' dBm';
        if (label) {
          // Perpendicular offset so parallel edges don't stack
          var edx = b.x - a.x, edy = b.y - a.y;
          var elen = Math.sqrt(edx*edx + edy*edy) || 1;
          var nx = -edy / elen, ny = edx / elen;  // normal
          var off = 14;
          ctx.font = '11px "Courier New", monospace';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          // Dark pill behind text for readability
          var tw = ctx.measureText(label).width + 8;
          ctx.fillStyle = 'rgba(18,18,18,0.85)';
          ctx.beginPath();
          ctx.roundRect(mx + nx*off - tw/2, my + ny*off - 9, tw, 18, 4);
          ctx.fill();
          ctx.fillStyle = '#bbb';
          ctx.fillText(label, mx + nx*off, my + ny*off);
        }
      });

      // Nodes
      topoNodes.forEach(function(n) {
        var r = nodeRadius(n.id);
        var isHovered = hoveredNode && hoveredNode.id === n.id;

        // Glow on hover
        if (isHovered) {
          ctx.beginPath();
          ctx.arc(n.x, n.y, r + 8, 0, 2 * Math.PI);
          ctx.fillStyle = nodeColor(n.id) + '33';
          ctx.fill();
        }

        ctx.beginPath();
        if (n.type === 'bridge') {
          // Hexagon for bridges
          for (var i = 0; i < 6; i++) {
            var angle = Math.PI / 6 + (Math.PI / 3) * i;
            var px = n.x + r * Math.cos(angle);
            var py = n.y + r * Math.sin(angle);
            if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
          }
          ctx.closePath();
        } else {
          ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
        }
        ctx.fillStyle = nodeColor(n.id) + (isHovered ? '' : 'cc');
        ctx.fill();
        ctx.strokeStyle = nodeColor(n.id);
        ctx.lineWidth = 2;
        ctx.stroke();

        // Icon label inside
        ctx.font = 'bold 13px "Courier New", monospace';
        ctx.fillStyle = '#000';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        var icon = n.type === 'bridge' ? 'BR' : (n.type === 'dash' ? 'DB' : 'ND');
        ctx.fillText(icon, n.x, n.y);

        // Name below
        ctx.font = '12px "Courier New", monospace';
        ctx.fillStyle = COLORS.text;
        ctx.textBaseline = 'top';
        ctx.fillText(n.id, n.x, n.y + r + 8);
      });
    }

    // Tooltip on hover
    canvas.addEventListener('mousemove', function(ev) {
      var rect = canvas.getBoundingClientRect();
      var scaleX = canvas.width / rect.width;
      var scaleY = canvas.height / rect.height;
      var mx = (ev.clientX - rect.left) * scaleX;
      var my = (ev.clientY - rect.top)  * scaleY;

      hoveredNode = null;
      for (var i = 0; i < topoNodes.length; i++) {
        var n = topoNodes[i];
        var dx = mx - n.x, dy = my - n.y;
        if (dx*dx + dy*dy < Math.pow(nodeRadius(n.id), 2) + 100) {
          hoveredNode = n;
          break;
        }
      }

      if (hoveredNode) {
        var hn = hoveredNode;
        var wrapRect = canvas.parentElement.getBoundingClientRect();
        tooltip.style.display = 'block';
        tooltip.style.left = (ev.clientX - wrapRect.left + 14) + 'px';
        tooltip.style.top  = (ev.clientY - wrapRect.top  - 10) + 'px';
        tooltip.innerHTML =
          '<div class="tt-title">' + hn.id + '</div>' +
          '<div class="tt-row">Type: ' + hn.type + '</div>' +
          (hn.caps.length  ? '<div class="tt-row">Capabilities: ' + hn.caps.join(', ')  + '</div>' : '') +
          (hn.protos.length ? '<div class="tt-row">Protocols: '   + hn.protos.join(', ') + '</div>' : '');
      } else {
        tooltip.style.display = 'none';
      }
      drawGraph();
    });

    canvas.addEventListener('mouseleave', function() {
      hoveredNode = null;
      tooltip.style.display = 'none';
      drawGraph();
    });

    function resizeCanvas() {
      var wrap = canvas.parentElement;
      canvas.width  = wrap.clientWidth;
      canvas.height = 600;
    }
    window.addEventListener('resize', function() { resizeCanvas(); drawGraph(); });

    /* -- Refresh loop -- */
    async function refresh() {
      try {
        var responses = await Promise.all([
          fetch('/api/nodes'),
          fetch('/api/topology')
        ]);
        var nodesData = await responses[0].json();
        var topoData  = await responses[1].json();

        var allIds = {};
        Object.keys(nodesData).forEach(function(k) { allIds[k] = true; });
        Object.keys(topoData).forEach(function(k) { allIds[k] = true; });

        document.getElementById('meta').textContent =
          'Nodes online: ' + Object.keys(allIds).length +
          ' | Last refresh: ' + new Date().toLocaleTimeString();

        renderTelemetry(nodesData);

        if (Object.keys(topoData).length) {
          resizeCanvas();
          buildGraph(topoData);
          drawGraph();
        }
      } catch (e) {
        console.error('Dashboard fetch failed: ' + e);
      }
    }

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


def _extract_dashboard_fields(data):
    """Extracts data from the standard Mesh routing envelope."""
    node = data.get("node_id", "unknown")
    payload = data.get("payload", {})
    last_hop = data.get("hop_dst", "unknown_bridge")
    temp = payload.get("temp", payload.get("T"))
    hum = payload.get("hum", payload.get("H"))
    distance = payload.get("distance", None)
    rssi = data.get("rssi")
    return node, temp, hum, rssi, last_hop, distance


def _is_topology_msg(data):
    """Return True if the MQTT message is a neighbour-table advertisement."""
    return "neighbours" in data and "node_id" in data


def on_connect(client, userdata, flags, rc, properties=None):
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
        print(f"MQTT RX on {msg.topic}: {payload_text}")
    except Exception:
        return  # Ignore garbage data

    # -- Topology advertisement --
    if _is_topology_msg(data):
        reporter = data["node_id"]
        with _topo_lock:
            _topo_by_node[reporter] = {
                "neighbours": data["neighbours"],
                "updated_at": datetime.now().strftime("%H:%M:%S"),
                "_ts": time.time(),
            }
        print(f"[TOPO] Updated neighbour table from {reporter} "
              f"({len(data['neighbours'])} neighbours)")
        return  # topology msgs don't carry sensor data

    # -- Sensor telemetry --
    node, temp, hum, rssi, last_hop, distance = _extract_dashboard_fields(data)

    if temp is not None or distance is not None:
        row = {
            "node": node,
            "T": temp,
            "H": hum,
            "distance": distance,
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
    with _state_lock:
        return jsonify(dict(_state_by_node))

@app.route("/api/topology")
def api_topology():
    # Prune topology entries from bridges that haven't reported in 3× HELLO_INTERVAL
    stale_threshold = HELLO_INTERVAL * 3
    now = time.time()
    with _topo_lock:
        stale = [k for k, v in _topo_by_node.items() if now - v.get("_ts", now) > stale_threshold]
        for k in stale:
            print(f"[TOPO] Pruning stale topology from {k}")
            del _topo_by_node[k]
        return jsonify(dict(_topo_by_node))


def dashboard_hello_loop(client):
    while True:
        if _mqtt_connected:
            hello_payload = {
                "type": "hello",
                "node_id": NODE_ID,
                "protocols":["MQTT"],
                "capabilities":["MQTT"]
            }
            try:
                client.publish("mesh/hello", json.dumps(hello_payload))
                print(f"Broadcasted Hello from {NODE_ID}")
            except Exception as e:
                print(f"Failed to send hello: {e}")
        time.sleep(15)


def start_mqtt():
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=NODE_ID)
    except AttributeError:
        client = mqtt.Client(client_id=NODE_ID)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    hello_thread = threading.Thread(target=dashboard_hello_loop, args=(client,), daemon=True)
    hello_thread.start()
    return client


if __name__ == "__main__":
    mqtt_client = start_mqtt()
    print(f"Starting dashboard on http://{WEB_HOST}:{WEB_PORT}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=True, use_reloader=False)
