/**
 * Vercel serverless entry point for the OpenPhone connector.
 *
 * Endpoints:
 *   GET /health                           → liveness check
 *   GET /call-volume?date=YYYY-MM-DD      → total calls for a date (requires Bearer auth)
 *
 * Environment variables:
 *   QUO_API_KEY                 OpenPhone API key (sent as raw Authorization header)
 *   QUO_SERVER_TOKEN            Bearer token to protect this endpoint
 *   OPENPHONE_PHONE_NUMBER_ID   PN... phone number ID (required by OpenPhone /v1/calls)
 *   OPENPHONE_PARTICIPANT       (optional) E.164 number to filter calls by a specific party
 *   QUO_BASE_URL                Override base URL (default: https://api.openphone.com)
 */

const https = require("https");
const http = require("http");
const { URL } = require("url");
const crypto = require("crypto");

const API_KEY    = process.env.QUO_API_KEY || "";
const TOKEN      = process.env.QUO_SERVER_TOKEN || "";
const PN_ID      = process.env.OPENPHONE_PHONE_NUMBER_ID || "";
const PARTICIPANT= process.env.OPENPHONE_PARTICIPANT || "";
const BASE_URL   = (process.env.QUO_BASE_URL || "https://api.openphone.com").replace(/\/$/, "");

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

function openphoneGet(path, params) {
  return new Promise((resolve, reject) => {
    const url = new URL(`${BASE_URL}${path}`);
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
    const mod = url.protocol === "https:" ? https : http;
    const req = mod.get(
      url.toString(),
      // OpenPhone uses a raw API key — no "Bearer" prefix
      { headers: { Authorization: API_KEY, Accept: "application/json" } },
      (resp) => {
        let data = "";
        resp.on("data", (c) => (data += c));
        resp.on("end", () => {
          if (resp.statusCode >= 400) {
            return reject(Object.assign(new Error(data), { status: resp.statusCode }));
          }
          try { resolve(JSON.parse(data)); } catch { resolve(data); }
        });
      }
    );
    req.on("error", reject);
  });
}

module.exports = async function handler(req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`);

  // ── /health ───────────────────────────────────────────────────────────────
  if (url.pathname === "/health" || url.pathname === "/api/index") {
    return res.status(200).json({ status: "ok", service: "openphone-mcp" });
  }

  // ── /calls-detail (temporary) ────────────────────────────────────────────
  if (url.pathname === "/calls-detail") {
    if (!requireBearer(req, res)) return;
    try {
      const today = new Date().toISOString().slice(0, 10);
      const date  = url.searchParams.get("date") || today;
      // participant query param overrides env var; pass "all" to fetch without filter
      const participantParam = url.searchParams.get("participant");
      const participant = (participantParam === null) ? PARTICIPANT : (participantParam === "all" ? "" : participantParam);
      // Try all known phone number IDs
      const phoneIds = [PN_ID, "PNiWNztXf7"].filter(Boolean);
      let allCalls = [];
      for (const phoneId of phoneIds) {
        const callParams = {
          phoneNumberId: phoneId,
          maxResults: 100,
          createdAfter:  `${date}T00:00:00.000Z`,
          createdBefore: `${date}T23:59:59.999Z`,
        };
        if (participant) callParams.participants = participant;
        try {
          const r = await openphoneGet("/v1/calls", callParams);
          allCalls = allCalls.concat(r.data || []);
        } catch (e) { /* skip if this number errors */ }
      }
      const calls = { data: allCalls };
      const calls = await openphoneGet("/v1/calls", callParams);
      // Fetch transcript + summary for each call in parallel
      const enriched = await Promise.all((calls.data || []).map(async (call) => {
        const [transcript, summary] = await Promise.allSettled([
          openphoneGet(`/v1/call-transcripts/${call.id}`, {}),
          openphoneGet(`/v1/call-summaries/${call.id}`, {}),
        ]);
        return {
          id: call.id,
          direction: call.direction,
          status: call.status,
          duration: call.duration,
          createdAt: call.createdAt,
          participants: call.participants,
          transcript: transcript.status === "fulfilled" ? transcript.value?.data : null,
          summary:    summary.status    === "fulfilled" ? summary.value?.data    : null,
        };
      }));
      return res.status(200).json({ date, calls: enriched });
    } catch (err) {
      return res.status(err.status || 502).json({ error: err.message });
    }
  }

  // ── /call-volume ──────────────────────────────────────────────────────────
  if (url.pathname === "/call-volume") {
    if (!requireBearer(req, res)) return;

    if (!PN_ID) {
      return res.status(500).json({
        error: "OPENPHONE_PHONE_NUMBER_ID env var is required",
      });
    }

    const today = new Date().toISOString().slice(0, 10);
    const date  = url.searchParams.get("date") || today;

    // Build ISO 8601 bounds for the requested date (UTC)
    const createdAfter  = `${date}T00:00:00.000Z`;
    const createdBefore = `${date}T23:59:59.999Z`;

    try {
      // Fetch with maxResults=1 to get totalItems cheaply.
      // participants is optional — omit it to count all calls for the phone number.
      const params = { phoneNumberId: PN_ID, maxResults: 1, createdAfter, createdBefore };
      if (PARTICIPANT) params.participants = PARTICIPANT;
      const data = await openphoneGet("/v1/calls", params);
      return res.status(200).json({
        date,
        call_volume: data.totalItems ?? null,
      });
    } catch (err) {
      return res.status(err.status || 502).json({ error: err.message });
    }
  }

  res.status(404).json({ error: "Not found" });
};
