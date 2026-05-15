// worker.js — Cloudflare Durable Object SPL Relay
export class SplRelay {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.sessions = new Set();     // Dashboard clients
    this.readings = [];            // Rolling buffer (last 5 min @ 1Hz)
  }

  async fetch(request) {
    const url = new URL(request.url);

    // ── WebSocket Upgrade ──────────────────────────────────────────────
    if (request.headers.get("Upgrade") === "websocket") {
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      server.accept();
      this.sessions.add(server);

      server.addEventListener("message", async (msg) => {
        try {
          const data = JSON.parse(msg.data);

          // Validate publisher API key
          if (data.apiKey !== this.env.API_KEY) {
            server.send(JSON.stringify({ type: "error", message: "Invalid API key" }));
            return;
          }

          // Store reading (publisher path)
          const reading = {
            timestamp: data.timestamp,
            spl: data.spl,
            leq60: data.leq60,
            peak: data.peak,
            weighting: data.weighting,
            device: data.device
          };

          this.readings.push(reading);
          if (this.readings.length > 300) this.readings.shift(); // 5 minutes

          // Broadcast to all dashboard subscribers
          const broadcast = JSON.stringify({ type: "reading", data: reading });
          this.sessions.forEach((ws) => {
            if (ws.readyState === 1) ws.send(broadcast);
          });

        } catch (err) {
          server.send(JSON.stringify({ type: "error", message: err.message }));
        }
      });

      server.addEventListener("close", () => {
        this.sessions.delete(server);
      });

      // Send historical buffer to new dashboard client immediately
      server.send(JSON.stringify({ type: "history", data: this.readings }));

      return new Response(null, { status: 101, webSocket: client });
    }

    // ── HTTP Fallback API ──────────────────────────────────────────────
    if (url.pathname === "/api/readings") {
      return new Response(JSON.stringify(this.readings), {
        headers: {
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*"
        }
      });
    }

    return new Response("Not Found", { status: 404 });
  }
}

// Worker entrypoint routes all requests to the single Durable Object instance
export default {
  async fetch(request, env, ctx) {
    const id = env.SPL_RELAY.idFromName("primary");
    const relay = env.SPL_RELAY.get(id);
    return relay.fetch(request);
  }
};
