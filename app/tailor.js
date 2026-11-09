/* Picks and orders the candidate's existing resume content for one job. Never writes new claims: it only selects from
   profile/resume.json. Used by app/resume.html (preview/print) and by the autofill script (PDF to upload). */
(function(root){
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

  function trackKey(job){
    const t = `${job.track||""} ${job.title||""}`.toLowerCase();
    if (/front|engineer|developer|web dev/.test(t) && !/product design|ux design/.test(t)) return "frontend";
    if (/creative tech|experience|immersive|prototyp|museum|installation|motion|interactive/.test(t)) return "creative";
    if (/visual|graphic|brand|content design|marketing/.test(t)) return "visual";
    return "product";
  }
  function jobText(job){
    return [job.title, job.company, job.track, job.why, job.lead, job.note, ...Object.values(job.details||{})].join(" ").toLowerCase();
  }

  function plan(R, job, saved){
    const key = trackKey(job), text = jobText(job), reasons = [];
    const label = {product:"product / UI-UX design", frontend:"frontend / design engineering", creative:"creative technology / experience", visual:"visual / brand design"}[key];
    reasons.push(`Tailored for a ${label} role.`);

    // Projects: track weight + keyword overlap with the posting; one version per project group only.
    const scored = R.projects.map(p => {
      const hits = (p.keywords||[]).filter(k => new RegExp(`(^|[^a-z0-9])${k.replace(/[.*+?^${}()|[\]\\#]/g, "\\$&")}($|[^a-z0-9])`).test(text));
      return {p, hits, score: (p.tracks[key]||0) * 2 + hits.length * 1.5};
    }).sort((a,b) => b.score - a.score);
    let projects = [], groups = new Set();
    for (const s of scored){
      if (s.p.group && groups.has(s.p.group)) continue;
      if (projects.length >= 4) break;
      projects.push(s.p); if (s.p.group) groups.add(s.p.group);
      if (s.hits.length) reasons.push(`${s.p.name}: matches "${s.hits.slice(0,3).join('", "')}" in the posting.`);
    }

    // Experience: keep the strongest for this track, max 5, in the original (reverse-chronological) order.
    const expScore = e => (e.tracks[key]||0) + (e.bullets.join(" ").toLowerCase().split(/\W+/).filter(w => w.length > 4 && text.includes(w)).length > 2 ? 1 : 0);
    const keepIds = new Set(R.experience.map(e => ({e, s: expScore(e)})).sort((a,b)=>b.s-a.s).slice(0,5).filter(x=>x.s>0).map(x=>x.e.id));
    let experience = R.experience.filter(e => keepIds.has(e.id));

    if (saved){
      projects = R.projects.filter(p => saved.projects.includes(p.id));
      experience = R.experience.filter(e => saved.experience.includes(e.id));
      reasons.push("Using your saved picks for this job.");
    }

    // Skills: the track's list, with anything the posting names moved to the front.
    const skills = [...R.skills[key]].sort((a,b) => (text.includes(b.toLowerCase().split(" ")[0]) ? 1 : 0) - (text.includes(a.toLowerCase().split(" ")[0]) ? 1 : 0));
    const eduLines = R.education.lines.filter(l => l.tracks.includes("*") || l.tracks.includes(key));
    // Keep it to one page: trim long bullet lists when there are many entries.
    const maxBullets = experience.length >= 5 ? 3 : 4;
    reasons.push(`${experience.length} experiences, ${projects.length} projects. Skills ordered for ${label}.`);
    return {key, projects, experience, skills, eduLines, maxBullets, reasons};
  }

  // An editable resume document for one job: the plan flattened into plain data. The AI chat and manual edits change
  // this copy only; profile/resume.json stays the source of truth.
  function docFromPlan(R, P){
    return {
      education: {school: R.education.school, where: R.education.where, dates: R.education.dates, lines: P.eduLines.map(l => l.text)},
      experience: P.experience.map(e => ({title: e.title, where: e.where, dates: e.dates, bullets: e.bullets.slice(0, P.maxBullets)})),
      projects: P.projects.map(p => ({name: p.name, tools: p.tools, year: p.year, url: p.url, text: p.text})),
      skills: P.skills, languages: R.languages
    };
  }
  function header(R){
    return `<h1>${esc(R.name.toUpperCase())}</h1>
      <div class="contact">⌖ ${esc(R.location)} &nbsp;&nbsp;&nbsp; ✉ <a href="mailto:${esc(R.email)}">${esc(R.email)}</a></div>
      <div class="links">${R.links.map(l => `<a href="${esc(l.url)}">${esc(l.label)}</a>`).join("")}</div>`;
  }
  function renderDoc(R, D){
    const e = D.education;
    return `${header(R)}
      <h2>EDUCATION</h2>
      <div class="entry"><div class="t">${esc(e.school)}</div><div class="d">${esc(e.where)}<br>${esc(e.dates)}</div>
        <ul>${e.lines.map(l => `<li>${esc(l)}</li>`).join("")}</ul></div>
      <h2>EXPERIENCE</h2>
      ${D.experience.map(x => `<div class="entry"><div class="t">${esc(x.title)}</div><div class="d">${esc(x.where)}${x.where?"<br>":""}${esc(x.dates)}</div>
        <ul>${x.bullets.map(b => `<li>${esc(b)}</li>`).join("")}</ul></div>`).join("")}
      <h2>RELATED PROJECTS</h2>
      ${D.projects.map(p => `<div class="proj"><div class="t">${p.url ? `<a href="${esc(p.url)}">${esc(p.name)}</a>` : esc(p.name)} | <i>${esc(p.tools)}</i> | ${esc(p.year)}</div><p>${esc(p.text)}</p></div>`).join("")}
      <h2>SKILL</h2>
      <div class="skills">${esc((D.skills||[]).join(", "))}. ${esc(D.languages||"")}</div>`;
  }
  function renderLetter(R, text){
    const paras = String(text||"").trim().split(/\n\s*\n/).map(p => `<p>${esc(p).replace(/\n/g,"<br>").replace(/(https?:\/\/[^\s<]+)/g, m => `<a href="${m.startsWith("http")?m:"https://"+m}">${m}</a>`)}</p>`).join("");
    return `${header(R)}<hr class="rule">${paras || "<p class='empty'>No cover letter yet. Ask the AI to write one.</p>"}`;
  }

  function render(R, P){
    const e = R.education;
    return `
      <h1>${esc(R.name.toUpperCase())}</h1>
      <div class="contact">⌖ ${esc(R.location)} &nbsp;&nbsp;&nbsp; ✉ <a href="mailto:${esc(R.email)}">${esc(R.email)}</a></div>
      <div class="links">${R.links.map(l => `<a href="${esc(l.url)}">${esc(l.label)}</a>`).join("")}</div>
      <h2>EDUCATION</h2>
      <div class="entry"><div class="t">${esc(e.school)}</div><div class="d">${esc(e.where)}<br>${esc(e.dates)}</div>
        <ul>${P.eduLines.map(l => `<li>${esc(l.text)}</li>`).join("")}</ul></div>
      <h2>EXPERIENCE</h2>
      ${P.experience.map(x => `<div class="entry"><div class="t">${esc(x.title)}</div><div class="d">${esc(x.where)}${x.where?"<br>":""}${esc(x.dates)}</div>
        <ul>${x.bullets.slice(0, P.maxBullets).map(b => `<li>${esc(b)}</li>`).join("")}</ul></div>`).join("")}
      <h2>RELATED PROJECTS</h2>
      ${P.projects.map(p => `<div class="proj"><div class="t"><a href="${esc(p.url)}">${esc(p.name)}</a> | <i>${esc(p.tools)}</i> | ${esc(p.year)}</div><p>${esc(p.text)}</p></div>`).join("")}
      <h2>SKILL</h2>
      <div class="skills">${esc(P.skills.join(", "))}. ${esc(R.languages)}</div>`;
  }

  root.Tailor = {plan, render, trackKey, docFromPlan, renderDoc, renderLetter, esc};
})(typeof window !== "undefined" ? window : globalThis);
