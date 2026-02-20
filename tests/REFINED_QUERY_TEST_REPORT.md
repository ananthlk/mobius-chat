# Refined Query Testing Report

**Date:** 2025-02-19  
**Scope:** `app/state/refined_query.py`, worker integration, `DEFAULT_STATE`

---

## Executive Summary

| Category | Total | Passed | Failed |
|----------|-------|--------|--------|
| Refined Query Unit Tests | 38 | 38 | 0 |
| Related (Short-term Memory) | 6 | 6 | 0 |
| **Total** | **44** | **44** | **0** |

All tests passed.

---

## 1. `classify_message` Tests

Classifies user message as `slot_fill` (same question + context) vs `new_question` (different question).

| Test | Input | Expected | Output | Result |
|------|-------|----------|--------|--------|
| test_payer_answer_with_open_slots | "Sunshine Health", open_slots=["jurisdiction.payor"] | slot_fill | slot_fill | **PASS** |
| test_payer_answer_slots_already_cleared_fallback | "Sunshine Health", no open_slots, existing_refined | slot_fill | slot_fill | **PASS** |
| test_state_answer_with_open_slots | "Florida", open_slots=["jurisdiction.state"] | slot_fill | slot_fill | **PASS** |
| test_medicaid_answer_with_open_slots | "Medicaid", open_slots=["jurisdiction.program"] | slot_fill | slot_fill | **PASS** |
| test_as_a_provider_with_open_slots | "as a provider", open_slots | slot_fill | slot_fill | **PASS** |
| test_explicit_same | "that one", open_slots | slot_fill | slot_fill | **PASS** |
| test_united_healthcare_answer | "United Healthcare", open_slots | slot_fill | slot_fill | **PASS** |
| test_short_yes_with_open_slots | "yes", open_slots | slot_fill | slot_fill | **PASS** |
| test_full_new_question_how_do_i | "how do I check eligibility" | new_question | new_question | **PASS** |
| test_full_new_question_what_is | "what is the prior auth process for Sunshine" | new_question | new_question | **PASS** |
| test_what_about_indicates_new | "what about eligibility for prior auth" | new_question | new_question | **PASS** |
| test_different_question_phrase | "different question - how do I check status" | new_question | new_question | **PASS** |
| test_first_message_no_context | "how do I file an appeal", no last_turn | new_question | new_question | **PASS** |
| test_empty_text | "" | new_question | new_question | **PASS** |
| test_whitespace_only | "   " | new_question | new_question | **PASS** |
| test_long_question_with_question_mark | "is there anything else I need to know?" | new_question | new_question | **PASS** |
| test_sunshine_health_two_words_slot_fill | "Sunshine Health", 2 words, existing query | slot_fill | slot_fill | **PASS** |
| test_short_slot_like_without_existing_query | "Florida", no existing_refined | new_question | new_question | **PASS** |

---

## 2. `build_refined_query` Tests

Merges jurisdiction into base query (e.g., "how do I file an appeal" + Sunshine Health → "how do I file an appeal for Sunshine Health").

| Test | Input | Expected | Output | Result |
|------|-------|----------|--------|--------|
| test_payor_only | base="how do I file an appeal", j={payor:"Sunshine Health"} | "how do I file an appeal for Sunshine Health" | ✓ | **PASS** |
| test_payor_and_state | base + j with payor and state | contains both payor and state | ✓ | **PASS** |
| test_full_jurisdiction | base + j with payor, state, program | contains jurisdiction parts | ✓ | **PASS** |
| test_empty_jurisdiction | base + j={} | base unchanged | ✓ | **PASS** |
| test_none_jurisdiction | base + j=None | base unchanged | ✓ | **PASS** |
| test_avoid_duplicate | base already contains "Sunshine Health" | no duplicate | ✓ | **PASS** |
| test_empty_base | base="" | "" | ✓ | **PASS** |
| test_none_base | base=None | "" | ✓ | **PASS** |
| test_whitespace_base | base="  " | "" | ✓ | **PASS** |

---

## 3. `compute_refined_query` Tests

Computes refined_query from classification, state, and plan text.

| Test | Scenario | Expected | Result |
|------|----------|----------|--------|
| test_slot_fill_merges_jurisdiction | slot_fill + payer in state | merged query with Sunshine | **PASS** |
| test_new_question_uses_plan_text | new_question + plan_text | uses plan text | **PASS** |
| test_new_question_no_plan_fallback_user_text | new_question, no plan | uses user_text | **PASS** |
| test_slot_fill_no_last_refined_fallback | slot_fill, no last_refined | falls through to plan/user | **PASS** |
| test_new_question_empty_plan_uses_user_text | new_question, plan="" | uses user_text | **PASS** |

---

## 4. Jurisdiction Helpers (Smoke)

| Test | Expected | Result |
|------|----------|--------|
| test_get_jurisdiction_from_active_legacy_payer | active.payer → jurisdiction.payor | **PASS** |
| test_jurisdiction_to_summary | formats "Sunshine Health in Florida (Medicaid)" | **PASS** |

---

## 5. State & Flow

| Test | Expected | Result |
|------|----------|--------|
| test_refined_query_in_default_state | DEFAULT_STATE has refined_query key | **PASS** |
| test_flow_turn1_new_question | Turn 1: "how do I file an appeal" → new_question, refined from plan | **PASS** |
| test_flow_turn2_slot_fill_sunshine | Turn 2: "Sunshine Health" → slot_fill, refined = "... for Sunshine Health" | **PASS** |
| test_flow_turn3_new_question_replace | Turn 3: "how do I check eligibility" → new_question, refined replaced | **PASS** |

---

## 6. Related Tests (Short-term Memory)

Ensures state extractor and context router still behave correctly after changes:

| Test | Result |
|------|--------|
| test_payer_switch_resets_domain_and_slots | **PASS** |
| test_missing_payer_uses_previous_payer_stateful | **PASS** |
| test_open_slots_cleared_when_user_provides_service_code | **PASS** |
| test_no_patient_info_in_state | **PASS** |
| test_website_for_united_healthcare_sets_payer | **PASS** |
| test_answer_card_to_open_slots | **PASS** |

---

## 7. Worker Import Check

| Check | Result |
|-------|--------|
| `process_one` imports without error | **PASS** |
| `classify_message`, `build_refined_query`, `compute_refined_query` import | **PASS** |
| `DEFAULT_STATE["refined_query"]` exists | **PASS** |

---

## Run Command

```bash
cd /Users/ananth/Mobius
.venv/bin/python -m pytest mobius-chat/tests/test_refined_query.py mobius-chat/tests/test_short_term_memory.py -v
```
