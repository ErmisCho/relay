# Connecting the ElevenLabs agent to a localhost Delegator

*Researched 2026-09-26 for relay (TASK-22/23). Every claim is backed by a source listed at the end, except where marked **unverified**.*

## The core constraint

With **ElevenLabs Agents + custom LLM**, the ElevenLabs cloud calls **our** endpoint, so the connection is inbound. The ElevenLabs docs require a public URL and recommend a tunnel:

> "create a public URL using a tunneling tool like ngrok … Direct your server URL to ngrok endpoint"

So during a call the flow is:

```
mic ─► our Python client (elevenlabs SDK, local) ─websocket─► ElevenLabs cloud (STT, VAD, turn-taking, TTS)
                                                                   │  POST /v1/chat/completions (SSE)
                                                                   ▼
                                               public URL ─tunnel─► localhost:8000 Delegator ─► local Ollama
```

The endpoint must stream **Server-Sent Events** (`Content-Type: text/event-stream`, `data: {json}\n\n`, ending with `data: [DONE]`). Any tunnel we use must therefore pass SSE through unbuffered. This rules out one of the popular free options (see below).

## "Can the ElevenLabs code run locally and only the computation happen in the cloud?"

**Partly, and that is already how it works.** The ElevenLabs *client* code runs locally. `ElevenLabsVoiceSession` uses the Python SDK on this Mac: it owns the mic and speakers and opens an **outbound** websocket to ElevenLabs. Only STT, turn-taking/barge-in and TTS run in their cloud.

What we **cannot** avoid with Agents: the Agents runtime lives only in ElevenLabs' cloud and calls the LLM from there, so our LLM endpoint must be reachable from the internet. Their newer "Speech Engine" product does not change this: ElevenLabs connects **to our websocket** (`"ws_url": "wss://abc123.ngrok.io/ws"`), and the docs again say it "needs a publicly reachable URL". To our knowledge there is no self-hosted ElevenLabs Agents runtime (**unverified**; nothing public found).

## Options

### A. Keep ElevenLabs Agents + custom LLM, expose the Delegator through a tunnel (current design)

| Tunnel | Cost / account | Stable URL | SSE | Limits | Verdict |
|---|---|---|---|---|---|
| **ngrok (free)** | Free, account + authtoken | ✅ one free dev domain `*.ngrok-free.app` | Works in practice; the ElevenLabs guide uses it (**not stated in ngrok's limits page**) | 1 GB/month out, 20,000 HTTP requests/month, 3 endpoints. The interstitial is shown only to *browser* traffic and is skipped by the `ngrok-skip-browser-warning` header or a non-browser User-Agent | **Recommended for the first live test.** It is what ElevenLabs documents. One voice turn ≈ 1 request, so 20k/month is plenty |
| **Tailscale Funnel** | Free on the Personal plan, Tailscale account | ✅ `<machine>.<tailnet>.ts.net` | Should work (TLS-terminating HTTPS proxy) but **unverified**; test with `curl -N` | Ports 443/8443/10000 only; "non-configurable bandwidth limits"; needs MagicDNS + HTTPS certs enabled | **Good long-term free choice**: stable, no request quota |
| **Cloudflare Tunnel, named** | Free, Cloudflare account **plus a domain on Cloudflare** (~$10/yr if you don't have one) | ✅ your own hostname | ✅ HTTP(S) streaming supported | No published metered caps | Best if you own a domain; most robust |
| **Cloudflare Quick Tunnel** (`cloudflared tunnel --url`) | Free, no account | ❌ random each run | ❌ **"Quick Tunnels do not support Server-Sent Events (SSE)"** | 200 in-flight requests, no SLA | **Do not use.** It breaks our SSE stream |
| Pinggy (free) | Free, no signup (ssh) | ❌ random subdomain | **unverified** | 60-minute tunnel timeout | Only for quick throwaway tests |
| VS Code / Microsoft dev tunnels | Free, GitHub/MS account | ✅ per tunnel | **unverified** | 5 GB/user/month, 1,500 req/min/port | Viable alternative |
| localhost.run / localtunnel | Free | ❌ rotates every few hours | **unverified** | Unreliable | Not recommended |

Security with any tunnel: the Delegator is then on the public internet. It already requires `Authorization: Bearer <DELEGATOR_SHARED_SECRET>` and refuses the dev placeholder secret, and `apply_agent_config.py --apply` refuses the dev secret and localhost URLs. Use a long random secret (`openssl rand -hex 32`).

### B. Deploy the Delegator to a cloud host (no tunnel for the Delegator)

The Delegator gets a real public URL (Fly.io, a small VPS, …), but then **Ollama is still local**. Either we tunnel the *other* way (cloud Delegator → home Ollama, same problem in reverse) or we switch the delegator model to a hosted API via `.env`. That contradicts "use local LLMs", so it is not recommended for now.

### C. Drop the Agents platform; use ElevenLabs only for STT + TTS (everything outbound, no tunnel)

Run the conversation loop locally and call ElevenLabs as plain APIs over **outbound** websockets:
- **STT:** Scribe v2 Realtime, websocket, ~150 ms partial latency, with automatic VAD or manual commit. Priced at $0.39/audio-hour realtime (third-party figure as of 2026-08-29).
- **TTS:** ElevenLabs streaming TTS websocket (Flash v2.5).
- **Orchestration:** [Pipecat](https://github.com/pipecat-ai/pipecat) already ships `ElevenLabsSTTService`/`ElevenLabsTTSService` (with interruption handling) and supports Ollama. Our Delegator logic (tools, hooks, commitment protocol) would be called in-process instead of over HTTP.

Pros:
- No public endpoint at all.
- Per-hour STT/TTS pricing instead of Agents' per-minute price (SPEC §9: $0.08/min).
- Matches SPEC §5's "swap the voice layer later", which `VoiceSession` was designed for.

Cons:
- We take over VAD, turn-taking and barge-in quality, the part SPEC §1 says ElevenLabs Agents "removes".
- A new `VoiceSession` implementation is needed (roughly TASK-23-sized).

**Good Phase-2+ option; not needed to start testing.**

### D. ElevenLabs-hosted LLM + a local "client tool"

The ElevenLabs SDK can run *client tools* locally, called over the existing outbound websocket. A single tool could forward to `localhost:8000`, so no tunnel is needed. But the **ElevenLabs LLM** would then decide what to say and when to call us. That bypasses the Delegator as "the LLM", including the scope gate and the commitment protocol's read-back/assent guarantees. **Not recommended.**

## Recommendation

1. **Now:** option A with **ngrok free** (a stable dev domain, and the setup ElevenLabs documents). Fall back to **Tailscale Funnel** if ngrok's quota or interstitial gets in the way.
2. **Never:** Cloudflare Quick Tunnels (no SSE).
3. **Later:** evaluate option C once Phase 1 is validated. It removes the tunnel and cuts voice cost.

### ngrok steps (for TASK-23's live check)

```bash
brew install ngrok
ngrok config add-authtoken <token from dashboard.ngrok.com>
uv run python -m relay.delegator                 # localhost:8000
ngrok http 8000 --url=<your-dev-domain>.ngrok-free.app
curl -N https://<your-dev-domain>.ngrok-free.app/healthz
```

Then set in `.env`: `DELEGATOR_PUBLIC_URL=https://<your-dev-domain>.ngrok-free.app` and a strong `DELEGATOR_SHARED_SECRET`. Run `uv run python scripts/apply_agent_config.py` (dry run), review it, then run `--apply`. This touches your ElevenLabs account.

## Open issue found while researching: which URL form ElevenLabs expects

The docs show handler routes `/v1/chat/completions`, and community guides paste the **full** path (`https://…ngrok-free.app/v1/chat/completions`) into the agent's Server URL. Our `config/elevenlabs/agent.json` currently sends `${DELEGATOR_PUBLIC_URL}/v1`, which follows the OpenAI base-URL convention. The official page does not say which form it uses. **Mitigation (implemented):** the Delegator serves `/v1/chat/completions`, `/chat/completions`, `/v1/v1/chat/completions` and `/v1`, so any interpretation works on the first live call. The first request's path in the Delegator log then tells us which form ElevenLabs uses. How the key is sent (we expect `Authorization: Bearer <secret>`) is likewise undocumented, and the first live request will confirm it.

## Sources

- ElevenLabs, Integrate your own model (custom LLM): https://elevenlabs.io/docs/eleven-agents/customization/llm/custom-llm (also `/docs/agents-platform/customization/llm/custom-llm`)
- ElevenLabs, Speech Engine quickstart: https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-engine/add-voice-to-chat-agent
- ElevenLabs, Scribe v2 Realtime: https://elevenlabs.io/realtime-speech-to-text-api, pricing via https://tokenmix.ai/blog/elevenlabs-scribe-v2-realtime-api-2026 and https://www.therundown.ai/tools/scribe-v2-realtime
- Cloudflare, Quick Tunnels (TryCloudflare) limitations: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/
- Cloudflare, Create a remote tunnel (domain requirement): https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/
- ngrok, Free plan limits: https://ngrok.com/docs/pricing-limits/free-plan-limits/
- Tailscale, Funnel: https://tailscale.com/kb/1223/funnel
- Free developer tunnels comparison (2026): https://merginit.com/blog/19062026-free-developer-tunnels-comparison
- Pipecat ElevenLabs services: https://docs.pipecat.ai/server/services/tts/elevenlabs, https://reference-server.pipecat.ai/en/stable/api/pipecat.services.elevenlabs.stt.html
- Community example (full-path URL + ngrok): https://medium.com/@gagancopvtlimited/create-your-own-ai-voice-assistant-that-knows-everything-about-you-with-elevenlabs-b4baf4745464
