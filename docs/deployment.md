# Deployment

## Hugging Face Space (what runs today)

The live instance. `SERVER_MODE=1`, a persistent volume for state, and two
optional extras installed non-fatally so a missing one can never stop the
dashboard from booting:

- `openwakeword` — the wake word
- `google-antigravity` — the agent runtime delegation uses

Deploying is per-file through the API rather than `git push`, because the
histories have diverged and `git fetch hf` fails on the acknowledgements.

## Docker

```bash
docker build -t jarvis .
docker run -p 8080:8080 \
  -e SERVER_MODE=1 \
  -e GEMINI_API_KEY=… \
  -e JARVIS_CITY=… \
  -v jarvis-data:/data \
  jarvis
```

## Desktop

```bash
pip install -r requirements.txt
python main.py
```

## Windows client — not built yet

The intent: an installer a non-technical person can run, which pairs with the
Space so the assistant can drive that machine. `devices/pair`, `devices/ping`,
`devices/exec` and `jarvisd.py` exist. The installer and the pairing UI do not.
Tracked in [known-issues.md](known-issues.md).

## Checking a deployment

```bash
curl -s https://<space>/api/health | python -m json.tool
```

Public, no credentials, and it answers the three questions that otherwise look
identical from the outside: is the assistant running, is anyone logged in, and
what failed recently.
