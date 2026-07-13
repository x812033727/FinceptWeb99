# LLM Gateway (subscription providers)

Host-side sidecar that lets the containerized FinceptWeb99 backend use
the **subscription** CLIs — Claude Max, Codex, Antigravity — instead of
metered API keys. Credentials stay on the host; the container never
sees them.

```
 backend container ──(OpenAI-compat SSE, Bearer)──▶ llm-gateway :8799 (host)
   ai.llm_router._gateway_stream                      ├ claude_sub  (claude-agent-sdk → ~/.claude/.credentials.json)
   providers claude_sub / codex_sub / agy             ├ codex_sub   (codex exec --json → ~/.codex/auth.json)
                                                       └ agy         (agy -p → Google OAuth)
```

## Deploy (one-time)

```bash
sudo mkdir -p /opt/llm-gateway
sudo cp gateway/{app.py,providers.py,requirements.txt} /opt/llm-gateway/
python3 -m venv /opt/llm-gateway/venv
/opt/llm-gateway/venv/bin/pip install -r /opt/llm-gateway/requirements.txt

# systemd unit is templated on the shared token (systemd %i):
TOKEN=$(openssl rand -hex 24)
sudo cp gateway/llm-gateway.service /etc/systemd/system/llm-gateway@.service
sudo systemctl daemon-reload
sudo systemctl enable --now "llm-gateway@${TOKEN}"

# point the backend at it (.env), then redeploy the backend container:
#   LLM_GATEWAY_URL=http://host-gateway:8799
#   LLM_GATEWAY_TOKEN=<same TOKEN>
#   AI_FALLBACK_TO_API=true
# docker-compose.yml backend service already gets extra_hosts host-gateway.

curl -s http://172.17.0.1:8799/health   # {"ok":true,...}
```

## Ti coexistence (IMPORTANT)

The same Claude Max accounts power Ti's autopilot (host, 24/7) and this
gateway. They deliberately run in **shared-account mode**: the unit's
`HOME=/root` means every request spawns a fresh CLI that reads the live
`/root/.claude/.credentials.json` — the exact file Ti's dual-account
rotation (`/opt/ti/studio/claude_accounts.py switch()`) rewrites. When
Ti switches A↔B (auto-rotate or manual pin), the gateway's very next
request uses the new account. No restart, no config: Ti's settings
panel is the single control point for both projects.

Consequences to keep in mind:

- **Shared quota**: fincept traffic draws down the same 5h/7d windows
  as Ti. Ti's rotation reacts to that usage and switches accounts for
  both. The container-side brake is `AI_AUTO_UPGRADE_TO_SUB=false`
  (stops keyless anthropic/claude_agent traffic from auto-routing
  here); `AI_FALLBACK_TO_API=true` covers exhaustion by dropping to
  the API key when one is configured.
- **Do NOT point this unit's `HOME` at a separate credential profile**
  — that would fork it off Ti's rotation and reintroduce split-brain
  account state.
- **Token-refresh race (rare)**: Ti's long-lived SDK and this gateway's
  per-request CLIs share the live file; near-simultaneous OAuth
  refreshes can invalidate one side's single-use refreshToken for one
  request. The failed call surfaces as a gateway error and is absorbed
  by `AI_FALLBACK_TO_API` / retry.
- **Never set `ANTHROPIC_API_KEY` in this unit** — it would silently
  switch Claude to API billing (the guard in `providers.py` unsets it
  defensively and logs a warning).

## Scope

R2a ships **text-only** streaming. The fincept in-process tool loop (18
tools) reaches these providers in R4 via a reverse HTTP callback
(`POST /internal/tools/{name}` on the backend). codex_sub / agy stay
text-only.
