# Token Usage and Cost Report

Generated automatically by `code/main.py` at 2026-10-25T07:37:05Z for the run that wrote `output.csv` (518 requests).

Provider: **Google Gemini API** (REST `generateContent`, free tier). LLM work per run: request-intent classification (unique texts, batched 75 per call) and one vision extraction per screenshot/PDF. All evidence parsing, identity resolution, policy decisions and step ordering are deterministic code and use no tokens.

| Model | API calls | Cache replays | Input tokens | Output tokens | Total tokens |
|---|---:|---:|---:|---:|---:|
| gemini-3.5-flash | 0 | 8 | 12,384 | 18,555 | 30,939 |
| gemini-3.5-flash-lite | 0 | 12 | 29,393 | 16,050 | 45,443 |
| **Overall** | **0** | **20** | **41,777** | **34,605** | **76,382** |

## Per-request averages

- Model calls (fresh + replayed) per request: 0.039
- Average tokens per request: 147.5
- Average input / output tokens per request: 80.7 / 66.8

## Cost

- Billed cost of this run: **$0.0000** (free tier; rates used: $0.0/M input, $0.0/M output).
- Per request: $0.000000
- To estimate paid-tier cost, set `GEMINI_PRICE_IN_PER_M` / `GEMINI_PRICE_OUT_PER_M` to the published rates for the model and re-run; the token counts above are exact values reported by the API (`usageMetadata`, thinking tokens counted as output).

Token counts for cache replays are the counts recorded when the response was first generated, so the table reflects the full LLM work behind the output even when a re-run is served from cache.
