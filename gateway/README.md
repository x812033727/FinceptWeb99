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
gateway. To avoid both draining one account:

- **Account split**: Ti pins account A (`~/.claude/.credentials.pin` =
  A); the gateway inherits whichever account is live. Prefer pinning the
  gateway's `HOME` to a profile biased toward B, or run the gateway
  under a HOME whose `.credentials.active` = B.
- **Quota gate** (future): share Ti's provider-quota state dir so both
  see the same "account limited until reset" flag. Until then the
  backend's `AI_FALLBACK_TO_API=true` covers exhaustion by dropping to
  the API key.
- **Never set `ANTHROPIC_API_KEY` in this unit** — it would silently
  switch Claude to API billing (the guard in `providers.py` unsets it
  defensively and logs a warning).

## Scope

R2a ships **text-only** streaming. The fincept in-process tool loop (18
tools) reaches these providers in R4 via a reverse HTTP callback
(`POST /internal/tools/{name}` on the backend). codex_sub / agy stay
text-only.
