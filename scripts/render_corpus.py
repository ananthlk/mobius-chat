"""Real answer drafts, each copied from a live trace or an A/B panel.

Named by correlation id where one exists. Nothing here is synthetic: a render
harness fed made-up drafts proves the harness works and nothing about the
product.
"""

CORPUS = [
 ("what is the timely filing deadline for sunshine health",
  "Timely filing deadlines for Sunshine Health vary by claim type and provider participation status.\n\n"
  "- **Initial claims** must be filed within 180 days for participating providers and 365 days for non-participating providers, calculated from the date of service.\n"
  "- **Reconsiderations or claim disputes** must be submitted within 90 days for participating providers and 180 days for non-participating providers, from the explanation-of-payment date.\n"
  "- **Coordination of benefits (COB) claims** have a 90-day filing limit from the primary payer's explanation of payment.\n"
  "- **Medicare claims crossing over to Medicaid** must be filed within the greater of 36 months from the date of service or 12 months from Medicare's adjudication date.",
  "A/B v1 arm — bold-led labels"),

 ("what is the timely filing deadline for sunshine health",
  "Sunshine Health's timely filing deadlines vary by claim type and provider status.\n\n"
  "- Initial claims must be filed within 180 days for participating providers and 365 days for non-participating providers, calculated from the date of service.\n"
  "- Reconsiderations or claim disputes for participating providers are due within 90 days, and for non-participating providers within 180 days.\n"
  "- Coordination of benefits claims must be filed within 90 days from the primary payer's explanation of payment date.\n"
  "- Medicare claims crossing over to Medicaid have a filing limit that is the greater of 36 months from the date of service or 12 months from Medicare's adjudication date.",
  "A/B v2 arm — same content, no labels"),

 ('Can you tell me more about Molina\'s care management for behavioral health?',
  "Of course, Genius! I'm happy to give you more detail on Molina's care management program.\n\n"
  "*   Dedicated Team: The Care Management team includes licensed nurses and clinicians with behavioral health experience to support members with mental health and substance use disorder needs. [1]\n"
  "*   Integrated Care Management (ICM): For members with high-risk psychiatric, medical, or psychosocial needs, there is a specialized ICM program. [1, 20]\n"
  "*   Provider Collaboration: Molina emphasizes partnership between Primary Care Providers and behavioral health specialists. [1]\n"
  "*   Provider Resources: Molina offers an online Behavioral Health Tool Kit for screening, assessment and diagnosis. [21]\n"
  "*   Member Support: Members have access to a 24/7 behavioral health crisis line staffed by clinicians. [3]\n\n"
  "Next step: To refer a member, contact Molina by phone at (855) 322-4076, by fax at (866) 440-9791, or by email at MFLCaseManagement@MolinaHealthcare.com.",
  "cid 9c825ca5 — labelled bullets, 31 words each"),

 ("What is the care management philosophy for Molina, Sunshine and United Healthcare?",
  "Hey Genius, I've got the care management philosophies for you! Here's how each plan approaches it:\n\n"
  "- **Molina Healthcare** focuses on a comprehensive Integrated Care Management (ICM) program built on a Member advocacy philosophy. Their goal is to coordinate all the care, services, and resources a member needs throughout their healthcare journey, aiming for the best possible outcomes [molina_fl_provider_manual_2026.pdf p111, p103].\n"
  "- **Sunshine Health** uses an interdisciplinary approach for its case management program. They bring together input from physical and behavioral health care managers, social workers, pharmacists, members, caregivers, and treating providers to address both medical and behavioral health needs [Sunshine Provider Manual p52].\n"
  "- **UnitedHealthcare Community Plan** operates with a Care Model program that aims to empower members, care providers, and the community to improve care coordination and outcomes [FL-Care-Provider-Manual p5].",
  "Three-payer comparison — bold-led but 50-word values"),

 ("Walk me through the Sunshine Health appeal process",
  "Sunshine Health's appeal process runs in three levels, and the clock on each starts from a different event.\n\n"
  "| Level | Deadline | Clock starts |\n| --- | --- | --- |\n"
  "| Level 1 | 90 days | Denial date on the EOP |\n"
  "| Level 2 | 60 days | Level 1 determination letter |\n"
  "| Fair hearing | 120 days | Final internal determination |\n\n"
  "Note that the Level 2 clock starts from the determination letter date, not the date you received it.\n\n"
  "Step 1: Pull the original claim and the EOP showing the denial reason.\n"
  "Step 2: Complete the Provider Dispute Resolution Request form in full.\n"
  "Step 3: Submit through the provider portal and keep the confirmation number.\n\n"
  "You will need the following on hand:\n\n"
  "- The original claim number\n- The denial CARC and RARC codes\n- Medical records for the dates in dispute\n\n"
  "Provider services: 1-844-477-8313\nAppeals fax: 1-866-534-5978\nPayer ID: 68069",
  "Long multi-shape — table + steps + checklist + contacts"),

 ("Tell me about COB claim filing for Sunshine Health",
  "| Topic | Requirement | Deadline |\n| :--- | :--- | :--- |\n"
  "| Filing COB Claims | Submit after the primary payer's EOP. Ensure all COB data is correct. | Within 90 days from the primary EOP |\n"
  "| Electronic Claims | Institutional (837I): COB data must be in loop 2300.<br>Professional (837P): loop 2320. | Same window |",
  "cid 65ed12e2 — real pipe table with <br> cells"),

 ("What does the provider manual say about filing?",
  "[1] Sunshine Provider Manual [authority=authoritative]\n"
  "Initial filing: 180 days\nResubmission: 90 days\nCopay: $25",
  "Thin-evidence hedge — a verbatim retrieved excerpt"),

 ("Does a patient need prior auth for outpatient therapy?",
  "Whether a prior authorization is required for outpatient behavioral health services depends on the specific service, the place of service, and whether the member is enrolled in the Medicaid or the Marketplace product. For routine outpatient therapy delivered in an office setting, the first twenty visits per calendar year generally do not require authorization for Medicaid members, though this threshold is applied per provider rather than per member, which means a member who switches providers mid-year may trigger a new count.",
  "Genuinely unstructured policy narrative — the control"),
]
