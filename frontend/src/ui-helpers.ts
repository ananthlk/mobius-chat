// ui-helpers — shared, self-contained UI primitives extracted from app.ts (markdown +
// confidence/QC badges + small shared types). Both app.ts and render/* import from here,
// so no render module needs to reach back into the app.ts monolith. No app state, DOM/string only.

export interface QcAuditInfo {
  passed: boolean;
  /** Canonical rubric verdict (PASS / PARTIAL / FAIL); ``passed`` is true for PASS and PARTIAL. */
  adjudication_verdict?: string;
  reason?: string;
  source?: string;
  audited_at?: string;
  /** Post-run / eval automated score 0–1 */
  automated_score?: number;
  /** Human override 0–1 (persisted in chat_turns.qc_audit) */
  user_score?: number;
  user_score_comment?: string | null;
  user_score_updated_at?: string;
  score?: number;
  /** Rubric dimension → 0–1 (post-run JSON adjudicator or eval POST) */
  sub_scores?: Record<string, number>;
  adjudicator_full_response?: string;
  adjudicator_model?: string;
  adjudicator_llm_call_id?: string;
}
export interface FollowupLineNormalized {
  text: string;
  clickable: boolean;
}
export function simpleMarkdownToHtml(text: string): string {
  const s = (text ?? "").trim();
  if (!s) return "";
  const escape = (t: string) =>
    t
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  const imgs: string[] = [];
  // Match ![alt](url) - prefer data:image/ for charts; fallback to general URL
  const imgRe = /!\[([^\]]*)\]\(([^)]+)\)/g;
  let out = s.replace(imgRe, (_m, alt: string, url: string) => {
    const escapedAlt = escape(alt || "");
    const i = imgs.length;
    imgs.push(`<img src="${url}" alt="${escapedAlt}" class="report-chart" loading="lazy" />`);
    return `\uE000${i}\uE001`;
  });

  // Stash links/emails/phones before escaping so special chars in URLs survive.
  // Uses PUA codepoints \uE010/\uE011 (distinct from image stash \uE000/\uE001).
  const links: string[] = [];
  const stashLink = (html: string): string => {
    const i = links.length;
    links.push(html);
    return `\uE010${i}\uE011`;
  };
  // Only Mobius agent URLs (mobius-*.run.app) become purple pill buttons.
  // All other URLs remain plain text — no linkification.
  const MOBIUS_URL_RE = /https:\/\/mobius-[a-z0-9\-]+\.(?:a\.run\.app|us-central1\.run\.app)[^\s"'<>()[\]]*[^\s"'<>()[\].,!?;:]/g;
  const EMAIL_RE = /[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/g;
  const PHONE_RE = /(?:\+?1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]\d{3}[\s.\-]\d{4}/g;
  // Markdown [text](url): agent URLs → pill button; external URLs → just the link text
  out = out.replace(/\[([^\]]+)\]\((https:\/\/[^)]+)\)/g, (_m, linkText: string, url: string) => {
    if (/^https:\/\/mobius-/.test(url)) {
      return stashLink(`<a href="${url}" class="chat-link chat-link--url" target="_blank" rel="noopener noreferrer" title="${url}">${linkText} ↗</a>`);
    }
    return linkText;
  });
  out = out.replace(MOBIUS_URL_RE, (url: string) => {
    let display = url;
    try {
      display = new URL(url).hostname
        .replace(/\.(?:a\.run|us-central1\.run)\.app$/, "")
        .replace(/^mobius-/, "")
        .replace(/-[a-z0-9]+-uc$/, "");
    } catch { display = url.length > 40 ? url.slice(0, 39) + "…" : url; }
    return stashLink(`<a href="${url}" class="chat-link chat-link--url" target="_blank" rel="noopener noreferrer" title="${url}">${display} ↗</a>`);
  });
  out = out.replace(EMAIL_RE, (email: string) =>
    stashLink(`<a href="mailto:${email}" class="chat-link chat-link--email">${email}</a>`)
  );
  out = out.replace(PHONE_RE, (raw: string) => {
    const digits = raw.replace(/[^\d+]/g, "");
    return stashLink(`<a href="tel:${digits}" class="chat-link chat-link--tel">${raw}</a>`);
  });

  out = escape(out);
  imgs.forEach((img, i) => {
    out = out.replace(`\uE000${i}\uE001`, img);
  });
  links.forEach((html, i) => {
    out = out.replace(`\uE010${i}\uE011`, html);
  });
  out = out.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
  out = out.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  out = out.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  out = out.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\n\n+/g, "</p><p>");
  out = out.replace(/\n/g, "<br>\n");
  return "<p>" + out + "</p>";
}

/** Same as simpleMarkdownToHtml but does not escape HTML. Use only for trusted backend content (e.g. inside npi-profile-card). */
export function simpleMarkdownToHtmlInner(text: string): string {
  const s = (text ?? "").trim();
  if (!s) return "";
  let out = s;
  out = out.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
  out = out.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  out = out.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  out = out.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/^- (.+)$/gm, "<li>$1</li>");
  out = out.replace(/\n\n+/g, "</p><p>");
  out = out.replace(/\n/g, "<br>\n");
  out = "<p>" + out + "</p>";
  // Wrap consecutive <li> in <ul>
  out = out.replace(/((?:<li>[\s\S]*?<\/li>(?:<br>\s*)?)+)/g, "<ul>$1</ul>");
  return out;
}

/** Roster step markdown: preserves <div class="npi-profile-card"> and renders markdown inside it (for chat/collapsible). */
export function rosterStepMarkdownToHtml(text: string): string {
  const s = (text ?? "").trim();
  if (!s) return "";
  if (!s.includes("npi-profile-card")) {
    return simpleMarkdownToHtml(s);
  }
  const cardBlocks: string[] = [];
  const placeholder = (i: number) => `\uE000CARD${i}\uE001`;
  const re = /<div class="npi-profile-card" markdown="1">\s*([\s\S]*?)<\/div>/g;
  let out = s.replace(re, (_full: string, inner: string) => {
    const i = cardBlocks.length;
    cardBlocks.push(inner);
    return placeholder(i);
  });
  out = simpleMarkdownToHtml(out);
  cardBlocks.forEach((inner, i) => {
    const cardHtml = '<div class="npi-profile-card">' + simpleMarkdownToHtmlInner(inner) + "</div>";
    out = out.replace(placeholder(i), cardHtml);
  });
  return out;
}
export const CONFIDENCE_BADGE_MAP: Record<
  string,
  { label: string; variant: string; icon: string }
> = {
  approved_authoritative: {
    label: "Approved – Authoritative",
    variant: "approved_authoritative",
    icon: "check",
  },
  approved_informational: {
    label: "Approved – Informational",
    variant: "approved_informational",
    icon: "shield",
  },
  proceed_with_caution: {
    label: "Proceed with Caution",
    variant: "proceed_with_caution",
    icon: "alert-triangle",
  },
  augmented_with_google: {
    label: "Augmented with External Search",
    variant: "augmented_with_google",
    icon: "globe",
  },
  informational_only: {
    label: "Informational Only",
    variant: "informational_only",
    icon: "info",
  },
  no_sources: {
    label: "No Sources",
    variant: "no_sources",
    icon: "alert-circle",
  },
};

export function renderConfidenceBadge(strip: string): HTMLElement {
  const key = strip.toLowerCase().replace(/\s+/g, "_");
  const cfg = CONFIDENCE_BADGE_MAP[key] ?? {
    label: strip.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
    variant: "unverified",
    icon: "info",
  };
  const wrap = document.createElement("div");
  wrap.className = "confidence-badge-wrap";
  const badge = document.createElement("span");
  badge.className = `confidence-badge confidence-badge--${cfg.variant}`;
  badge.setAttribute("aria-label", "Source confidence: " + cfg.label);

  const iconEl = document.createElement("span");
  iconEl.className = "confidence-badge-icon";
  iconEl.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  const paths: Record<string, string> = {
    check: "M20 6L9 17l-5-5",
    shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
    "alert-triangle": "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
    globe: "M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9",
    info: "M12 16v-4 M12 8h.01 M22 12c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2s10 4.477 10 10z",
    "alert-circle": "M12 8v4m0 4h.01M22 12c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2s10 4.477 10 10z",
  };
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", paths[cfg.icon] ?? paths.info);
  svg.appendChild(path);
  iconEl.appendChild(svg);

  const labelEl = document.createElement("span");
  labelEl.className = "confidence-badge-label";
  labelEl.textContent = cfg.label;

  badge.appendChild(iconEl);
  badge.appendChild(labelEl);
  wrap.appendChild(badge);
  return wrap;
}

/** Neutral shield icon — no semantic color (stroke only). */
export function createQcSampleShieldSvg(): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "qc-audit-badge-shield-svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "11");
  svg.setAttribute("height", "11");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.35");
  path.setAttribute("stroke-linejoin", "round");
  path.setAttribute(
    "d",
    "M12 2.5 19.5 5.2v5.8c0 3.2-2.4 6.5-7.5 8.5-5.1-2-7.5-5.3-7.5-8.5V5.2L12 2.5z"
  );
  svg.appendChild(path);
  return svg;
}

/**
 * Subtle end-user marker: post-run QA / adjudication ran on this turn (when server merges qc_audit).
 * Omits pass/fail in the strip — admins see scores in the QA / Adjudicator panel.
 */
export function renderQcAuditBadge(_qc: QcAuditInfo): HTMLElement {
  void _qc;
  const wrap = document.createElement("div");
  wrap.className = "qc-audit-badge-wrap";
  wrap.setAttribute("data-qc-sample", "1");

  const row = document.createElement("div");
  row.className = "qc-audit-badge-row";

  const badge = document.createElement("span");
  badge.className = "qc-audit-badge qc-audit-badge--neutral";
  badge.setAttribute(
    "aria-label",
    "This reply was checked by an automated quality review. It does not change your answer."
  );

  const iconEl = document.createElement("span");
  iconEl.className = "qc-audit-badge-icon";
  iconEl.setAttribute("aria-hidden", "true");
  iconEl.appendChild(createQcSampleShieldSvg());

  const labelEl = document.createElement("span");
  labelEl.className = "qc-audit-badge-label";
  labelEl.textContent = "Quality review completed";

  badge.appendChild(iconEl);
  badge.appendChild(labelEl);
  row.appendChild(badge);
  wrap.appendChild(row);

  const foot = document.createElement("p");
  foot.className = "qc-audit-badge-footnote";
  foot.textContent = "Does not change your answer.";

  wrap.appendChild(foot);
  return wrap;
}
