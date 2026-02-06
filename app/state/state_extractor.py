"""Deterministic state extraction from user text and optional answer_card. No LLM calls.
Do not write user-provided patient info to chat_state (DOB, names, MRNs, etc.)."""
import re
from typing import Any

# Fallback payer names when config/payer_normalization.yaml is not used (e.g. Aetna, Medicaid)
PAYER_NAMES_FALLBACK = (
    "Sunshine Health",
    "Sunshine",
    "UnitedHealthcare",
    "United Healthcare",
    "UHC",
    "Aetna",
    "Humana",
    "Cigna",
    "Anthem",
    "Blue Cross",
)

# Program / coverage words that should NOT be treated as a payer for RAG filtering.
# If we set active.payer="Medicaid", Vertex filtering usually returns 0 because the index uses document_payer=plan name.
GENERIC_COVERAGE_WORDS = ("medicaid", "medicare")

# Program keywords -> canonical program token used in the RAG index (document_program).
# Keep these canonical values aligned with what the dbt sync writes to Vertex/Postgres.
PROGRAM_KEYWORDS: list[tuple[list[str], str]] = [
    (["medicaid", "medicaid managed care", "mco"], "Medicaid"),
    (["medicare", "medicare advantage", "ma plan"], "Medicare"),
]

# Domain keywords -> domain enum
DOMAIN_KEYWORDS: list[tuple[list[str], str]] = [
    (["prior auth", "preauth", "pre-auth", "authorization", "prior authorization"], "prior_auth"),
    (["dispute", "appeal", "reconsideration", "grievance"], "disputes"),
    (["eligibility", "coverage", "cob", "co-b", "verified"], "eligibility"),
    (["contact", "phone", "provider relations", "provider relations"], "contacts"),
    (["utilization management", "um ", " utilization review"], "um"),
    (["claims", "denial", "eob", "explanation of benefits", "claim status"], "claims"),
    (["billing", "payment", "reimbursement"], "billing"),
    (["benefits", "benefit"], "benefits"),
    (["other"], "other"),
]

# Jurisdiction: state abbreviations and full names (sample)
STATE_ABBREVS = ("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY")
STATE_NAMES = ("Florida", "North Carolina", "Texas", "California", "New York", "Georgia", "Ohio", "Pennsylvania")

# User role keywords
ROLE_PROVIDER = ("as a provider", "our clinic", "provider portal", "we are a provider", "provider office")
ROLE_PATIENT = ("member", "i am a patient", "i'm a patient", "as a member", "as a patient")

# Open slot answer patterns (fulfill slot when user says something like this)
SLOT_ANSWER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("service_code", re.compile(r"\b(CPT|HCPCS|procedure\s+code)\s*[:\s]*\d+|\b\d{5}(-\d{2})?\b", re.I)),
    ("plan_type", re.compile(r"\b(plan\s+is|medicaid|medicare|commercial|ppo|hmo)\b", re.I)),
    ("member_type", re.compile(r"\b(member\s+type|subscriber|dependent)\b", re.I)),
    ("date_range", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2},?\s*\d{4}\b", re.I)),
    ("provider_type", re.compile(r"\b(provider\s+type|npi|facility)\b", re.I)),
]

# Reset / new topic phrases
NEW_TOPIC_PHRASES = ("new question", "different topic", "different question", "new topic", "switch to", "what about")


def _detect_payer(text: str) -> str | None:
    """Detect single payer from user text. Prefer config; fallback to PAYER_NAMES_FALLBACK."""
    payers = _detect_all_payers(text)
    return payers[0] if payers else None


def _detect_all_payers(text: str) -> list[str]:
    """Detect all payers mentioned in text. Returns list of canonical names for multi-payer questions (compare A, B, C)."""
    try:
        from app.payer_normalization import detect_all_payers_from_text
        found = detect_all_payers_from_text(text)
        if found:
            return found
    except Exception:
        pass
    t = (text or "").strip().lower()
    result: list[str] = []
    seen: set[str] = set()
    for name in PAYER_NAMES_FALLBACK:
        if name.lower() in t and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _detect_domain(text: str) -> str | None:
    t = (text or "").strip().lower()
    for keywords, domain in DOMAIN_KEYWORDS:
        for kw in keywords:
            if kw in t:
                return domain
    return None


def _detect_program(text: str) -> str | None:
    """Detect program from text (Medicaid/Medicare, etc.)."""
    tl = (text or "").strip().lower()
    if not tl:
        return None
    for keywords, program in PROGRAM_KEYWORDS:
        for kw in keywords:
            # Use word boundaries for short tokens; allow phrases as substring match.
            if len(kw) <= 3:
                if re.search(r"\b" + re.escape(kw) + r"\b", tl, flags=re.I):
                    return program
            else:
                if kw in tl:
                    return program
    return None


def _detect_jurisdiction(text: str) -> str | None:
    """
    Detect a US state jurisdiction from text.

    Important: avoid false-positives on common short tokens:
    - "ID" is almost always "member ID / Medicaid ID / ID card" in this app, not Idaho.
    - "OR", "IN", "ME", "OK", "HI" appear frequently as normal words.

    We therefore only accept state abbreviations when they appear in clear *location* contexts
    (e.g. "in FL", "Miami, FL", "(FL)", "state of FL"). Full state names are accepted.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    t = raw
    tl = raw.lower()

    # 1) Full names (less ambiguous)
    for name in STATE_NAMES:
        if name.lower() in tl:
            return name

    # 2) Abbreviations: require clear context
    # Patterns intentionally case-insensitive for the surrounding words, but the token itself is matched and normalized.
    ctx_patterns = (
        r"(?:,|\()\s*(?P<ab>[A-Z]{2})\b",              # "Miami, FL" or "(FL"
        r"\bstate\s+of\s+(?P<ab>[A-Z]{2})\b",          # "state of FL"
        r"\bin\s+(?P<ab>[A-Z]{2})\b",                  # "in FL"
        r"\bfor\s+(?P<ab>[A-Z]{2})\b",                 # "for FL"
        r"\b(?:florida|texas|california|new\s+york|georgia|ohio|pennsylvania|north\s+carolina)\s*\((?P<ab>[A-Z]{2})\)",  # "Florida (FL)"
    )

    def _is_id_like_context(ab: str, start: int, end: int) -> bool:
        """True when the matched 'ID' token is likely an identifier, not Idaho."""
        if ab != "ID":
            return False
        after = tl[end : end + 20].strip()
        before = tl[max(0, start - 25) : start].strip()
        # Common contexts: "ID card", "member ID", "Medicaid ID", "insurance ID"
        if after.startswith(("card", "cards", "number", "numbers")):
            return True
        if before.endswith(("member", "medicaid", "insurance", "plan")):
            return True
        if "id card" in tl or "member id" in tl or "medicaid id" in tl:
            return True
        return False

    # Search for context patterns and validate the abbreviation.
    for pat in ctx_patterns:
        m = re.search(pat, t, flags=re.I)
        if not m:
            continue
        ab = (m.group("ab") or "").upper()
        if ab not in STATE_ABBREVS:
            continue
        if _is_id_like_context(ab, m.start("ab"), m.end("ab")):
            continue
        return ab

    return None


def _detect_user_role(text: str) -> str | None:
    t = (text or "").strip().lower()
    for phrase in ROLE_PROVIDER:
        if phrase in t:
            return "provider_office"
    for phrase in ROLE_PATIENT:
        if phrase in t:
            return "patient"
    return None


def _open_slots_fulfilled(user_text: str, open_slots: list[str]) -> list[str]:
    """Return open_slots with any slot removed if user_text looks like an answer for it."""
    if not open_slots or not user_text:
        return list(open_slots)
    t = (user_text or "").strip()
    remaining = []
    for slot in open_slots:
        matched = False
        for slot_key, pattern in SLOT_ANSWER_PATTERNS:
            if slot_key == slot and pattern.search(t):
                matched = True
                break
        if not matched:
            remaining.append(slot)
    return remaining


def answer_card_to_open_slots(parsed_answer_card: dict[str, Any]) -> list[str]:
    """Map AnswerCard required_variables and followups to open_slot type strings. Used post-turn."""
    return _answer_card_to_slot_types(
        parsed_answer_card.get("required_variables") or [],
        parsed_answer_card.get("followups") or [],
    )


def _answer_card_to_slot_types(required_variables: list[str], followups: list[dict]) -> list[str]:
    """Map required_variables and followup questions to slot type strings."""
    slot_map = {
        "service code": "service_code",
        "service_code": "service_code",
        "cpt": "service_code",
        "procedure code": "service_code",
        "plan type": "plan_type",
        "plan_type": "plan_type",
        "member type": "member_type",
        "member_type": "member_type",
        "date": "date_range",
        "date range": "date_range",
        "date_range": "date_range",
        "provider type": "provider_type",
        "provider_type": "provider_type",
        "npi": "provider_type",
    }
    out: list[str] = []
    seen: set[str] = set()
    for v in required_variables or []:
        key = (v or "").strip().lower().replace(" ", "_")
        if key in slot_map and slot_map[key] not in seen:
            out.append(slot_map[key])
            seen.add(slot_map[key])
        key_alt = (v or "").strip().lower()
        if key_alt in slot_map and slot_map[key_alt] not in seen:
            out.append(slot_map[key_alt])
            seen.add(slot_map[key_alt])
    for f in followups or []:
        q = (f.get("question") or f.get("reason") or f.get("field") or "").strip().lower()
        for phrase, slot in slot_map.items():
            if phrase in q and slot not in seen:
                out.append(slot)
                seen.add(slot)
                break
    return out


def extract_state_patch(
    user_text: str,
    existing_state: dict[str, Any],
    parse1_output: dict[str, Any] | None,
    answer_card: dict[str, Any] | None,
) -> tuple[dict[str, Any], str | None]:
    """Extract state patch from user text and optional parse1/answer_card. Returns (patch, reset_reason).
    Does not write any patient-specific data (DOB, names, MRNs, etc.) to the patch."""
    patch: dict[str, Any] = {}
    reset_reason: str | None = None
    existing_active = (existing_state or {}).get("active") or {}
    existing_payer = existing_active.get("payer")
    existing_program = existing_active.get("program")
    existing_domain = existing_active.get("domain")
    existing_jurisdiction = (existing_active.get("jurisdiction") or "").strip() or None
    existing_slots = (existing_state or {}).get("open_slots") or []

    # 1) Payer(s): single -> active.payer; multiple -> active.payers (list), active.payer = None so RAG gets all
    payers_raw = _detect_all_payers(user_text or "")
    payers = [p for p in payers_raw if (p or "").strip().lower() not in GENERIC_COVERAGE_WORDS]
    if payers:
        if len(payers) == 1:
            patch.setdefault("active", {})["payer"] = payers[0]
            patch.setdefault("active", {})["payers"] = []
            if existing_payer and (existing_payer or "").strip().lower() != (payers[0] or "").strip().lower():
                reset_reason = "payer_change"
                patch.setdefault("active", {})["domain"] = None
                patch["open_slots"] = []
        else:
            patch.setdefault("active", {})["payer"] = None
            patch.setdefault("active", {})["payers"] = payers
            reset_reason = "payer_change"
            patch.setdefault("active", {})["domain"] = None
            patch["open_slots"] = []
    # 2) Domain
    domain = _detect_domain(user_text or "")
    if domain:
        patch.setdefault("active", {})["domain"] = domain
        if existing_domain and (existing_domain or "").strip().lower() != (domain or "").strip().lower():
            patch["open_slots"] = []
    # 2b) Program (Medicaid/Medicare). Program is separate from payer; do not reset payer on program change.
    program = _detect_program(user_text or "")
    if program:
        patch.setdefault("active", {})["program"] = program
        if existing_program and (existing_program or "").strip().lower() != (program or "").strip().lower():
            # Program changes can change which docs are relevant; clear open slots.
            patch["open_slots"] = []
    # 3) Jurisdiction
    jur = _detect_jurisdiction(user_text or "")
    if jur:
        patch.setdefault("active", {})["jurisdiction"] = jur
    else:
        # Recovery: if a previous turn incorrectly set a jurisdiction (most commonly "ID" from "ID card"),
        # clear it when the current message is clearly about an identifier and not a location.
        tl = (user_text or "").lower()
        if existing_jurisdiction == "ID" and (
            "id card" in tl
            or "member id" in tl
            or "medicaid id" in tl
            or "insurance id" in tl
        ):
            # Only clear when there's no explicit Idaho/location mention in the same message.
            if not re.search(r"(?:\bin\s+ID\b|\bstate\s+of\s+ID\b|,\s*ID\b|\(ID\))", user_text or "", flags=re.I):
                patch.setdefault("active", {})["jurisdiction"] = None
    # 4) User role
    role = _detect_user_role(user_text or "")
    if role:
        patch.setdefault("active", {})["user_role"] = role
    # 5) Open slots: remove fulfilled when user_text looks like an answer (slots are added post-turn via register_open_slots)
    remaining_slots = _open_slots_fulfilled(user_text or "", existing_slots)
    if "open_slots" not in patch and remaining_slots != existing_slots:
        patch["open_slots"] = remaining_slots
    # 6) Recent entities from answer_card sections (labels)
    if answer_card:
        sections = answer_card.get("sections") or []
        labels = []
        for s in sections[:5]:
            lbl = (s.get("label") or "").strip()
            if lbl and len(lbl) < 80:
                labels.append(lbl)
        if labels:
            patch["recent_entities"] = labels[:5]

    return (patch, reset_reason)
