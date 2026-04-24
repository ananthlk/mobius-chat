# Model profile baseline — 2026-04-24 (rev 00045, SHA e86bf14)

Source: `scripts/bench_chat_e2e.py` against
https://mobius-chat-ortabkknqa-uc.a.run.app, 9-question standard suite,
copilot mode, one run per profile, run sequentially same session.

| Profile   | Pass | Fail | p50      | p95      | fc p50 | fc p95 | rounds avg |
|-----------|-----:|-----:|---------:|---------:|-------:|-------:|-----------:|
| optimal   |    9 |    0 | 15.9s    | 32.9s    |  60 ms |  71 ms |       2.44 |
| default   |    8 |    1 | 18.2s    | 41.2s    |  56 ms |  85 ms |       2.62 |
| gemini    |    7 |    2 | 24.0s    | 39.9s    |  60 ms |  72 ms |       2.43 |
| anthropic |    9 |    0 | 27.0s    | 45.0s    |  54 ms | 106 ms |       2.33 |

**Demo default: `optimal`** — fastest and most reliable; fallback is
`gemini-2.5-flash` so any missing pin degrades gracefully.

**Follow-ups:**
1. `gemini` pure-Vertex dropped 2 turns — investigate Flash at round-4
   and integrator on long context (throttle vs. timeout).
2. `anthropic` is slow because Sonnet is pinned at the integrator —
   swap to Haiku for integrator if we need a faster "Anthropic-only"
   variant.
3. Per-turn raw data saved alongside this file:
   `bench_default.json`, `bench_optimal.json`, `bench_gemini.json`,
   `bench_anthropic.json`.
