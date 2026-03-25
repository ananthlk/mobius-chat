# Credentialing UI: default composer + envelope (v1)

## Goals

- **Default chat** includes upload and **agentic / non-agentic** (full run vs step-by-step) for the normal composer—no separate “credentialing-only” strip as the primary UX.
- **Envelope (modal / in-thread card)** for credentialing / Medicaid NPI **report** flows: **always shown for now** (v1 simplification). Later we may gate on “incomplete request” only.
- **Inference:** When org name (and optionally other fields) are **inferrable** from the user message, **acknowledge** and **prepopulate** the envelope form, still **show** the envelope so the user can confirm or edit.
- When **not inferrable**, show the same envelope with **empty / required** fields and ask the user to set them before Run.

## v1 behavior (locked)

| Rule | Behavior |
|------|----------|
| Envelope visibility | **Always** show the credentialing envelope when the user intent is a credentialing / Medicaid NPI **report build** (same class of messages that today route to `run_credentialing_report`). **No** “only if incomplete” gate in v1. |
| Prepopulation | If **inferrable** (e.g. org name from “report for David Lawrence Center”), **prepopulate** the form and show it—user confirms or edits. |
| Missing info | If **not inferrable**, show the envelope and **ask** the user to fill org (and any other required fields). |
| Defaults button | Optional: **Use defaults** → autopilot + outside-in + no refresh (product default); still can show envelope for confirmation in v1. |

## Envelope fields (conceptual)

- **Organization** — text; prepopulated when parsed from message.
- **Run style** — agentic (full) vs non-agentic (step-by-step / copilot); align with `run_credentialing_report` `mode`.
- **Roster choice** — outside-in vs uploaded roster / reconciliation path.
- **Refresh** — refresh Florida Medicaid / PML before run vs use current data.

## Client ↔ server (future implementation)

- **Thread- or turn-scoped** `credentialing_options` after user confirms the envelope.
- **POST /chat** (or follow-up message) should carry structured choices so the worker does not rely on free text alone.

## Related code

- Tool: `run_credentialing_report` (`autopilot` | `copilot`) — `mobius-chat/app/pipeline/react_loop.py`
- Manifest: `mobius-chat/app/pipeline/tool_manifest.py`
- Envelope types: `assistant_envelope` / `pipeline_human_gate` — `mobius-chat/frontend/src/app.ts`

## Changelog

- **2025-03-24:** Initial spec — default composer upload + agentic toggle; envelope always for credentialing report intent; prepopulate when inferrable.
