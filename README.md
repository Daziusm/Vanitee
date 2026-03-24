<p align="center">
  <img src="assets/vanitee.png" width="160" alt="Vanitee">
</p>

# Vanitee

Discord vanity-code checker. No API bypass — just straightforward requests within Discord's rate limits.

## Setup

```bash
pip install -r requirements.txt
python vanitee.py wordlist.txt
```

Requires Python 3.10+.

## What it does

Give it a wordlist (one code per line) and it checks each one against Discord's vanity URL endpoint. Anything available gets written to `hits.txt` and optionally pinged to a Discord webhook.

If you get rate limited it waits once, retries, and moves on — the skipped codes aren't marked as done so a resume run catches them later.

## Proxies

Put proxies in a text file (see `proxies_example.txt`) and point `proxy_file` at it. Vanitee runs a health check first so dead proxies get filtered out before the run starts. Each working proxy gets its own worker and rate limiter.

## Multiple windows

Enable "Multiple windows" in the config screen and you can run several instances on the same wordlist without them stepping on each other.

## Config

Settings are saved to `config.json`. Edit it in any text editor — useful for pasting webhook URLs without having to type them out in the TUI.

| Field | Default | Notes |
|---|---|---|
| `rps` | `2.0` | Requests per second per proxy (or direct) |
| `output` | `hits.txt` | Where hits get written |
| `proxy_file` | `null` | Path to proxy list |
| `webhook_url` | `""` | Discord webhook for hit notifications |
| `webhook_max_len` | `0` | Only notify for codes ≤ this length (0 = all) |
| `coordinate` | `false` | Multi-window no-overlap mode |

## License

MIT
