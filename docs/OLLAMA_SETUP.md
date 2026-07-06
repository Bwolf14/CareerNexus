# Hooking a Windows Ollama server into Career Nexus

This guide takes you from a bare Windows PC with a GPU to Career Nexus using
it for AI question generation and match analysis. The target setup:

```
┌─────────────────────────────┐         ┌──────────────────────────────┐
│  Linux server               │  HTTP   │  Windows PC (RTX 4090)       │
│  docker compose:            │ ──────► │  Ollama, listening on        │
│   web  (Flask UI)  :8000    │  :11434 │  0.0.0.0:11434               │
│   db   (MariaDB)   :3306    │         │  model: qwen3:32b (example)  │
└─────────────────────────────┘         └──────────────────────────────┘
```

The web app speaks the **OpenAI-compatible API** (`/v1/chat/completions`),
which Ollama exposes out of the box — the same config also works with
LM Studio (port 1234) or vLLM if you ever switch.

If the PC is off, asleep, or unreachable, **nothing breaks**: the app
detects it (connect timeout, a few seconds) and falls back to the built-in
template questions and heuristic-only ranking.

---

## 1. Install Ollama on the Windows PC

1. Download the installer from <https://ollama.com/download/windows> and run
   it. No admin choices matter here; defaults are fine.
2. Open **PowerShell** and confirm it works:

   ```powershell
   ollama --version
   ```

Ollama runs as a background app (icon in the system tray) and starts with
Windows by default.

## 2. Pull a model

For a 24 GB card like the RTX 4090, good fits (as of writing):

| Model tag           | Size (Q4) | Notes                                          |
| ------------------- | --------- | ---------------------------------------------- |
| `qwen3:32b`         | ~20 GB    | Dense, strong quality, fits fully in VRAM      |
| `qwen3:30b-a3b`     | ~19 GB    | MoE — much faster per token, great interactive |
| `gemma3:27b`        | ~17 GB    | Solid alternative                              |

```powershell
ollama pull qwen3:32b
```

Verify it loads and answers (first run loads the model into VRAM, so give it
20–30 seconds):

```powershell
ollama run qwen3:32b "Say hi in five words."
```

Whatever tag `ollama list` shows is exactly what you'll enter as the
**Model** in the Career Nexus settings page.

> **Thinking models:** Qwen3 "thinks" before answering, which improves
> quality but adds latency. Career Nexus strips the thinking from the output
> automatically. If responses feel slow, try the `30b-a3b` variant — the
> speed difference is dramatic.

## 3. Make Ollama listen on the LAN

By default Ollama only listens on `127.0.0.1` — the Linux server can't reach
it. Set the `OLLAMA_HOST` environment variable to `0.0.0.0`:

**GUI route:** Start → search "environment variables" → *Edit the system
environment variables* → **Environment Variables…** → under *User variables*
click **New…**:

- Variable name: `OLLAMA_HOST`
- Variable value: `0.0.0.0`

**Or in PowerShell:**

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
```

Then **quit Ollama from the system-tray icon and start it again** (or reboot)
so it picks the variable up. Verify it's listening on all interfaces:

```powershell
curl.exe http://localhost:11434/v1/models
netstat -an | findstr 11434     # should show 0.0.0.0:11434 LISTENING
```

## 4. Open the Windows firewall

Allow inbound TCP 11434 — restricted to the **Private** profile so it's only
reachable from your LAN (run PowerShell **as Administrator**):

```powershell
New-NetFirewallRule -DisplayName "Ollama (Career Nexus)" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11434 `
  -Profile Private
```

(GUI route: Windows Defender Firewall → Advanced Settings → Inbound Rules →
New Rule → Port → TCP 11434 → Allow → check *Private* only.)

> If your network shows as **Public** in Windows (common after Wi-Fi setup),
> either change it to Private (Settings → Network → properties → Network
> profile) or add `-Profile Any` — but only on a network you trust.

## 5. Pin the PC's address

The web server needs a stable address for the PC. Either:

- **DHCP reservation** (recommended): in your router's admin page, reserve
  the PC's current IP against its MAC address; or
- **Static IP**: Settings → Network → adapter properties → IP assignment →
  Manual.

Find the current address with `ipconfig` (the IPv4 address, e.g.
`192.168.1.50`).

## 6. Verify from the Linux server

From the machine running docker compose:

```bash
curl http://192.168.1.50:11434/v1/models
```

You should get JSON listing your models. If this works, everything
network-level is done. If it doesn't, see Troubleshooting below — the
problem is always one of: `OLLAMA_HOST` not applied, firewall, or wrong IP.

## 7. Connect it in the web UI

1. Open Career Nexus → **AI settings** (top-right nav), or go straight to
   `http://<server>:8000/settings`.
2. Enter the PC's address: `192.168.1.50:11434` (the `http://` and `/v1` are
   added automatically).
3. Click **Test connection** — you should see the latency and a list of the
   models installed on the PC. Click one to select it.
4. Tick **Enable AI features** and **Save settings**.

From then on, the follow-up questionnaire is written by the model (badge:
*AI interviewer active*) and the career plan gets per-job AI analysis plus an
overall summary. Both pages have a **Regenerate** link if you want a fresh
take, and both fall back to the deterministic versions automatically whenever
the server is unreachable.

Prefer configuring via environment instead of the UI? Set these on the `web`
service in `docker-compose.yml` — they act as defaults until someone saves
the settings page:

```yaml
AI_ENABLED: "1"
AI_BASE_URL: http://192.168.1.50:11434
AI_MODEL: qwen3:32b
```

Settings saved from the UI live in `job_results/ai_settings.json` (mounted
volume, survives container restarts) and take precedence over the env vars.

## 8. Quality-of-life on the PC

- **Keep the model loaded.** Ollama unloads models after 5 minutes idle by
  default, so the first request after a quiet spell pays a 10–30 s load. Set
  `OLLAMA_KEEP_ALIVE` to `1h` (or `-1` for "never unload") the same way you
  set `OLLAMA_HOST`, then restart Ollama.
- **Stop the PC from sleeping.** A sleeping PC looks like a dead server.
  Settings → System → Power → *Screen and sleep* → set "When plugged in, put
  my device to sleep" to **Never** (screen sleep is fine).
- **Gaming/other GPU use:** Ollama shares the GPU. Heavy games will slow
  generation and vice versa; nothing crashes, it's just slower.

## 9. Security notes

- **Ollama has no authentication.** Anyone on your LAN can use the model.
  Never port-forward 11434 to the internet.
- The firewall rule above is Private-profile only — laptops on public Wi-Fi
  won't expose it.
- Career Nexus only ever *reads* from the model (chat completions). It sends
  resume digests and job-posting text as prompt context, so treat the PC as
  part of the same trust domain as the server that stores the resumes.

## Troubleshooting

| Symptom (Test connection says…) | Cause → fix |
| --- | --- |
| *Connection refused or host unreachable* | Ollama not running → start it; `OLLAMA_HOST` not applied → re-check step 3 (did you restart Ollama?); firewall → step 4; wrong IP → `ipconfig` |
| *Timed out* | PC asleep → step 8; wrong subnet/VLAN between server and PC; IP changed → step 5 |
| Works with `curl` on the PC but not from the server | Firewall profile mismatch (network is Public) → step 4 note |
| *Model … not found* when generating | Tag typo — must match `ollama list` exactly (`qwen3:32b`, not `qwen-3-32b`) → pick from the Test-connection list |
| First generation extremely slow, later ones fast | Cold model load → set `OLLAMA_KEEP_ALIVE` (step 8) |
| Questions/analysis show "AI unavailable" but Test connection is green | Response timeout too low for a thinking model → raise the response timeout in AI settings (Timeouts ▸), or use a faster model like `qwen3:30b-a3b` |
| Everything is slow | Another app is using the GPU; or the model spilled to CPU/RAM — check `ollama ps` shows `100% GPU` |

Docker-specific note: if you ever run Ollama **on the same Linux host** as
the containers (instead of the Windows PC), the address to enter is
`host.docker.internal:11434` — the compose file maps that name to the host
gateway.
