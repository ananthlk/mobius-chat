// src/ui-helpers.ts
function simpleMarkdownToHtml(text) {
  const s = (text ?? "").trim();
  if (!s)
    return "";
  const escape = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const imgs = [];
  const imgRe = /!\[([^\]]*)\]\(([^)]+)\)/g;
  let out = s.replace(imgRe, (_m, alt, url) => {
    const escapedAlt = escape(alt || "");
    const i = imgs.length;
    imgs.push(`<img src="${url}" alt="${escapedAlt}" class="report-chart" loading="lazy" />`);
    return `\uE000${i}\uE001`;
  });
  const links = [];
  const stashLink = (html) => {
    const i = links.length;
    links.push(html);
    return `\uE010${i}\uE011`;
  };
  const MOBIUS_URL_RE = /https:\/\/mobius-[a-z0-9\-]+\.(?:a\.run\.app|us-central1\.run\.app)[^\s"'<>()[\]]*[^\s"'<>()[\].,!?;:]/g;
  const EMAIL_RE = /[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/g;
  const PHONE_RE = /(?:\+?1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]\d{3}[\s.\-]\d{4}/g;
  out = out.replace(/\[([^\]]+)\]\((https:\/\/[^)]+)\)/g, (_m, linkText, url) => {
    if (/^https:\/\/mobius-/.test(url)) {
      return stashLink(`<a href="${url}" class="chat-link chat-link--url" target="_blank" rel="noopener noreferrer" title="${url}">${linkText} \u2197</a>`);
    }
    return linkText;
  });
  out = out.replace(MOBIUS_URL_RE, (url) => {
    let display = url;
    try {
      display = new URL(url).hostname.replace(/\.(?:a\.run|us-central1\.run)\.app$/, "").replace(/^mobius-/, "").replace(/-[a-z0-9]+-uc$/, "");
    } catch {
      display = url.length > 40 ? url.slice(0, 39) + "\u2026" : url;
    }
    return stashLink(`<a href="${url}" class="chat-link chat-link--url" target="_blank" rel="noopener noreferrer" title="${url}">${display} \u2197</a>`);
  });
  out = out.replace(
    EMAIL_RE,
    (email) => stashLink(`<a href="mailto:${email}" class="chat-link chat-link--email">${email}</a>`)
  );
  out = out.replace(PHONE_RE, (raw) => {
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

// src/render/bubble.ts
var MAX_BULLETS_PER_SECTION = 4;
function _renderSectionBody(sec, body) {
  const fmt = sec.format ?? "bullets";
  const data = sec.data;
  if (fmt === "table" && data?.headers && data?.rows) {
    const tbl = document.createElement("table");
    tbl.className = "ac-fmt-table";
    const thead = tbl.createTHead();
    const hRow = thead.insertRow();
    data.headers.forEach((h) => {
      const th = document.createElement("th");
      th.innerHTML = _inlineMd(h);
      hRow.appendChild(th);
    });
    const tbody = tbl.createTBody();
    data.rows.forEach((row) => {
      const tr = tbody.insertRow();
      row.forEach((cell) => {
        const td = tr.insertCell();
        td.innerHTML = _inlineMd(cell);
      });
    });
    const scroll = document.createElement("div");
    scroll.className = "ac-fmt-table-scroll";
    scroll.appendChild(tbl);
    body.appendChild(scroll);
    return;
  }
  if (fmt === "steps" && data?.items) {
    const ol = document.createElement("ol");
    ol.className = "ac-fmt-steps";
    data.items.forEach((item) => {
      const li = document.createElement("li");
      li.className = "ac-fmt-step";
      li.innerHTML = _inlineMd(typeof item === "string" ? item : item.label ?? "");
      ol.appendChild(li);
    });
    body.appendChild(ol);
    return;
  }
  if (fmt === "stats" && data?.items) {
    const grid = document.createElement("div");
    grid.className = "ac-fmt-stats";
    data.items.slice(0, 4).forEach((item) => {
      const tile = document.createElement("div");
      tile.className = "ac-fmt-stat-tile";
      const val = document.createElement("div");
      val.className = "ac-fmt-stat-value";
      val.innerHTML = _inlineMd(item.value ?? "");
      const lbl = document.createElement("div");
      lbl.className = "ac-fmt-stat-label";
      lbl.innerHTML = _inlineMd(item.label ?? "");
      tile.appendChild(val);
      tile.appendChild(lbl);
      if (item.note) {
        const note = document.createElement("div");
        note.className = "ac-fmt-stat-note";
        note.innerHTML = _inlineMd(item.note);
        tile.appendChild(note);
      }
      grid.appendChild(tile);
    });
    body.appendChild(grid);
    return;
  }
  if (fmt === "bars" && data?.items) {
    const list = document.createElement("div");
    list.className = "ac-fmt-bars";
    data.items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "ac-fmt-bar-row";
      const lbl = document.createElement("div");
      lbl.className = "ac-fmt-bar-label";
      lbl.innerHTML = _inlineMd(item.label ?? "");
      const track = document.createElement("div");
      track.className = "ac-fmt-bar-track";
      const fill = document.createElement("div");
      fill.className = "ac-fmt-bar-fill";
      const pct = Math.round(Math.min(1, Math.max(0, item.weight ?? 0)) * 100);
      fill.style.width = `${pct}%`;
      track.appendChild(fill);
      row.appendChild(lbl);
      row.appendChild(track);
      if (item.note) {
        const note = document.createElement("div");
        note.className = "ac-fmt-bar-note";
        note.innerHTML = _inlineMd(item.note);
        row.appendChild(note);
      }
      list.appendChild(row);
    });
    body.appendChild(list);
    return;
  }
  if (fmt === "conditions" && data?.items) {
    const list = document.createElement("div");
    list.className = "ac-fmt-conditions";
    data.items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "ac-fmt-condition-row";
      const cond = document.createElement("div");
      cond.className = "ac-fmt-condition-if";
      cond.innerHTML = _inlineMd(item.condition ?? "");
      const result = document.createElement("div");
      result.className = "ac-fmt-condition-then";
      result.innerHTML = _inlineMd(item.result ?? "");
      row.appendChild(cond);
      row.appendChild(result);
      list.appendChild(row);
    });
    body.appendChild(list);
    return;
  }
  if (fmt === "appeals_rules") {
    _renderAppealsRules(sec, body);
    return;
  }
  if (fmt === "appeals_playbook") {
    _renderAppealsPlaybook(sec, body);
    return;
  }
  const allBullets = sec.bullets ?? [];
  const visibleBullets = allBullets.slice(0, MAX_BULLETS_PER_SECTION);
  const hiddenBullets = allBullets.slice(MAX_BULLETS_PER_SECTION);
  visibleBullets.forEach((b) => {
    const li = document.createElement("div");
    li.className = "answer-card-bullet";
    li.innerHTML = _inlineMd(b);
    body.appendChild(li);
  });
  if (hiddenBullets.length > 0) {
    const overflow = document.createElement("div");
    overflow.className = "answer-card-bullets-overflow";
    overflow.style.display = "none";
    hiddenBullets.forEach((b) => {
      const li = document.createElement("div");
      li.className = "answer-card-bullet";
      li.innerHTML = _inlineMd(b);
      overflow.appendChild(li);
    });
    body.appendChild(overflow);
    const more = document.createElement("button");
    more.type = "button";
    more.className = "answer-card-more";
    more.setAttribute("aria-label", "Show more bullets");
    more.textContent = `Show ${hiddenBullets.length} more`;
    let expanded = false;
    more.addEventListener("click", () => {
      expanded = !expanded;
      overflow.style.display = expanded ? "" : "none";
      more.textContent = expanded ? "Show less" : `Show ${hiddenBullets.length} more`;
    });
    body.appendChild(more);
  }
}
function _chip(text, cls) {
  const el2 = document.createElement("span");
  el2.className = cls;
  el2.textContent = text;
  return el2;
}
function _safeHttpUrl(url) {
  if (typeof url !== "string" || !url.trim())
    return null;
  try {
    const u = new URL(url.trim());
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}
function _renderAppealsRules(sec, body) {
  const data = sec.data ?? {};
  const wrap = document.createElement("div");
  wrap.className = "ac-appeals-rules";
  if (data.carc || data.carc_title || data.archetype) {
    const head = document.createElement("div");
    head.className = "ac-appeals-head";
    if (data.carc)
      head.appendChild(_chip(`CARC ${data.carc}`, "ac-appeals-carc"));
    if (data.carc_title) {
      const t = document.createElement("span");
      t.className = "ac-appeals-carc-title";
      t.textContent = data.carc_title;
      head.appendChild(t);
    }
    if (data.archetype)
      head.appendChild(_chip(data.archetype, "ac-appeals-archetype"));
    wrap.appendChild(head);
  }
  const rules = Array.isArray(data.rules) ? data.rules : [];
  if (rules.length === 0) {
    const empty = document.createElement("div");
    empty.className = "ac-appeals-empty";
    empty.textContent = "No appeal rules on file for this CARC.";
    wrap.appendChild(empty);
    body.appendChild(wrap);
    return;
  }
  rules.forEach((rule) => {
    const det = document.createElement("details");
    det.className = "ac-appeals-rule";
    const sum = document.createElement("summary");
    sum.className = "ac-appeals-rule-summary";
    if (rule.rule_id)
      sum.appendChild(_chip(rule.rule_id, "ac-appeals-rule-id"));
    const name = document.createElement("span");
    name.className = "ac-appeals-rule-name";
    name.textContent = rule.rule_name || "Appeal rule";
    sum.appendChild(name);
    if (rule.triggers_when) {
      const trig = document.createElement("span");
      trig.className = "ac-appeals-rule-trigger-brief";
      trig.textContent = rule.triggers_when;
      sum.appendChild(trig);
    }
    det.appendChild(sum);
    const expand = document.createElement("div");
    expand.className = "ac-appeals-rule-body";
    if (rule.triggers_when) {
      const row = document.createElement("div");
      row.className = "ac-appeals-field ac-appeals-field--trigger";
      const lbl = _chip("Applies when", "ac-appeals-field-label");
      const val = document.createElement("div");
      val.className = "ac-appeals-field-value";
      val.textContent = rule.triggers_when;
      row.appendChild(lbl);
      row.appendChild(val);
      expand.appendChild(row);
    }
    if (rule.appeal_argument) {
      const arg = document.createElement("div");
      arg.className = "ac-appeals-argument";
      const lbl = _chip("Appeal argument", "ac-appeals-argument-label");
      const val = document.createElement("div");
      val.className = "ac-appeals-argument-value";
      val.textContent = rule.appeal_argument;
      arg.appendChild(lbl);
      arg.appendChild(val);
      expand.appendChild(arg);
    }
    if (rule.rule_statement) {
      const st = document.createElement("div");
      st.className = "ac-appeals-statement";
      st.textContent = rule.rule_statement;
      expand.appendChild(st);
    }
    const requires = Array.isArray(rule.requires) ? rule.requires : [];
    if (requires.length) {
      const row = document.createElement("div");
      row.className = "ac-appeals-field ac-appeals-field--requires";
      row.appendChild(_chip("Requires", "ac-appeals-field-label"));
      const ul = document.createElement("ul");
      ul.className = "ac-appeals-requires";
      requires.forEach((r) => {
        const li = document.createElement("li");
        li.textContent = r;
        ul.appendChild(li);
      });
      row.appendChild(ul);
      expand.appendChild(row);
    }
    if (rule.authority_notes) {
      const auth = document.createElement("div");
      auth.className = "ac-appeals-authority";
      auth.appendChild(_chip("Authority", "ac-appeals-authority-label"));
      const val = document.createElement("span");
      val.className = "ac-appeals-authority-value";
      val.textContent = rule.authority_notes;
      auth.appendChild(val);
      expand.appendChild(auth);
    }
    const variants = Array.isArray(rule.payor_variants) ? rule.payor_variants : [];
    if (variants.length) {
      const pills = document.createElement("div");
      pills.className = "ac-appeals-variants";
      variants.forEach((v) => {
        const label = typeof v === "string" ? v : [v.payor, v.note].filter(Boolean).join(" \u2014 ");
        if (label)
          pills.appendChild(_chip(label, "ac-appeals-variant-pill"));
      });
      if (pills.childElementCount)
        expand.appendChild(pills);
    }
    det.appendChild(expand);
    wrap.appendChild(det);
  });
  const adminUrl = _safeHttpUrl(data.admin_url);
  if (adminUrl) {
    const footer = document.createElement("div");
    footer.className = "ac-appeals-admin";
    const a = document.createElement("a");
    a.className = "ac-appeals-admin-link";
    a.href = adminUrl;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = "\u270F Edit rules in Admin \u2192";
    footer.appendChild(a);
    wrap.appendChild(footer);
  }
  body.appendChild(wrap);
}
function _inlineMd(text) {
  const esc = String(text ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc.replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+?)`/g, "<code>$1</code>").replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, "$1<em>$2</em>");
}
function _renderAppealsPlaybook(sec, body) {
  const data = sec.data ?? {};
  const wrap = document.createElement("div");
  wrap.className = "ac-appeals-playbook";
  if (data.found === false) {
    const empty = document.createElement("div");
    empty.className = "ac-appeals-playbook-empty";
    empty.textContent = data.message || "No appeal playbook on file for this payor.";
    wrap.appendChild(empty);
    body.appendChild(wrap);
    return;
  }
  const mkRow = (icon, label, value) => {
    const row = document.createElement("div");
    row.className = "ac-pb-row";
    const ic = document.createElement("span");
    ic.className = "ac-pb-row-icon";
    ic.textContent = icon;
    const bd = document.createElement("span");
    bd.className = "ac-pb-row-body";
    if (label) {
      const b = document.createElement("b");
      b.className = "ac-pb-row-label";
      b.textContent = label + " ";
      bd.appendChild(b);
    }
    const val = document.createElement("span");
    val.innerHTML = _inlineMd(value);
    bd.appendChild(val);
    row.appendChild(ic);
    row.appendChild(bd);
    return row;
  };
  const head = document.createElement("div");
  head.className = "ac-appeals-playbook-head";
  const title = document.createElement("b");
  title.className = "ac-appeals-playbook-title";
  const _carcLabel = data.carc ? String(data.carc) : Array.isArray(data.carc_codes) && data.carc_codes.length ? data.carc_codes.join(", ") : "";
  const parts = ["\u{1F4D8}"];
  if (_carcLabel)
    parts.push(`CARC ${_carcLabel}`);
  if (data.payor)
    parts.push((_carcLabel ? "\xD7 " : "") + data.payor);
  title.textContent = parts.join(" ") + " \u2014 Playbook";
  head.appendChild(title);
  const CONF_BADGE = {
    0: ["GENERATED", "generated"],
    1: ["REVIEWED", "reviewed"],
    2: ["PUBLISHED", "published"],
    3: ["VALIDATED", "validated"]
  };
  const cl = data.confidence_level;
  if (typeof cl === "number" && CONF_BADGE[cl]) {
    const [label, cls] = CONF_BADGE[cl];
    const badge = document.createElement("span");
    badge.className = "ac-pb-badge ac-pb-badge--" + cls;
    badge.textContent = label;
    head.appendChild(badge);
  }
  wrap.appendChild(head);
  if (cl === 0) {
    const draft = document.createElement("div");
    draft.className = "ac-pb-draft-label";
    draft.textContent = "\u26A0 Draft \u2014 not yet reviewed";
    wrap.appendChild(draft);
  }
  if (data.description && data.description.trim()) {
    const desc = document.createElement("div");
    desc.className = "ac-pb-description";
    desc.innerHTML = _inlineMd(data.description.trim());
    wrap.appendChild(desc);
  }
  if (data.what_it_usually_means && data.what_it_usually_means.trim()) {
    const wtum = document.createElement("div");
    wtum.className = "ac-pb-usually-means";
    wtum.innerHTML = _inlineMd(data.what_it_usually_means.trim());
    wrap.appendChild(wtum);
  }
  const mkSection = (label) => {
    const section = document.createElement("div");
    section.className = "ac-pb-section";
    const h = document.createElement("div");
    h.className = "ac-pb-section-heading";
    h.textContent = label;
    section.appendChild(h);
    const b = document.createElement("div");
    b.className = "ac-appeals-playbook-rows";
    section.appendChild(b);
    return b;
  };
  const rows = mkSection("Deadlines & Appeal Strategy");
  const dParts = [];
  if (typeof data.deadline_appeal_days === "number")
    dParts.push(`appeal ${data.deadline_appeal_days}d from denial`);
  if (typeof data.deadline_resubmit_days === "number") {
    dParts.push(`resubmit ${data.deadline_resubmit_days}d from DOS${data.deadline_resubmit_note ? ` (${data.deadline_resubmit_note})` : ""}`);
  }
  if (dParts.length)
    rows.appendChild(mkRow("\u23F1", "Deadlines:", dParts.join(" \xB7 ")));
  if (data.strategy && data.strategy.trim())
    rows.appendChild(mkRow("\u{1F3AF}", "Strategy:", data.strategy.trim()));
  const levels = Array.isArray(data.appeal_levels) ? data.appeal_levels : [];
  if (levels.length) {
    const LADDER_CAP = 3;
    const ladderRow = document.createElement("div");
    ladderRow.className = "ac-pb-row ac-pb-row--ladder";
    const ic = document.createElement("span");
    ic.className = "ac-pb-row-icon";
    ic.textContent = "\u21B3";
    const bd = document.createElement("span");
    bd.className = "ac-pb-row-body";
    const ol = document.createElement("ol");
    ol.className = "ac-appeals-levels-list";
    const mkLevel = (lv) => {
      const li = document.createElement("li");
      li.className = "ac-appeals-level";
      const head2 = document.createElement("span");
      head2.className = "ac-appeals-level-head";
      const name = document.createElement("span");
      name.className = "ac-appeals-level-name";
      name.textContent = lv.name || (lv.level != null ? `Level ${lv.level}` : "Level");
      head2.appendChild(name);
      if (lv.submission && lv.submission.trim()) {
        const via = document.createElement("span");
        via.className = "ac-appeals-level-via";
        via.textContent = "\xB7 " + lv.submission.trim();
        head2.appendChild(via);
      }
      if (typeof lv.deadline_days === "number")
        head2.appendChild(_chip(`${lv.deadline_days}d`, "ac-appeals-level-deadline"));
      li.appendChild(head2);
      if (lv.notes && lv.notes.trim()) {
        const note = document.createElement("span");
        note.className = "ac-appeals-level-note";
        note.innerHTML = _inlineMd(lv.notes);
        li.appendChild(note);
      }
      return li;
    };
    levels.slice(0, LADDER_CAP).forEach((lv) => ol.appendChild(mkLevel(lv)));
    bd.appendChild(ol);
    if (levels.length > LADDER_CAP) {
      const extra = document.createElement("ol");
      extra.className = "ac-appeals-levels-list ac-appeals-levels-extra";
      extra.style.display = "none";
      extra.setAttribute("start", String(LADDER_CAP + 1));
      levels.slice(LADDER_CAP).forEach((lv) => extra.appendChild(mkLevel(lv)));
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "ac-appeals-levels-more";
      toggle.textContent = `+${levels.length - LADDER_CAP} more`;
      toggle.addEventListener("click", () => {
        const open = extra.style.display === "none";
        extra.style.display = open ? "" : "none";
        toggle.textContent = open ? "Show fewer" : `+${levels.length - LADDER_CAP} more`;
      });
      bd.appendChild(extra);
      bd.appendChild(toggle);
    }
    ladderRow.appendChild(ic);
    ladderRow.appendChild(bd);
    rows.appendChild(ladderRow);
  }
  const guidance = Array.isArray(data.guidance) ? data.guidance : [];
  if (guidance.length) {
    guidance.forEach((g) => {
      if (!g || !g.text)
        return;
      const row = document.createElement("div");
      row.className = "ac-pb-row ac-pb-row--guidance";
      const ic = document.createElement("span");
      ic.className = "ac-pb-row-icon";
      ic.textContent = "\u{1F4A1}";
      const bd = document.createElement("span");
      bd.className = "ac-pb-row-body";
      const t = document.createElement("span");
      t.className = "ac-pb-guide-text";
      t.innerHTML = _inlineMd(g.text);
      bd.appendChild(t);
      if (g.detail && g.detail.trim()) {
        const d = document.createElement("span");
        d.className = "ac-pb-guide-detail";
        d.innerHTML = _inlineMd(g.detail);
        bd.appendChild(d);
      }
      row.appendChild(ic);
      row.appendChild(bd);
      rows.appendChild(row);
    });
  } else {
    const questions = Array.isArray(data.questions) ? data.questions : [];
    questions.forEach((q, i) => {
      if (!q || !q.text)
        return;
      const n = typeof q.n === "number" ? q.n : i + 1;
      const text = q.text + (q.hint ? ` (${q.hint})` : "");
      rows.appendChild(mkRow(`${n}.`, null, text));
    });
  }
  if (rows.childElementCount)
    wrap.appendChild(rows.parentElement);
  const docs = (Array.isArray(data.docs_required) ? [...data.docs_required] : []).sort((a, b) => (a?.required === false ? 1 : 0) - (b?.required === false ? 1 : 0));
  if (docs.length) {
    const docsBody = mkSection("Documentation");
    const docsRow = document.createElement("div");
    docsRow.className = "ac-pb-row ac-pb-row--docs";
    const ic = document.createElement("span");
    ic.className = "ac-pb-row-icon";
    ic.textContent = "\u{1F4CE}";
    const bd = document.createElement("span");
    bd.className = "ac-pb-row-body";
    const lbl = document.createElement("b");
    lbl.className = "ac-pb-row-label";
    lbl.textContent = "Docs: ";
    bd.appendChild(lbl);
    const ul = document.createElement("ul");
    ul.className = "ac-appeals-docs-list";
    docs.forEach((d) => {
      const li = document.createElement("li");
      li.className = d.required === false ? "ac-appeals-doc ac-appeals-doc--optional" : "ac-appeals-doc ac-appeals-doc--required";
      const mark = document.createElement("span");
      mark.className = "ac-appeals-doc-mark";
      mark.textContent = d.required === false ? "\u25CB" : "\u25CF";
      const head2 = document.createElement("span");
      head2.className = "ac-appeals-doc-head";
      const optSuffix = d.required === false ? " (optional)" : "";
      const docUrl = _safeHttpUrl(d.url);
      if (docUrl) {
        const a = document.createElement("a");
        a.className = "ac-appeals-doc-link";
        a.href = docUrl;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.innerHTML = _inlineMd(d.doc || "Form");
        const dl = document.createElement("span");
        dl.className = "ac-appeals-doc-dl";
        dl.textContent = " \u2B07";
        a.appendChild(dl);
        head2.appendChild(a);
        if (optSuffix)
          head2.appendChild(document.createTextNode(optSuffix));
      } else {
        const txt = document.createElement("span");
        txt.className = "ac-appeals-doc-text";
        txt.innerHTML = _inlineMd(d.doc || "") + optSuffix;
        head2.appendChild(txt);
      }
      li.appendChild(mark);
      li.appendChild(head2);
      if (d.notes && d.notes.trim()) {
        const note = document.createElement("span");
        note.className = "ac-appeals-doc-note";
        note.innerHTML = _inlineMd(d.notes);
        li.appendChild(note);
      }
      ul.appendChild(li);
    });
    bd.appendChild(ul);
    docsRow.appendChild(ic);
    docsRow.appendChild(bd);
    docsBody.appendChild(docsRow);
    wrap.appendChild(docsBody.parentElement);
  }
  const portalUrl = _safeHttpUrl(data.portal_url);
  if (data.submission_method || portalUrl || data.fax || data.mail_address) {
    const subBody = mkSection("Submission");
    const subRow = document.createElement("div");
    subRow.className = "ac-pb-row ac-pb-row--submit";
    const ic = document.createElement("span");
    ic.className = "ac-pb-row-icon";
    ic.textContent = "\u{1F4E4}";
    const bd = document.createElement("span");
    bd.className = "ac-pb-row-body";
    const lbl = document.createElement("b");
    lbl.className = "ac-pb-row-label";
    lbl.textContent = "Submit: ";
    bd.appendChild(lbl);
    const bits = [];
    if (portalUrl) {
      const a = document.createElement("a");
      a.className = "ac-appeals-portal";
      a.href = portalUrl;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = "provider portal";
      bits.push(a);
    }
    if (data.fax)
      bits.push(document.createTextNode(`fax ${data.fax}`));
    if (data.mail_address)
      bits.push(document.createTextNode(`mail ${data.mail_address}`));
    if (bits.length === 0 && data.submission_method) {
      const m = document.createElement("span");
      m.innerHTML = _inlineMd(data.submission_method);
      bits.push(m);
    }
    bits.forEach((node, i) => {
      if (i)
        bd.appendChild(document.createTextNode(" \xB7 "));
      bd.appendChild(node);
    });
    subRow.appendChild(ic);
    subRow.appendChild(bd);
    subBody.appendChild(subRow);
    wrap.appendChild(subBody.parentElement);
  }
  let adminHref = _safeHttpUrl(data.admin_url);
  if (adminHref) {
    try {
      const u = new URL(adminHref);
      const sameOrigin = typeof window !== "undefined" && !!window.location && u.origin === window.location.origin;
      const isAppealsHost = /(^|\.)mobius-appeals[\w.-]*\.run\.app$/i.test(u.hostname);
      if (!sameOrigin && !isAppealsHost)
        adminHref = null;
    } catch {
      adminHref = null;
    }
  }
  if (!adminHref && data.admin_edit && (data.admin_edit.carc || data.admin_edit.payor)) {
    const qs = new URLSearchParams();
    if (data.admin_edit.carc)
      qs.set("carc", data.admin_edit.carc);
    if (data.admin_edit.payor)
      qs.set("payor", data.admin_edit.payor);
    qs.set("tab", "playbook");
    const origin = typeof window !== "undefined" && window.location && window.location.origin || "";
    adminHref = _safeHttpUrl(`${origin}/admin/rules-library?${qs.toString()}`);
  }
  if (adminHref) {
    const footer = document.createElement("div");
    footer.className = "ac-appeals-admin";
    const a = document.createElement("a");
    a.className = "ac-appeals-admin-link";
    a.href = adminHref;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = "\u270F Edit this playbook in Admin \u2192";
    footer.appendChild(a);
    wrap.appendChild(footer);
  }
  body.appendChild(wrap);
}
var COLLAPSIBLE_CARD_FORMATS = /* @__PURE__ */ new Set(["table", "stats", "steps", "bars", "conditions"]);
function _sectionHasContent(sec) {
  const fmt = sec.format ?? "bullets";
  if (fmt === "appeals_playbook" || fmt === "appeals_rules")
    return true;
  const d = sec.data ?? {};
  if (fmt === "table")
    return Array.isArray(d.rows) && d.rows.length > 0;
  if (fmt === "stats" || fmt === "steps" || fmt === "bars" || fmt === "conditions")
    return Array.isArray(d.items) && d.items.length > 0;
  return Array.isArray(sec.bullets) && sec.bullets.length > 0;
}
function renderOneSection(sec) {
  const fmt = sec.format ?? "bullets";
  if (!_sectionHasContent(sec))
    return null;
  const sectionEl = document.createElement("div");
  sectionEl.className = `answer-card-section answer-card-section--${fmt}`;
  if (COLLAPSIBLE_CARD_FORMATS.has(fmt) && (sec.label || "").trim()) {
    const header = document.createElement("button");
    header.type = "button";
    header.className = "answer-card-section-label ac-card-toggle";
    const lblText = document.createElement("span");
    lblText.className = "ac-card-toggle-text";
    lblText.textContent = sec.label || "";
    const chev = document.createElement("span");
    chev.className = "ac-card-chevron";
    chev.setAttribute("aria-hidden", "true");
    chev.textContent = "\u25BE";
    header.appendChild(lblText);
    header.appendChild(chev);
    const body = document.createElement("div");
    body.className = "ac-card-body";
    _renderSectionBody(sec, body);
    const startCollapsed = sec.visibility === "detail";
    header.setAttribute("aria-expanded", startCollapsed ? "false" : "true");
    if (startCollapsed) {
      sectionEl.classList.add("ac-card--collapsed");
      body.style.maxHeight = "0px";
    }
    header.addEventListener("click", () => {
      const collapsing = !sectionEl.classList.contains("ac-card--collapsed");
      sectionEl.classList.toggle("ac-card--collapsed");
      header.setAttribute("aria-expanded", collapsing ? "false" : "true");
      body.style.maxHeight = collapsing ? "0px" : body.scrollHeight + "px";
    });
    sectionEl.appendChild(header);
    sectionEl.appendChild(body);
    return sectionEl;
  }
  const labelEl = document.createElement("div");
  labelEl.className = "answer-card-section-label";
  labelEl.textContent = sec.label || "";
  sectionEl.appendChild(labelEl);
  _renderSectionBody(sec, sectionEl);
  return sectionEl;
}
function renderFormatBlock(block) {
  const isDomain = block.type === "domain_card";
  const isBullets = block.type === "bullets";
  const section = {
    label: block.label ?? "",
    format: isDomain ? block.variant : block.type,
    visibility: "primary",
    bullets: isBullets ? block.items ?? [] : [],
    data: isDomain ? block.data : { items: block.items, headers: block.headers, rows: block.rows }
  };
  return renderOneSection(section);
}
function renderModeBadge(mode) {
  const m = String(mode ?? "").trim().toUpperCase();
  if (m !== "CANONICAL" && m !== "RECITAL")
    return null;
  const lbl = document.createElement("div");
  lbl.className = "ac-answer-mode-label ac-answer-mode-label--" + m.toLowerCase();
  lbl.textContent = m;
  return lbl;
}
function renderFirstPass(block) {
  const draft = (block.draft_markdown ?? "").trim();
  const rounds = (block.trace_rounds ?? []).map((r, i) => ({
    n: typeof r?.round === "number" ? r.round : i + 1,
    ans: (r?.running_answer ?? "").trim() || (r?.learned ?? "").trim(),
    isThought: !(r?.running_answer ?? "").trim() && !!(r?.learned ?? "").trim()
  })).filter((r) => r.ans.length > 0);
  if (!draft && rounds.length === 0)
    return null;
  const fp = document.createElement("div");
  fp.className = "ac-first-pass";
  const sum = document.createElement("button");
  sum.type = "button";
  sum.className = "ac-first-pass-summary";
  sum.textContent = rounds.length > 1 ? `First pass \xB7 ${rounds.length} rounds` : "First pass";
  const fpBody = document.createElement("div");
  fpBody.className = "ac-first-pass-body";
  if (rounds.length > 0) {
    rounds.forEach((r) => {
      const step = document.createElement("div");
      step.className = "ac-rd-step";
      const lbl = document.createElement("span");
      lbl.className = "ac-rd-label";
      lbl.textContent = "rd-" + r.n;
      const ans = document.createElement("div");
      ans.className = "ac-rd-answer" + (r.isThought ? " ac-rd-thought" : "");
      ans.innerHTML = simpleMarkdownToHtml(r.ans);
      step.appendChild(lbl);
      step.appendChild(ans);
      fpBody.appendChild(step);
    });
  } else {
    fpBody.innerHTML = simpleMarkdownToHtml(draft);
  }
  sum.addEventListener("click", () => {
    const opening = !fp.classList.contains("ac-first-pass--open");
    fp.classList.toggle("ac-first-pass--open");
    fpBody.style.maxHeight = opening ? fpBody.scrollHeight + "px" : "0px";
  });
  fp.appendChild(sum);
  fp.appendChild(fpBody);
  return fp;
}
function _proseBlock(cls, markdown) {
  const el2 = document.createElement("div");
  el2.className = cls;
  el2.innerHTML = simpleMarkdownToHtml(String(markdown ?? ""));
  return el2;
}
function _detailBlock(block) {
  const details = document.createElement("details");
  details.className = "envelope-detail";
  details.open = block.collapsed_default === false;
  const sum = document.createElement("summary");
  sum.textContent = "Details";
  details.appendChild(sum);
  const body = document.createElement("div");
  body.className = "envelope-detail-body";
  body.innerHTML = simpleMarkdownToHtml(String(block.markdown ?? ""));
  details.appendChild(body);
  return details;
}
function renderEnvelope(blocks, opts = {}) {
  const answerBody = document.createElement("div");
  answerBody.className = "ac-answer-final";
  let sources = null;
  const dropped = [];
  const FORMAT_TYPES = /* @__PURE__ */ new Set(["table", "stats", "bullets", "steps", "bars", "conditions", "domain_card"]);
  for (const block of blocks || []) {
    if (!block || typeof block !== "object" || typeof block.type !== "string")
      continue;
    const t = block.type;
    let el2 = null;
    if (t === "sources") {
      sources = block;
      continue;
    } else if (t === "mode_badge")
      el2 = renderModeBadge(block.mode);
    else if (FORMAT_TYPES.has(t))
      el2 = renderFormatBlock(block);
    else if (t === "first_pass")
      el2 = renderFirstPass(block);
    else if (t === "direct_answer")
      el2 = _proseBlock("ac-answer-envelope-body", block.markdown);
    else if (t === "tldr")
      el2 = _proseBlock("ac-answer-tldr", block.markdown);
    else if (t === "markdown_report")
      el2 = _proseBlock("envelope-markdown-report", block.markdown);
    else if (t === "detail")
      el2 = _detailBlock(block);
    else
      el2 = opts.renderExtraBlock ? opts.renderExtraBlock(block) : null;
    if (el2) {
      answerBody.appendChild(el2);
    } else if (t !== "mode_badge" && t !== "first_pass") {
      dropped.push(t);
      opts.onUnknownBlock?.(t);
    }
  }
  return { answerBody, sources, dropped };
}
function renderSourcesList(sources, onSourceClick) {
  if (!sources || sources.length === 0)
    return null;
  const wrap = document.createElement("div");
  wrap.className = "ac-sources-footnotes";
  const heading = document.createElement("div");
  heading.className = "ac-sources-footnotes-heading";
  heading.textContent = "Sources";
  wrap.appendChild(heading);
  const ol = document.createElement("ol");
  ol.className = "ac-sources-list";
  sources.forEach((src, i) => {
    const li = document.createElement("li");
    li.className = "ac-source-item";
    li.setAttribute("data-cite-src", String(i + 1));
    const clickable = !!(src.document_id && onSourceClick);
    if (clickable) {
      li.classList.add("ac-source-item--clickable");
      li.setAttribute("role", "button");
      li.setAttribute("tabindex", "0");
      const open = () => onSourceClick(src.document_id, src.page_number ?? null, src.snippet ?? null);
      li.addEventListener("click", open);
      li.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      });
    }
    const title = document.createElement("span");
    title.className = "ac-source-title";
    title.textContent = src.document_name || src.doc_title || `Source ${i + 1}`;
    li.appendChild(title);
    if (src.locator) {
      const loc = document.createElement("span");
      loc.className = "ac-source-locator";
      loc.textContent = src.locator;
      li.appendChild(loc);
    }
    if (src.snippet) {
      const snip = document.createElement("span");
      snip.className = "ac-source-snippet";
      snip.textContent = src.snippet;
      li.appendChild(snip);
    }
    ol.appendChild(li);
  });
  wrap.appendChild(ol);
  return wrap;
}

// src/ab.ts
var API = window.location.origin;
var LS_KEY = "ab:expandAll";
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls)
    e.className = cls;
  if (text != null)
    e.textContent = text;
  return e;
}
function dash(v) {
  if (v === null || v === void 0 || v === "")
    return "\u2014";
  return String(v);
}
function ms(v) {
  return v == null ? "\u2014" : `${(v / 1e3).toFixed(1)}s`;
}
function collapsible(summaryText, body, open) {
  const d = document.createElement("details");
  d.className = "ab-collapsible";
  d.open = open;
  const s = document.createElement("summary");
  s.className = "ab-collapsible-summary";
  s.textContent = summaryText;
  d.appendChild(s);
  const wrap = el("div", "ab-collapsible-body");
  wrap.appendChild(body);
  d.appendChild(wrap);
  return d;
}
function renderArmBox(arm, data, expandAll) {
  const box = el("section", "ab-box");
  box.setAttribute("aria-label", `arm ${arm.label}`);
  const head = el("header", "ab-box-head");
  head.appendChild(el("span", "ab-box-arm", arm.label));
  const st = el("span", "ab-box-status", data.status === "captured" ? "captured" : dash(data.status));
  st.classList.add(`ab-status--${data.status || "unknown"}`);
  head.appendChild(st);
  box.appendChild(head);
  const mismatched = data.decision_trace.filter((r) => r.prompt_mismatch);
  if (mismatched.length) {
    const directives = [...new Set(mismatched.map(
      (r) => (typeof r.prompt_mismatch === "string" ? r.prompt_mismatch : null) || r.applied_directive || r.posture || "a posture"
    ))].join(", ");
    box.appendChild(el(
      "div",
      "ab-mismatch",
      `\u26A0 v2 chose ${directives} \u2014 v1 has no prompt for it, so the person saw v1's answer, not this. Not a like-for-like comparison on those rounds.`
    ));
  }
  const env = data.answer_envelope;
  if (env && Array.isArray(env.blocks) && env.blocks.length) {
    const { answerBody, sources } = renderEnvelope(env.blocks, {
      renderExtraBlock: (b) => {
        if (b.type === "tool_attribution") {
          const chip = el("div", "envelope-tool-chip", b.label || "Research");
          chip.setAttribute("data-icon", b.icon || "search");
          return chip;
        }
        return null;
      }
    });
    const answer = el("div", "ab-answer");
    answer.appendChild(answerBody);
    if (sources && Array.isArray(sources.refs)) {
      const refs = sources.refs.map((r) => ({
        doc_title: r.title,
        page_number: r.page ?? null,
        snippet: r.snippet,
        document_id: r.document_id
      }));
      const sourcesEl = renderSourcesList(refs);
      if (sourcesEl)
        answer.appendChild(sourcesEl);
    }
    box.appendChild(answer);
  } else if (data.error) {
    box.appendChild(el("div", "ab-answer ab-answer--error", `Arm errored: ${data.error}`));
  } else if (data.decision_trace.length) {
    const note = el("div", "ab-trace-mode-note", "No answer emitted \u2014 showing the decision trace (shadow arm).");
    box.appendChild(note);
    box.appendChild(renderTrace(
      data.decision_trace,
      /*inline*/
      true
    ));
  } else {
    box.appendChild(el("div", "ab-answer ab-answer--empty", "\u2014 not captured \u2014"));
  }
  if (env && data.decision_trace.length) {
    box.appendChild(collapsible(
      `Round-by-round trace \xB7 ${data.decision_trace.length} rounds`,
      renderTrace(data.decision_trace, false),
      expandAll
    ));
  }
  return box;
}
function renderTrace(rows, inline) {
  const wrap = el("div", inline ? "ab-trace ab-trace--inline" : "ab-trace");
  for (const r of rows) {
    const row = el("div", "ab-trace-row");
    row.appendChild(el("span", "ab-trace-round", `R${r.round_n}`));
    const label = r.posture || r.directive || "\u2014";
    row.appendChild(el("span", "ab-trace-posture", label));
    if (r.tool_called)
      row.appendChild(el("span", "ab-trace-tool", r.tool_called));
    if (r.round_duration_s != null)
      row.appendChild(el("span", "ab-trace-dur", `${r.round_duration_s.toFixed(1)}s`));
    if (r.rationale)
      row.appendChild(el("span", "ab-trace-why", r.rationale));
    wrap.appendChild(row);
  }
  return wrap;
}
function findShadowArm(cmp) {
  for (const id of Object.keys(cmp.arms)) {
    if (cmp.arms[id].decision_trace.some((r) => r.verdict != null))
      return cmp.arms[id];
  }
  return null;
}
function renderDivergences(shadow, expandAll) {
  const rows = shadow.decision_trace.filter((r) => r.verdict && r.verdict !== "agree");
  if (!rows.length) {
    const nored = el("div", "ab-diverge-none", "No divergences \u2014 the arms agreed on every mapped round.");
    return collapsible("Divergences \xB7 0", nored, false);
  }
  const body = el("div", "ab-diverge");
  let diverge = 0, unmapped = 0;
  for (const r of rows) {
    const row = el("div", `ab-diverge-row ab-diverge--${r.verdict}`);
    row.appendChild(el("span", "ab-diverge-round", `R${r.round_n}`));
    if (r.verdict === "unmapped") {
      unmapped++;
      row.appendChild(el("span", "ab-diverge-tag", "mapping gap"));
      row.appendChild(el(
        "span",
        "ab-diverge-detail",
        `v1 "${dash(r.v1_directive)}" \u2014 not covered by the mapping`
      ));
    } else {
      diverge++;
      row.appendChild(el("span", "ab-diverge-tag ab-diverge-tag--real", "diverge"));
      row.appendChild(el(
        "span",
        "ab-diverge-detail",
        `v1 "${dash(r.v1_maps_to || r.v1_directive)}" vs v2 "${dash(r.posture || r.directive)}"` + (r.rationale ? ` \u2014 ${r.rationale}` : "")
      ));
    }
    body.appendChild(row);
  }
  const summary = `Divergences \xB7 ${diverge} diverge` + (unmapped ? ` \xB7 ${unmapped} mapping-gap (not a disagreement)` : "");
  return collapsible(summary, body, expandAll);
}
function renderTerms(cmp, arms, expandAll) {
  const table = el("table", "ab-terms-table");
  const head = el("tr");
  head.appendChild(el("th", "", ""));
  for (const a of arms)
    head.appendChild(el("th", "", a.label));
  table.appendChild(head);
  const rowDefs = [
    ["latency", (d) => ms(d.delivered.latency_ms)],
    ["promised", (d) => ms(d.promised?.latency_ms)],
    ["kept", (d) => d.kept == null ? "\u2014" : d.kept ? "\u2713" : "missed"],
    ["exit", (d) => dash(d.delivered.exit_mode)],
    ["rounds", (d) => dash(d.delivered.rounds)],
    // cost_cents is CENTS (every cost in the system is — promised_cost_c, delivered_cost_c,
    // Budget.remaining_c). Rendered in $ so the unit is on the value, never a bare number
    // that reads as dollars while holding cents. null → "—", never 0 (0 is a measured cost).
    ["cost", (d) => d.delivered.cost_cents == null ? "\u2014" : `$${(d.delivered.cost_cents / 100).toFixed(3)}`]
  ];
  for (const [label, fn] of rowDefs) {
    const tr = el("tr");
    tr.appendChild(el("td", "ab-terms-label", label));
    for (const a of arms)
      tr.appendChild(el("td", "ab-terms-val", fn(cmp.arms[a.id])));
    table.appendChild(tr);
  }
  const note = el(
    "div",
    "ab-terms-note",
    "Diagnostic only \u2014 not a judgement. 20 comparisons are zero data points for any exit criterion and twenty for human reading."
  );
  const body = el("div");
  body.appendChild(table);
  body.appendChild(note);
  return collapsible("Terms \xB7 latency \xB7 cost \xB7 exit \xB7 rounds \xB7 kept", body, expandAll);
}
function renderComparison(cmp, root) {
  root.textContent = "";
  const expandAll = readExpandAll();
  const arms = cmp.experiment.arms;
  const banner = el(
    "div",
    "ab-banner",
    "A/B HARNESS \u2014 every arm ran on identical input. Nobody was served. Production routes to exactly one orchestrator."
  );
  root.appendChild(banner);
  const header = el("header", "ab-exp-header");
  const held = el("div", "ab-exp-held");
  held.appendChild(el("span", "ab-exp-key", "Held"));
  held.appendChild(el("span", "ab-exp-val", cmp.experiment.held_constant.join(" \xB7 ") || "\u2014"));
  const varied = el("div", "ab-exp-varied");
  varied.appendChild(el("span", "ab-exp-key", "Varied"));
  varied.appendChild(el(
    "span",
    "ab-exp-val",
    cmp.experiment.varied.length ? cmp.experiment.varied.join(" \xB7 ") : "nothing \u2014 single-arm capture"
  ));
  header.appendChild(held);
  header.appendChild(varied);
  const toggle = el("button", "ab-expand-all", expandAll ? "Collapse all detail" : "Expand all detail");
  toggle.addEventListener("click", () => {
    writeExpandAll(!expandAll);
    renderComparison(cmp, root);
  });
  header.appendChild(toggle);
  root.appendChild(header);
  root.appendChild(el("h1", "ab-question", cmp.question.q));
  const meta = el("div", "ab-question-meta");
  meta.appendChild(el("span", "ab-chip", cmp.question.id));
  if (cmp.question.shape)
    meta.appendChild(el("span", "ab-chip", cmp.question.shape));
  if (cmp.question.mode)
    meta.appendChild(el("span", "ab-chip", cmp.question.mode));
  root.appendChild(meta);
  const grid = el("div", "ab-grid");
  grid.style.setProperty("--ab-cols", String(arms.length));
  for (const a of arms)
    grid.appendChild(renderArmBox(a, cmp.arms[a.id], expandAll));
  root.appendChild(grid);
  const shadow = arms.length > 1 ? findShadowArm(cmp) : null;
  if (shadow) {
    const div = renderDivergences(shadow, expandAll);
    if (div)
      root.appendChild(div);
  }
  root.appendChild(renderTerms(cmp, arms, expandAll));
}
function renderNav(run, current, onPick) {
  const nav = el("nav", "ab-nav");
  nav.appendChild(el("div", "ab-nav-run", run.run_id));
  const list = el("div", "ab-nav-list");
  for (const q of run.questions) {
    const b = el("button", "ab-nav-q" + (q.id === current ? " ab-nav-q--active" : ""), q.id);
    b.title = q.q;
    b.addEventListener("click", () => onPick(q.id));
    list.appendChild(b);
  }
  nav.appendChild(list);
  return nav;
}
function readExpandAll() {
  try {
    return localStorage.getItem(LS_KEY) === "1";
  } catch {
    return false;
  }
}
function writeExpandAll(v) {
  try {
    localStorage.setItem(LS_KEY, v ? "1" : "0");
  } catch {
  }
}
async function main() {
  const app = document.getElementById("ab-app");
  if (!app)
    return;
  const params = new URLSearchParams(location.search);
  const runId = params.get("run");
  let qid = params.get("q") || "";
  if (!runId) {
    app.appendChild(el(
      "div",
      "ab-empty",
      "No run selected. Open with ?run=<run_id> (e.g. ?run=ab-3fb1e4a6a3&q=q01)."
    ));
    return;
  }
  let run;
  try {
    const r = await fetch(`${API}/ab/runs/${encodeURIComponent(runId)}`);
    if (!r.ok)
      throw new Error(`run ${runId}: HTTP ${r.status}`);
    run = await r.json();
  } catch (e) {
    app.appendChild(el("div", "ab-empty", `Could not load run: ${e.message}`));
    return;
  }
  if (!qid && run.questions.length)
    qid = run.questions[0].id;
  const navHost = el("div", "ab-nav-host");
  const body = el("div", "ab-body");
  app.appendChild(navHost);
  app.appendChild(body);
  async function show(q) {
    qid = q;
    const url = new URL(location.href);
    url.searchParams.set("run", runId);
    url.searchParams.set("q", q);
    history.replaceState(null, "", url.toString());
    navHost.textContent = "";
    navHost.appendChild(renderNav(run, q, (nq) => {
      void show(nq);
    }));
    body.textContent = "";
    body.appendChild(el("div", "ab-loading", "Loading comparison\u2026"));
    try {
      const r = await fetch(`${API}/ab/runs/${encodeURIComponent(runId)}/q/${encodeURIComponent(q)}`);
      if (!r.ok)
        throw new Error(`HTTP ${r.status}`);
      const cmp = await r.json();
      renderComparison(cmp, body);
    } catch (e) {
      body.textContent = "";
      body.appendChild(el("div", "ab-empty", `Could not load ${q}: ${e.message}`));
    }
  }
  void show(qid);
}
void main();
export {
  renderComparison
};
