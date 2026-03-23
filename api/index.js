const https = require("https");
const http = require("http");
const { URL } = require("url");
const crypto = require("crypto");

const TOKEN = process.env.QUO_SERVER_TOKEN || "";
const API_KEY = process.env.QUO_API_KEY || "";
const BASE_URL = (process.env.QUO_BASE_URL || "https://api.quo.voip/v1").replace(/\/$/, "");

function requireBearer(req, res) {
  const auth = req.headers["authorization"] || "";
  if (!auth.startsWith("Bearer ")) {
    res.status(401).json({ error: "Authorization: Bearer <token> required" });
    return false;
  }
  const token = auth.slice(7);
  if (!TOKEN || !crypto.timingSafeEqual(Buffer.from(token), Buffer.from(TOKEN))) {
    res.status(403).json({ error: "Invalid token" });
    return false;
  }
  return true;
}

function quoGet(path, params) {
  return new Promise((resolve, reject) => {
    const url = new URL(`${BASE_URL}/${path.replace(/^\//, "")}`);
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
    const mod = url.protocol === "https:" ? https : http;
    mod.get(url.toString(), { headers: { Authorization: `Bearer ${API_KEY}`, Accept: "application/json" } }, (resp) => {
      let data = "";
      resp.on("data", (c) => (data += c));
      resp.on("end", () => {
        if (resp.statusCode >= 400) return reject(Object.assign(new Error(data), { status: resp.statusCode }));
        try { resolve(JSON.parse(data)); } catch { resolve(data); }
      });
    }).on("error", reject);
  });
}

module.exports = async function handler(req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (url.pathname === "/health" || url.pathname === "/api/index") {
    return res.status(200).json({ status: "ok", service: "quo-voip-mcp" });
  }

  if (url.pathname === "/call-volume") {
    if (!requireBearer(req, res)) return;
    const today = new Date().toISOString().slice(0, 10);
    const date = url.searchParams.get("date") || today;
    try {
      const data = await quoGet("/calls", {
        from_date: `${date}T00:00:00`,
        to_date: `${date}T23:59:59`,
        page_size: 1,
      });
      return res.status(200).json({ date, call_volume: data.total ?? null });
    } catch (err) {
      return res.status(err.status || 502).json({ error: err.message });
    }
  }

  res.status(404).json({ error: "Not found" });
};
