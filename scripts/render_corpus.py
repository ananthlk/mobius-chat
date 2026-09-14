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

 # ── Ananth's 15 paired A/B questions, 2026-09-14 02:44-03:10 UTC ──────────
 #
 # Both arms of every question, real drafts, pulled from chat_turns. The UX
 # seat asked for these because they are shapes they have not seen; v1 and v2
 # are BOTH here because the two arms write differently and the pair is the
 # comparison -- v1 shipped 0.73 sections/answer on this run, v2 0.00.
 #
 # Nothing synthesised, per this file's own rule.

 ("Compare the prior authorization requirements for inpatient behavioral health between Sunshine Health and Molina in Florida.",
  "Hey Genius, I've looked into the prior authorization requirements for inpatient behavioral h"
  "ealth in Florida for you! Here's what I found: **Sunshine Health** has specific prior author"
  "ization requirements for **non-emergency inpatient behavioral health admissions** [6], cover"
  "ing submission methods, emergency notifications, and involuntary admissions. Unfortunately, "
  "I couldn't find specific prior authorization requirements for **Molina Healthcare** in our a"
  "vailable materials, so you'll want to check directly with them for their guidelines.",
  "A/B v1 arm — cid 70fd23a3, qc 0.701"),

 ("Compare the prior authorization requirements for inpatient behavioral health between Sunshine Health and Molina in Florida.",
  "Hey there, Genius! Here's a quick rundown on prior authorization for inpatient behavioral he"
  "alth with **Sunshine Health** and **Molina** in Florida:\n\n**Molina Healthcare** requires pri"
  "or authorization for inpatient behavioral health services, including psychiatric treatment a"
  "nd SUD detoxification. You can submit these requests via a form, by calling their prior auth"
  "orization team at **(855) 322-4076**, or through the **Availity Essentials portal**. Emergen"
  "cy psychiatric services don't need prior authorization. They use standard medical necessity "
  "criteria, like the **ASAM Criteria** for mental health/substance use disorder inpatient revi"
  "ews.\n\n**Sunshine Health** also requires prior authorization for inpatient psychiatric servic"
  "es. You can submit these through their **Secure Provider Portal**, by calling **1-844-477-83"
  "13**, or by faxing **1-866-796-0526**. For emergency behavioral health services, prior autho"
  "rization isn't needed, but you must notify them within two business days of admission, or **"
  "24 hours** for a **Baker Act** admission. For involuntary admissions, they'll automatically "
  "approve a minimum of three days for **MMA, CW, SMI, CMS, and LTC Comprehensive** products, a"
  "nd one day for **Ambetter**, even if medical necessity isn't immediately met.\n\n→ Next step: "
  "Always check the specific payer's provider manual or prior authorization tool for the most c"
  "urrent and detailed requirements.",
  "A/B v2 arm — cid ebd580bc, qc 1.0"),

 ("Do Sunshine Health and UnitedHealthcare Community Plan use the same provider credentialing timeline in Florida?",
  "Hey there, Genius! Good question. It looks like **Sunshine Health** and **UnitedHealthcare C"
  "ommunity Plan** actually have pretty similar credentialing timelines in Florida.\n\nSo, while "
  "the exact wording differs, the core timelines for recredentialing and decision notification "
  "are quite aligned!",
  "A/B v1 arm — cid ac5c066d, qc 0.94"),

 ("Do Sunshine Health and UnitedHealthcare Community Plan use the same provider credentialing timeline in Florida?",
  "Hey there, Genius! I've looked into the credentialing timelines for both **Sunshine Health**"
  " and **UnitedHealthcare Community Plan** in Florida.\n\n**Sunshine Health** states that their "
  "credentialing staff takes up to **60 calendar days** to complete the initial credentialing p"
  "rocess once an application is submitted. They also notify providers of the decision within *"
  "*60 days** from the credentialing committee meeting. Recredentialing occurs at least every *"
  "*three years** or **36 months**.\n\n**UnitedHealthcare Community Plan** notifies providers of "
  "their credentialing decision within **60 calendar days** of the review. They also recredenti"
  "al practitioners every **3 years**.\n\nSo, it looks like both plans have a similar timeline fo"
  "r initial decisions and recredentialing cycles.\n\n**→ Next step:** If you have specific quest"
  "ions about the application process or required documents, you might want to check their resp"
  "ective provider manuals or contact their credentialing departments directly.",
  "A/B v2 arm — cid 5d35b1b8, qc 0.993"),

 ("Does Aetna Better Health of Florida cover doula services under Florida Medicaid?",
  "Hey there, Genius! I've looked through the available materials for **Aetna Better Health of "
  "Florida** regarding their **Florida Medicaid** coverage, but I couldn't find any specific me"
  "ntion of **doula services**.\n\nThis wasn't found in our materials.\n\n*   The documents cover g"
  "eneral **prenatal and postpartum care**, but don't specify if doula services are included.\n*"
  "   It's best to reach out to **Aetna Better Health of Florida** directly for the most accura"
  "te and up-to-date information on this specific service.\n\n→ Next step: You can contact **Aetn"
  "a Better Health of Florida's Member Services** at **1-800-441-5501** (for Managed Medicaid, "
  "MMA) or **1-844-528-5815** (for Florida Healthy Kids) to inquire about coverage for doula se"
  "rvices.",
  "A/B v1 arm — cid 9e0c274b, qc 1.0"),

 ("Does Aetna Better Health of Florida cover doula services under Florida Medicaid?",
  "Hey Genius! I couldn't find specific information about **Aetna Better Health of Florida's co"
  "verage for doula services** under Florida Medicaid in our available provider manuals. This m"
  "eans the documents we have don't explicitly list doula services as either covered or exclude"
  "d.\n\n→ Next step: To get a definitive answer, you'll need to contact **Aetna Better Health of"
  " Florida** directly. You can reach their Provider Services at **1-844-528-5815** (for Florid"
  "a Healthy Kids) or **1-800-441-5501** (for Managed Medicaid MMA).",
  "A/B v2 arm — cid 402165a7, qc 0.955"),

 ("Does Molina Healthcare reimburse Florida providers for telephonic-only care coordination?",
  "Genius, regarding Molina Healthcare in Florida, their provider manual clarifies that **telep"
  "hone conversations are not considered telemedicine** [2]. This means that telephonic-only ca"
  "re coordination, when solely relying on telephone conversations, is not reimbursed under the"
  "ir telemedicine guidelines [2]. The available information doesn't explicitly state whether s"
  "uch services might be covered under a different service category or if they are not reimburs"
  "ed at all.",
  "A/B v1 arm — cid 4246e67d, qc 0.832"),

 ("Does Molina Healthcare reimburse Florida providers for telephonic-only care coordination?",
  "Hey there, Genius! Generally, **Molina Healthcare of Florida** does **not** reimburse for te"
  "lephonic-only care coordination, as telephone conversations aren't considered telemedicine.\n"
  "\n→ Next step: If you have a specific scenario or code in mind, you might want to check the l"
  "atest Molina provider manual or contact them directly for clarification on any exceptions.",
  "A/B v2 arm — cid 35e61af5, qc 1.0"),

 ("For Florida Medicaid SMI members, what care coordination services are covered and which require prior authorization, by plan?",
  "Florida Medicaid generally **does not cover case management** [2, 12]. For Molina Healthcare"
  ", \"Behavioral Health Assessment Services\" for **SMI members do not require prior authorizati"
  "on** [9], though some other behavioral health services may [1]. Sunshine Health covers \"Comm"
  "unity-based Wraparound Service in Lieu of Services\" for SMI members, which **requires prior "
  "authorization** [11]. The evidence does not specify other care coordination services covered"
  " for SMI members by plan.\n\n_I wasn't fully confident in this answer and couldn't complete my"
  " usual verification — treat it as a starting point, not a final word._",
  "A/B v1 arm — cid 9e242c4a, qc 0.868"),

 ("For Florida Medicaid SMI members, what care coordination services are covered and which require prior authorization, by plan?",
  "Hey Genius, I looked into the care coordination coverage for you. It's a bit tricky because "
  "the plans don't list everything in one place, but I've got the key details and where to find"
  " the rest!\n\n**For most plans, you'll need to check their provider portal for a specific list"
  " of services requiring prior authorization, as general care coordination coverage isn't deta"
  "iled in the manuals.**\n\n*   **UnitedHealthcare:** Members can access all behavioral health o"
  "utpatient services without a referral. However, more intensive services (like intensive outp"
  "atient, partial hospitalization, or residential care) **do require prior authorization**. Yo"
  "u can find the complete, up-to-date list of services needing PA on their portal at **UHCprov"
  "ider.com/FLcommunityplan** under **Prior Authorization and Notification**.\n\n*   **Sunshine H"
  "ealth & Molina:** Based on what I found in earlier searches, both plans direct you to online"
  " tools for the most accurate information. Sunshine Health has a **Pre-Auth Check Tool**, and"
  " Molina posts its list of services requiring PA on its website at **MolinaHealthcare.com**.\n"
  "\n*   **Florida Medicaid (General Policy):** It's important to know that under the state's \"B"
  "ehavioral Health Community Support Services\" policy, **case management is specifically liste"
  "d as a non-covered service**.\n\n→ **Next step:** The best way to be certain is to use the pro"
  "vider portals for each plan before rendering services. They are the final source of truth fo"
  "r prior authorization.",
  "A/B v2 arm — cid 3a3d30a7, qc 1.0"),

 ("How does a provider submit a corrected claim to Aetna Better Health of Florida, and what is the deadline?",
  "Hey there, Genius! Here's the scoop on submitting corrected claims to Aetna Better Health of"
  " Florida:\n\n**Providers can submit corrected claims to Aetna Better Health of Florida electro"
  "nically or by mail.**\n\n**The deadline for submitting a corrected claim is **180 days** from "
  "the date of service or discharge from an inpatient admission.**\n\n→ Next step: For any questi"
  "ons, you can call the Claims Inquiry Claims Research (CICR) department at **1-844-235-1340**"
  ".",
  "A/B v1 arm — cid 3dd809cb, qc 0.856"),

 ("How does a provider submit a corrected claim to Aetna Better Health of Florida, and what is the deadline?",
  "Hey there, Genius! Here's how you can submit a corrected claim to **Aetna Better Health of F"
  "lorida** and what the deadline is:\n\n**Corrected claims** must be submitted within **180 days"
  " from the date of service or discharge date**.\n\n*   You can submit them **electronically via"
  " EDI or the Availity portal**.\n*   For mail submissions, use the **Claim Adjustment Request/"
  "Claim Reconsideration form** and include an updated copy of the claim, the original claim, t"
  "he remittance advice, and a brief note describing the correction. Clearly label it \"Resubmis"
  "sion\" and mail to **Aetna Better Health of Florida, PO Box 982960, El Paso, TX 79998-2960**."
  "\n*   For Medicare crossover claims, the deadline is **36 months** from the original submissi"
  "on to Medicare.\n\n→ Next step: Ensure all required fields are completed and include the origi"
  "nal claim number in field 64 of the CMS-1450 (UB-04) or field 22 of the CMS-1500 form, along"
  " with the appropriate frequency code.",
  "A/B v2 arm — cid da93967f, qc 1.0"),

 ("How many days does Aetna Better Health of Florida have to resolve a standard provider appeal?",
  "The available evidence does not literally state how many days Aetna Better Health of Florida"
  " has to resolve a \"standard provider appeal.\"\n\n*   The evidence indicates that **provider co"
  "mplaints** are resolved within 60 calendar days of receipt according to the Florida Healthy "
  "Kids Provider Manual [3].\n*   Alternatively, the Medicaid Comprehensive LTC Provider Manual "
  "states that **provider complaints** will be resolved within 9",
  "A/B v1 arm — cid 69ab11ed, qc 0.373"),

 ("How many days does Aetna Better Health of Florida have to resolve a standard provider appeal?",
  "**Aetna Better Health of Florida resolves provider complaints within either 60 or 90 calenda"
  "r days of receipt, depending on the program.**\n*   For Medicaid and Comprehensive Long Term "
  "Care (LTC) programs, all provider complaints are resolved within 90 calendar days [2, 11].\n*"
  "   For the Florida Healthy Kids program, all provider complaints are resolved within 60 cale"
  "ndar days [15].",
  "A/B v2 arm — cid f781fdba, qc 0.966"),

 ("What are the prior authorization requirements and billing codes for peer support services across Sunshine Health, Molina, and UnitedHealthcare in Florida?",
  "Hey Genius, it's a great question! Here's the breakdown on peer support services for those p"
  "ayers.\n\nThe main thing to know is that none of them give a simple 'yes' or 'no' on prior aut"
  "horization—they all point you to an online tool to check the requirements for each specific "
  "member and service code.\n\n## Billing Codes\nPeer support generally falls under \"Behavioral He"
  "alth Community Support Services\" [4] or \"Psychosocial Rehabilitation.\" The most common HCPCS"
  " codes you'll likely use are **H2017** (Psychosocial rehabilitation services) and **H2030** "
  "(Clubhouse services).\n\n## Prior Authorization by Payer\nThe rules vary by payer, so I've put "
  "the details into a table for a quick side-by-side look.",
  "A/B v1 arm — cid 6a6a5c14, qc 0.823"),

 ("What are the prior authorization requirements and billing codes for peer support services across Sunshine Health, Molina, and UnitedHealthcare in Florida?",
  "Hey Genius, I've looked into the requirements for peer support services for you. It's a bit "
  "of a mixed bag, but here's the breakdown for each plan.\n\n**The primary billing codes for pee"
  "r support are likely H2017 (Psychosocial Rehabilitation) and H2030 (Clubhouse Services).** W"
  "hile policies vary, all three plans require you to verify prior authorization using their sp"
  "ecific online tools before billing.\n\n*   **Sunshine Health:** Generally, most outpatient beh"
  "avioral health services **do not require prior authorization** [9, 15]. However, they make i"
  "t clear that you should always confirm this by using their **Pre-Auth Check Tool** on the se"
  "cure provider portal for the specific code you're billing [10].\n\n*   **Molina Healthcare:** "
  "Molina is a bit stricter. They **do require prior authorization** for most behavioral health"
  " services, unless the member has a Serious Mental Illness (SMI) designation [32]. You'll nee"
  "d to check their **Prior Authorization Code Lookup Tool** on the Molina website to be sure ["
  "1].\n\n*   **UnitedHealthcare (UHC):** Similar to Sunshine, UHC states that members can access"
  " most outpatient behavioral health services without a referral or prior auth. However, **pri"
  "or authorization may be required for more intensive services** [17], which could include psy"
  "chosocial rehab. \n\n→ **Next step:** For all three payers, the most important step is to use "
  "their online provider portal to check the specific code and member before providing the serv"
  "ice. That's the only way to guarantee you'll be paid!",
  "A/B v2 arm — cid 585aafc8, qc 0.9"),

 ("What is Molina Healthcare's timely filing deadline for corrected claims in Florida?",
  "**Corrected Claims must be sent within six months of the Date of Service or the most recent "
  "adjudicated date of the Claim.** This applies to Molina Healthcare Medicaid Members in Flori"
  "da.",
  "A/B v1 arm — cid be076fa1, qc 1.0"),

 ("What is Molina Healthcare's timely filing deadline for corrected claims in Florida?",
  "**Corrected Claims for Molina Healthcare in Florida must be sent within six months.** This d"
  "eadline is calculated from either the Date of Service or the most recent adjudicated date of"
  " the Claim.",
  "A/B v2 arm — cid 12261b98, qc 1.0"),

 ("What is Sunshine Health's reimbursement rate for CPT 99490 in Florida?",
  "Hey Genius! It looks like I couldn't pinpoint **Sunshine Health's** exact reimbursement rate"
  " for **CPT 99490** in the materials I have right now. Specific fee schedules like these are "
  "usually proprietary and not publicly available in general documentation, making them tricky "
  "to find without direct access to their provider resources.",
  "A/B v1 arm — cid 13199ff0, qc 0.955"),

 ("What is Sunshine Health's reimbursement rate for CPT 99490 in Florida?",
  "Hey Genius! I couldn't find **Sunshine Health's reimbursement rate for CPT 99490** in our cu"
  "rrent resources. This CPT code isn't listed in our Florida Medicaid behavioral health servic"
  "e line registry, and a broader search didn't return the specific rate either. You'd likely n"
  "eed to contact **Sunshine Health** directly for this information.",
  "A/B v2 arm — cid 43ceb3fd, qc 0.892"),

 ("What is the member cost-share for emergency room visits under Sunshine Health's Florida Medicaid plan?",
  "The provided evidence does not state a specific member cost-share for emergency room visits "
  "under Sunshine Health's Florida Medicaid plan. The evidence indicates that Sunshine Health c"
  "overs emergency services and does not require prior authorization for them. It also notes th"
  "at Indians are exempt from all cost sharing for services furnished or received by an Indian "
  "Health Care Provider (IHCP) or referral under contract health services.",
  "A/B v1 arm — cid 35f0042c, qc 0.458"),

 ("What is the member cost-share for emergency room visits under Sunshine Health's Florida Medicaid plan?",
  "**The provided evidence does not specify a member cost-share for emergency room visits under"
  " Sunshine Health's Florida Medicaid plan.** The documents confirm that emergency services ar"
  "e covered and do not require prior authorization, even if the facility is outside the provid"
  "er network or service area. Members are advised to use the emergency room only for real emer"
  "gencies.",
  "A/B v2 arm — cid 6a93e700, qc 0.854"),

 ("What is the process for a Florida Medicaid provider to dispute a UnitedHealthcare recoupment?",
  "Hey there, Genius! To dispute a **UnitedHealthcare Florida Medicaid** recoupment, you'll gen"
  "erally follow a two-step process:\n\n*   **Step 1: Claim Reconsideration.** If you disagree wi"
  "th an overpayment, underpayment, or payment denial (which includes recoupments), you can req"
  "uest a reconsideration. This needs to be submitted within **90 business days** of the origin"
  "al determination.\n*   **Step 2: Formal Appeal.** If you're not happy with the outcome of the"
  " reconsideration, you can then file a formal appeal. This must be done within **90 days** fr"
  "om the date of the reconsideration determination.\n*   **Submission:** Most providers in Flor"
  "ida are expected to submit these requests electronically through the **UnitedHealthcare Prov"
  "ider Portal** (UHCprovider.com/claims). You can also call **Provider Services** at **1-800-7"
  "91-9233** (for LTC) or use the chat feature on the portal for MMA inquiries.\n*   **Documenta"
  "tion:** When appealing, make sure to include all supporting documents like a cover letter, m"
  "edical records, and any additional information not previously submitted.\n\n→ Next step: Start"
  " by gathering all relevant documentation and preparing your claim reconsideration request th"
  "rough the **UnitedHealthcare Provider Portal**.",
  "A/B v1 arm — cid 06438b37, qc 1.0"),

 ("What is the process for a Florida Medicaid provider to dispute a UnitedHealthcare recoupment?",
  "Hey there, Genius! To dispute a **UnitedHealthcare Community Plan** recoupment in Florida, y"
  "ou'll follow a two-step process:\n\n**1. Care Provider Claim Reconsideration:**\n*   This is th"
  "e first step if you disagree with an overpayment, underpayment, or denial. You've got **90 b"
  "usiness days** to submit it.\n*   Most providers need to submit these requests electronically"
  " through the **UnitedHealthcare Provider Portal** (UHCprovider.com/claims).\n\n**2. Care Provi"
  "der Claim Formal Appeal:**\n*   If you're not happy with the reconsideration outcome, you can"
  " file a formal appeal. This needs to be done within **90 days** from the date of the reconsi"
  "deration determination.\n*   Again, electronic submission via the **UnitedHealthcare Provider"
  " Portal** is generally required.\n\n→ Next step: Visit **UHCprovider.com/claims** or the **Uni"
  "tedHealthcare Provider Portal** to initiate a claim reconsideration.",
  "A/B v2 arm — cid a43167fc, qc 0.986"),

 ("What steps must a provider take to request a peer-to-peer review after a Sunshine Health denial?",
  "Hey there, Genius! To request a peer-to-peer review after a service denial or limitation fro"
  "m **Sunshine Health**, you'll need to call them directly.\n\n**Providers can request a peer-to"
  "-peer review by calling 1-844-427-8313 ext. 6032912.**",
  "A/B v1 arm — cid 5b07fe96, qc 0.673"),

 ("What steps must a provider take to request a peer-to-peer review after a Sunshine Health denial?",
  "Hey there, Genius! To request a peer-to-peer review after a Sunshine Health denial, you'll n"
  "eed to make a phone call. 📞\n\n*   If a medical director or contracted vendor denies or limits"
  " a service, the **requesting provider can call 1-844-427-8313 ext. 6032912** to initiate a p"
  "eer-to-peer review.",
  "A/B v2 arm — cid a11068a3, qc 0.729"),

 ("Which HCPCS codes does Florida Medicaid use for behavioral health assessment, and do Sunshine Health and Molina both follow the state schedule?",
  "Hey Genius! Happy to help you sort out those assessment codes. Here's the breakdown for Flor"
  "ida Medicaid: 😊\n\n## Behavioral Health Assessment Codes\nThe main HCPCS codes Florida Medicaid"
  " uses for these assessments are **H0001**, **H0031**, **H2000**, and **H2010**. You'll often"
  " see these with modifiers like `HO`, `TS`, or `HN` to specify the provider type or other det"
  "ails.\n\n## Sunshine Health & Molina Alignment\nYes, both **Sunshine Health** and **Molina** ar"
  "e required to follow the state's coverage rules for these codes. Florida Medicaid policy sta"
  "tes that managed care plans can't be more restrictive than the state's own guidelines. The p"
  "rovider manuals for both plans confirm they align their billing policies with state and fede"
  "ral Medicaid rules [1, 2].",
  "A/B v1 arm — cid 477b682b, qc 0.902"),

 ("Which HCPCS codes does Florida Medicaid use for behavioral health assessment, and do Sunshine Health and Molina both follow the state schedule?",
  "Hey Genius, I've got the details you were looking for on those assessment codes. It's pretty"
  " straightforward, which is always nice!\n\n**Yes, both Sunshine Health and Molina follow the F"
  "lorida Medicaid schedule for behavioral health assessment codes.**\n\n*   **State Codes:** Flo"
  "rida Medicaid's main codes for behavioral health assessment are **H0001** (for substance abu"
  "se), **H0031** (for mental health), **H2000** (psychiatric/multidisciplinary evaluation), **"
  "H0032** (treatment plan development), and **T1007** (substance abuse treatment plan).\n*   **"
  "Molina:** Their provider manual is super clear on this. It says they cover behavioral health"
  " assessment services exactly as they're listed on the **Florida Medicaid Community Behaviora"
  "l Health Fee Schedule**.\n*   **Sunshine Health:** They also follow the state's rules. State "
  "policy actually requires all managed care plans to stick to the coverage requirements. Sunsh"
  "ine Health does offer some *expanded* benefits, like unlimited assessments when medically ne"
  "cessary for certain members, but their core coverage is based on that state schedule.",
  "A/B v2 arm — cid 622cd83b, qc 1.0"),

 ("Which has the longer claims appeal window in Florida Medicaid — Aetna Better Health or Molina?",
  "Hey there, Genius! Looks like **Molina Healthcare** has the longer claims appeal window in F"
  "lorida Medicaid. You've got **one year** from the original remittance advice date to dispute"
  " or appeal a claim with them. For **Aetna Better Health**, it's **90 calendar days** from th"
  "e claim denial. So, Molina gives you more time! 😊",
  "A/B v1 arm — cid 562d5564, qc 0.963"),

 ("Which has the longer claims appeal window in Florida Medicaid — Aetna Better Health or Molina?",
  "Hey there, Genius! **Molina** has the longer claims appeal window in Florida Medicaid.\n\n*   "
  "**Molina** allows providers **one year** from the original remittance advice date to dispute"
  " a claim.\n*   **Aetna Better Health** gives providers **90 calendar days** from the claim de"
  "nial date to file a claims dispute.",
  "A/B v2 arm — cid 20a6c589, qc 0.971"),
]
