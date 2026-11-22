'use strict';
/* Claim Check single-page app. Every number comes from the JSON API (claimcheck/app/server.py). */

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const money = (n) => '$' + Number(n || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money0 = (n) => '$' + Math.round(Number(n || 0)).toLocaleString('en-US');
const int = (n) => Number(n || 0).toLocaleString('en-US');
const pct = (x) => (x * 100).toFixed(1) + '%';
const PAYERS = { PY01: 'Northwind', PY02: 'Harbor', PY03: 'Summit' };
const KIND = { evv_required: 'Verified EVV visit required', units_within_verified_time: 'Units within verified time',
  auth_required: 'Authorization number required', auth_matches: 'Authorization fits member, service and date',
  auth_balance: 'Units within authorization balance', timely_filing: 'Filed within the limit',
  modifier_required: 'Modifier required', daily_unit_max: 'Daily unit maximum', no_duplicates: 'No duplicate claims',
  whole_hours: 'Units in whole hours' };
const FIXLABEL = { fixable: 'Fix', needs_decision: 'Decide', not_fixable: 'Can’t fix', payable_as_is: 'Ready' };
const CHANGEKIND = { correction: 'Correction', required_reduction: 'Required reduction', business_choice: 'Your decision' };
const RECIPIENT = { care_coordinator: 'Care coordinator', payer_care_manager: 'Payer care manager', billing_lead: 'Billing lead' };

async function api(path, opts) {
  const r = await fetch('/api/' + path, opts);
  let j = null;
  try { j = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new Error((j && j.error) || r.statusText);
  return j;
}
const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

/* ------------------------------------------------------------------ routing */
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [path, query] = h.split('?');
  const parts = (path || 'claims').split('/');
  const params = Object.fromEntries(new URLSearchParams(query || ''));
  return { route: parts[0] || 'claims', arg: parts.slice(1).join('/') || null, params };
}
function hashFor(route, arg, params) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null)).toString();
  return '#/' + route + (arg ? '/' + arg : '') + (q ? '?' + q : '');
}
function setParams(upd) {
  const cur = parseHash();
  const next = { ...cur.params, ...upd };
  if (!('page' in upd)) delete next.page;
  location.hash = hashFor(cur.route, cur.arg, next);
}
let overview = null;
let renderSeq = 0;
let lastRoute = null;
const VIEWS = {};

async function render() {
  const seq = ++renderSeq;
  const { route, arg, params } = parseHash();
  document.querySelectorAll('.nav').forEach((a) => {
    const on = a.dataset.route === route || (route === 'claim' && a.dataset.route === 'claims') ||
      (route === 'add-payer' && a.dataset.route === 'rules');
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  const view = VIEWS[route] || VIEWS.claims;
  try {
    if (!overview) overview = await api('overview');
    const html = await view(arg, params);
    if (seq !== renderSeq) return;
    $('#view').innerHTML = html;
    sideFoot();
    if (route !== lastRoute) { window.scrollTo(0, 0); $('#view').focus({ preventScroll: true }); }
    lastRoute = route;
    if (view.after) view.after(arg, params);
  } catch (e) {
    if (seq !== renderSeq) return;
    $('#view').innerHTML = `<div class="page"><div class="err">${esc(e.message)}</div></div>`;
  }
}
function sideFoot() {
  if (!overview) return;
  $('#side-foot').innerHTML = `Rule set <span class="mono">${esc(overview.rule_set_version)}</span><br>` +
    `${overview.agencies} agencies · ${overview.payers.length} payers<br>Drafts dated ${esc(overview.as_of)}`;
}
async function refreshOverview() { overview = await api('overview'); }

/* --------------------------------------------------------------- components */
const kpi = (l, v, s, tone = '') => `<div class="kpi ${tone}"><div class="l">${esc(l)}</div><div class="v">${v}</div><div class="s">${esc(s)}</div></div>`;
const pill = (text, tone) => `<span class="pill ${tone}">${esc(text)}</span>`;
function tabs(list, active, key) {
  return `<div class="tabs" role="tablist">${list.map(([v, label, count]) =>
    `<button type="button" class="tab" role="tab" aria-selected="${v === active}" data-set="${key}=${v}">${esc(label)}${count != null ? `<span class="c">${int(count)}</span>` : ''}</button>`).join('')}</div>`;
}
function chips(list, active, key) {
  return list.map(([v, label, count]) =>
    `<button type="button" class="chip" aria-pressed="${v === active}" data-set="${key}=${v}">${esc(label)}${count != null ? ' · ' + int(count) : ''}</button>`).join('');
}
function pager(d, note = '') {
  return `<div class="pager"><span>${note ? esc(note) + ' · ' : ''}${int(d.total)} result${d.total === 1 ? '' : 's'} · page ${d.page} of ${d.pages}</span>
    <span class="row"><button type="button" class="btn small" ${d.page <= 1 ? 'disabled' : ''} data-set="page=${d.page - 1}">Previous</button>
    <button type="button" class="btn small" ${d.page >= d.pages ? 'disabled' : ''} data-set="page=${d.page + 1}">Next</button></span></div>`;
}
const search = (value, placeholder) => `<form class="grow" data-search><label class="sr" for="q">Search</label>
  <input id="q" type="search" name="q" value="${esc(value || '')}" placeholder="${esc(placeholder)}" style="width:320px"></form>`;
const claimLink = (id) => `<a href="#/claim/${esc(id)}">${esc(id)}</a>`;
const statusPill = (status, code) => status === 'paid' ? pill('Paid', 'paid') : pill('Denied · ' + code, 'denied');
const fixTag = (outcome) => outcome ? `<span class="tag ${esc(outcome)}">${esc(FIXLABEL[outcome] || outcome)}</span>` : '';
function bar(paid, pred, total, lg) {
  const scale = Math.max(total, paid + pred, 1);
  return `<div class="bar ${lg ? 'lg' : ''}" role="img" aria-label="${paid} used by paid claims, ${pred} by predicted claims, of ${total}">
    <div class="p" style="width:${(100 * paid / scale).toFixed(1)}%"></div><div class="q" style="width:${(100 * pred / scale).toFixed(1)}%"></div></div>`;
}
const legend = `<div class="legend"><span><i style="background:var(--blue)"></i>used by paid claims</span><span><i style="background:var(--blue-2)"></i>used by claims predicted paid</span></div>`;
function params2(rule) {
  const p = rule.params || {};
  const parts = Object.entries(p).map(([k, v]) => `${k.replace(/_/g, ' ')}: ${Array.isArray(v) ? v.join(', ') : String(v).replace(/_/g, ' ')}`);
  return parts.join(' · ');
}
function effective(rule) {
  const e = rule.effective || {};
  if (e.from && e.to) return `${e.from} to ${e.to}`;
  if (e.from) return `From ${e.from}`;
  if (e.to) return `Up to ${e.to}`;
  return 'All dates';
}

/* ------------------------------------------------------------------- Claims */
VIEWS.claims = async (_, p) => {
  const tab = p.tab || 'awaiting', chip = p.chip || 'all';
  const d = await api(`claims?tab=${tab}&chip=${chip}&payer=${p.payer || ''}&q=${encodeURIComponent(p.q || '')}&page=${p.page || 1}`);
  const o = overview, t = o.to_check, fo = t.fix_outcomes;
  let table;
  if (tab === 'awaiting') {
    table = `<table class="data" style="min-width:1060px"><thead><tr><th>Claim</th><th>Payer</th><th>Service · date of care</th><th class="right">Billed</th><th>Prediction</th><th style="width:36%">Why · how to fix</th><th>Review</th></tr></thead><tbody>
      ${d.rows.map((r) => `<tr><td class="nowrap"><strong>${claimLink(r.id)}</strong><div class="sub">${esc(r.member)}</div></td>
        <td>${esc(r.payer)}</td><td class="nowrap">${esc(r.service)} · ${esc(r.dos)}<div class="sub">${r.units} units · submit ${esc(r.submitted)}</div></td>
        <td class="right num">${money(r.billed)}</td><td>${statusPill(r.status, r.code)}</td>
        <td><div>${esc(r.reason)}</div>${r.fix ? `<div class="row" style="margin-top:4px">${fixTag(r.fix_outcome)}<span class="muted">${esc(r.fix)}</span></div>` : ''}</td>
        <td>${r.flags.map((f) => pill(f.label, 'neutral')).join(' ')}</td></tr>`).join('')}</tbody></table>`;
  } else if (tab === 'denied') {
    table = `<table class="data" style="min-width:1060px"><thead><tr><th>Claim</th><th>Payer · date of care</th><th class="right">Billed</th><th style="width:22%">Payer said</th><th style="width:24%">Our rules found</th><th style="width:28%">How to fix</th></tr></thead><tbody>
      ${d.rows.map((r) => `<tr><td class="nowrap"><strong>${claimLink(r.id)}</strong><div class="sub">${esc(r.service)} · ${r.units} units</div></td>
        <td class="nowrap">${esc(r.payer)}<div class="sub">${esc(r.dos)}</div></td><td class="right num">${money(r.billed)}</td>
        <td>${pill(r.code, 'denied')}<div class="sub">${esc(r.remark)}</div></td>
        <td>${esc(r.engine)}${r.predicted !== r.code ? `<div class="sub">Predicted ${esc(r.predicted)}</div>` : ''}</td>
        <td>${fixTag(r.fix_outcome)}<div class="muted">${esc(r.fix)}</div></td></tr>`).join('')}</tbody></table>`;
  } else {
    table = `<table class="data" style="min-width:760px"><thead><tr><th>Claim</th><th>Payer</th><th>Service</th><th>Date of care</th><th>Submitted</th><th class="right">Units</th><th class="right">Paid</th></tr></thead><tbody>
      ${d.rows.map((r) => `<tr><td><strong>${claimLink(r.id)}</strong></td><td>${esc(r.payer)}</td><td>${esc(r.service)}</td><td class="num">${esc(r.dos)}</td><td class="num">${esc(r.submitted)}</td><td class="right num">${r.units}</td><td class="right num" style="color:var(--blue);font-weight:600">${money(r.billed)}</td></tr>`).join('')}</tbody></table>`;
  }
  const payerSel = `<label class="sr" for="payer">Payer</label><select id="payer" data-select="payer"><option value="">All payers</option>${o.payers.map((x) => `<option value="${x.id}" ${p.payer === x.id ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select>`;
  return `<div class="page">
    <div class="head"><div><h1>Claims</h1><p>Check every claim before it goes to the payer. Predictions use approved rules only and give the same answer every run.</p></div></div>
    <div class="kpis">
      ${kpi('To submit', int(t.claims), money0(t.billed) + ' billed')}
      ${kpi('Predicted paid as submitted', money0(t.paid_as_is), int(t.paid_claims) + ' claims', 'blue')}
      ${kpi('Recovered by safe fixes', '+' + money0(t.safe_fixes), int(fo.fixable) + ' claims, no decision needed')}
      ${kpi('Waiting on your decision', '+' + money0(t.decision_fixes), int(fo.needs_decision) + ' claims', 'orange')}
      ${kpi('Backtest on ' + int(o.history.claims) + ' past claims', pct(o.backtest.exact_code_accuracy), pct(o.backtest.exact_code_accuracy_excluding_opaque_d99) + ' excluding D99 with no reason')}
    </div>
    <section class="card flush" aria-label="Claim list">
      ${tabs([['awaiting', 'Awaiting review', d.counts.awaiting], ['denied', 'Denied', d.counts.denied], ['paid', 'Paid', d.counts.paid]], tab, 'tab')}
      <div class="toolbar">${tab === 'awaiting' ? chips([['all', 'All', t.claims], ['denied', 'Will be denied', t.denied_claims], ['decision', 'Needs a decision', fo.needs_decision], ['flagged', 'Flagged for review', t.needs_review], ['paid', 'Will be paid', t.paid_claims]], chip, 'chip') : `<span class="muted small">${tab === 'denied' ? 'Past claims the payer denied: the payer’s code and remark, what our rules find, and what would have fixed it.' : 'Past claims the payer paid.'}</span>`}</div>
      <div class="toolbar">${search(p.q, 'Search claim, member, authorization or code')}${payerSel}</div>
      <div class="scroll">${table}</div>${pager(d)}
    </section></div>`;
};

/* -------------------------------------------------------------- Claim detail */
VIEWS.claim = async (id) => {
  const c = await api('claims/' + encodeURIComponent(id));
  const d = c.decision, cl = c.claim, ev = d.evidence, led = c.ledger, fix = d.fix;
  const failed = d.results.filter((r) => !r.passed);
  const head = `<div class="crumb"><a href="#/claims${c.history ? '?tab=' + (cl.status === 'paid' ? 'paid' : 'denied') : ''}">Claims</a> / ${esc(cl.claim_id)}</div>
    <div class="head"><div><div class="row"><h1 class="num">${esc(cl.claim_id)}</h1>
      ${c.history ? statusPill(cl.status, cl.denial_code).replace('Paid', 'Paid by payer').replace('Denied ·', 'Denied by payer ·') : statusPill(d.status, d.denial_code).replace('Paid', 'Predicted paid').replace('Denied ·', 'Predicted denied ·')}
      ${fix && fix.outcome !== 'payable_as_is' ? pill(FIXLABEL[fix.outcome] === 'Decide' ? 'Needs your decision' : (fix.outcome === 'fixable' ? 'Fixable' : 'Can’t fix by editing'), 'neutral') : ''}
      ${cl.frequency_code === 7 ? pill('Corrected claim for ' + cl.original_claim_id, 'neutral') : ''}</div>
      <p style="color:var(--ink-2)">${esc(c.payer_name)} · member ${esc(cl.patient_id)} · ${esc(cl.service_code)} · care on ${esc(ev.day_of_week)} ${esc(cl.date_of_service)} · ${cl.units} units · ${money(cl.billed_amount)} · ${c.history ? 'submitted' : 'to be submitted'} ${esc(cl.submitted_date)} (${ev.days_to_submit} days after care)${cl.modifier ? ' · modifier ' + esc(cl.modifier) : ''}${cl.auth_id ? ' · ' + esc(cl.auth_id) : ' · no authorization number'}</p></div></div>`;
  const actual = c.history ? `<section class="card ${cl.status === 'denied' ? 'warn' : ''}"><h2>What the payer decided</h2>
      <p style="margin-top:8px">${cl.status === 'paid' ? 'Paid in full.' : `Denied <strong>${esc(cl.denial_code)}</strong>: ${esc(cl.remark)}`}</p>
      <p class="sub" style="margin-top:6px">Our rules ${((d.denial_code || 'paid') === (cl.denial_code || 'paid')) ? 'predict the same outcome.' : `predict <strong>${esc(d.denial_code || 'paid')}</strong>, which differs.${cl.denial_code === 'D99' ? ' The payer gave no usable reason for this D99.' : ''}`}</p></section>` : '';
  const why = `<section class="card"><h2>${d.status === 'paid' ? 'Why it passes' : 'Why it would be denied'}</h2>
    <p style="margin-top:10px;font-size:15px">${esc(failed.length ? failed[0].detail : 'Every rule in force passes.')}</p>
    ${failed.slice(1).map((r) => `<p class="sub">Also fails ${esc(r.denial_code)}: ${esc(r.detail)}</p>`).join('')}
    <h3 style="margin-top:18px">Every rule checked (${d.results.length})</h3><ul class="rules">
    ${d.results.map((r) => { const k = !r.applicable ? 'na' : r.passed ? 'pass' : 'fail';
      return `<li><span class="mark ${k}" aria-label="${k === 'pass' ? 'Passed' : k === 'fail' ? 'Failed' : 'Not applicable'}">${k === 'pass' ? '✓' : k === 'fail' ? '✕' : '–'}</span>
        <div style="min-width:0"><div class="row"><span class="mono small">${esc(r.rule_id)}</span><span class="small muted">${esc(r.denial_code)}</span></div>
        <div>${esc(r.detail)}</div><div class="sub">Source: ${esc(r.source_ref)}</div></div></li>`; }).join('')}</ul></section>`;
  const used = led.units_authorized != null ? led.units_authorized - led.remaining_before : 0;
  const predUsed = led.draws.filter((x) => x.source === 'predicted').reduce((a, x) => a + x.units, 0);
  const evidence = `<section class="card stack"><h2>Evidence</h2>
    <div><h3>EVV visits on ${esc(cl.date_of_service)}</h3>${c.visits.length ? `<table class="mini"><tbody>${c.visits.map((v) => `<tr><td><a href="#/evv?q=${esc(v.id)}">${esc(v.id)}</a></td><td>${esc(v.clock_in)} – ${esc(v.clock_out.slice(11))}</td><td>${v.minutes} min</td><td>${v.status === 'verified' ? pill('Verified', 'paid') : pill(v.status.replace(/_/g, ' '), 'denied')}</td></tr>`).join('')}</tbody></table>
      <p class="sub">${ev.verified_minutes} verified minutes in total.</p>` : '<p class="muted">No EVV visit recorded for this member, service and date.</p>'}</div>
    ${led.found ? `<div><h3>Authorization ${esc(led.auth_id)} · ${c.auth ? esc(c.auth.start) + ' to ' + esc(c.auth.end) : ''} · ${led.units_authorized} units</h3>
      ${bar(used - predUsed, predUsed, led.units_authorized, true)}${legend}
      <table class="mini" style="margin-top:12px"><thead><tr><th>Submitted</th><th>Claim</th><th>Outcome</th><th class="right">Units</th></tr></thead><tbody>
      ${led.draws.map((x) => `<tr><td>${esc(x.submitted)}</td><td>${claimLink(x.claim_id)}</td><td>${x.source === 'predicted' ? '<span style="color:var(--blue)">Predicted paid</span>' : 'Paid'}</td><td class="right">${x.units}</td></tr>`).join('')}
      <tr class="${led.remaining_before < cl.units ? 'hot' : ''}"><td>${esc(cl.submitted_date)}</td><td>${esc(cl.claim_id)} (this claim)</td><td>${led.remaining_before} left before it</td><td class="right">${cl.units}</td></tr></tbody></table>
      <p class="sub"><a href="#/auth?open=${esc(led.auth_id)}&q=${esc(led.auth_id)}">Open the authorization ledger</a></p></div>`
      : `<div><h3>Authorization</h3><p class="muted">${cl.auth_id ? esc(cl.auth_id) + ' is not on file.' : 'The claim quotes no authorization.'}</p></div>`}
    ${led.prior_submissions.length ? `<div><h3>Earlier submissions for this member, service and date</h3><p>${led.prior_submissions.map(claimLink).join(', ')}</p></div>` : ''}
    </section>`;
  const fixCard = fix && fix.outcome !== 'payable_as_is' ? `<section class="card"><h2>${fix.outcome === 'needs_decision' ? 'Your decision' : fix.outcome === 'fixable' ? 'How to fix it' : 'Why it can’t be fixed by editing'}</h2>
      ${fix.blockers.map((b) => `<p style="margin-top:10px">${esc(b)}</p>`).join('')}
      ${fix.changes.length ? `<table class="mini" style="margin-top:12px"><tbody>${fix.changes.map((ch) => `<tr><td><strong>${esc(ch.field.replace(/_/g, ' '))}</strong> ${esc(ch.old === '' || ch.old == null ? 'blank' : ch.old)} → ${esc(ch.new)}<div class="sub">${esc(ch.reason)}</div></td><td class="right">${pill(CHANGEKIND[ch.kind] || ch.kind, ch.kind === 'business_choice' ? 'denied' : 'neutral')}</td></tr>`).join('')}</tbody></table>` : ''}
      ${fix.billed_after !== fix.billed_before && fix.outcome !== 'not_fixable' ? `<p class="sub" style="margin-top:8px">Billed ${money(fix.billed_before)} → ${money(fix.billed_after)}</p>` : ''}
      ${fix.outcome !== 'not_fixable' ? `<p class="sub" style="margin-top:8px">✓ Re-checked after ${fix.rounds} round${fix.rounds === 1 ? '' : 's'}: the edited claim passes every rule.</p>` : ''}</section>` : '';
  const flags = d.flags.length ? `<section class="card note"><h2>Review flags</h2>${d.flags.map((f) => `<div style="margin-top:10px"><div class="row">${pill(f.code.replace(/_/g, ' ').toLowerCase(), 'neutral')}<span class="small muted">${f.effect === 'status_may_change' ? 'Paid vs denied may change' : 'The code may change'}</span></div><p style="margin-top:4px">${esc(f.reason)}</p></div>`).join('')}</section>` : '';
  let msg = '';
  if (c.message) {
    const m = c.message;
    msg = `<section class="card"><div class="row between"><h2>Drafted message</h2>${pill(m.status === 'draft' ? 'Draft · not sent' : 'Check references', m.status === 'draft' ? 'paid' : 'denied')}</div>
      <dl class="kv"><dt>To</dt><dd>${esc(RECIPIENT[m.recipient] || m.recipient)}</dd><dt>Subject</dt><dd><strong>${esc(m.subject)}</strong></dd></dl>
      <div class="msg">${esc(m.body)}</div>
      <p class="sub" style="margin-top:8px">Written by ${esc(m.model || 'Claude')} from this claim's facts only. ${m.unknown_references && m.unknown_references.length ? 'Mentions IDs or dates not in the facts: ' + esc(m.unknown_references.join(', ')) : 'Every ID and date was checked against those facts.'}</p>
      <div class="row" style="margin-top:12px"><button type="button" class="btn small" data-action="copy" data-text="${esc(m.subject + '\n\n' + m.body)}">Copy</button></div></section>`;
  } else if (c.message_route) {
    msg = `<section class="card"><h2>Message</h2><p class="muted" style="margin-top:8px">For the ${esc((RECIPIENT[c.message_route.recipient] || '').toLowerCase())}: ${esc(c.message_route.purpose)}</p>
      <button type="button" class="btn primary" style="margin-top:12px" data-action="draft-message" data-id="${esc(cl.claim_id)}">Draft message with Claude</button></section>`;
  }
  const audit = `<section class="card small" style="color:var(--ink-2)"><h2 style="font-size:14px;margin-bottom:6px">Audit trail</h2>
    Decided by rule set <span class="mono">${esc(d.rule_set_version)}</span>. No language model is involved in this decision; the same inputs always give the same result.<br>
    ${c.in_stream_position ? `Judged at position ${int(c.in_stream_position)} of ${int(led.stream_length)} in the submission-order stream.` : ''} Reported rule: <span class="mono">${esc(d.reported_rule || 'none')}</span>.</section>`;
  return `<div class="page">${head}<div class="cols"><div class="main">${actual}${why}${evidence}${audit}</div><div class="aside">${fixCard}${flags}${msg}</div></div></div>`;
};

/* --------------------------------------------------------------------- EVV */
VIEWS.evv = async (_, p) => {
  const tab = p.tab || 'all', outcome = p.outcome || 'ready';
  const [d, a] = await Promise.all([api(`visits?tab=${tab}&q=${encodeURIComponent(p.q || '')}&page=${p.page || 1}`),
    api(`automator?outcome=${outcome}&page=${p.dpage || 1}`)]);
  const c = d.counts, s = a.summary;
  const S = (k) => s[k] || { drafts: 0, amount: 0 };
  const sel = a.rows.find((r) => r.key === p.draft) || a.rows[0];
  const stateLabel = { paid: ['Done', 'ready'], denied: ['Note', 'not_fixable'], waiting: ['Claim waiting', 'fixable'], fix: ['Fix EVV', 'late'], unbilled: ['Not billed', 'late'] };
  const rows = d.rows.map((v) => `<tr><td class="nowrap"><strong class="num">${esc(v.id)}</strong><div class="sub">caregiver ${esc(v.caregiver)}</div></td>
    <td class="nowrap">${esc(v.member)}<div class="sub">${esc(v.service)}</div></td>
    <td class="nowrap num">${esc(v.clock_in)} – ${esc(v.crosses_midnight ? v.clock_out : v.clock_out.slice(11))}<div class="sub">${v.minutes} min${v.crosses_midnight ? ' · crosses midnight, counts for ' + esc(v.date) : ''}</div></td>
    <td>${v.status === 'verified' ? pill('Verified', 'paid') : v.status === 'rejected' ? pill('Rejected', 'solid') : pill('Pending approval', 'denied')}</td>
    <td>${v.claims.length ? v.claims.map((x) => `<div class="nowrap">${claimLink(x.id)} ${pill(x.label, x.kind === 'paid' ? 'paid' : x.kind === 'denied' ? 'denied' : 'neutral')}</div>`).join('') : '<span class="muted">None yet</span>'}</td>
    <td><span class="tag ${stateLabel[v.state][1]}">${stateLabel[v.state][0]}</span><div class="muted">${esc(v.action)}</div></td></tr>`).join('');
  const draftRows = a.rows.map((r) => `<tr class="click ${sel && r.key === sel.key ? 'sel' : ''}" data-set="draft=${esc(r.key)}"><td><button type="button" class="btn small" data-set="draft=${esc(r.key)}">${esc(r.visits.join(', '))}</button><div class="sub">${esc(r.member)} · ${esc(r.service)} · ${esc(r.dos)}</div></td>
    <td>${esc(r.payer)}<div class="sub">${esc(r.auth_id || '')}</div></td><td>${r.minutes} min → ${r.units} units<div class="sub">${esc(r.units_note)}</div></td>
    <td class="right num">${money(r.amount)}</td><td class="right nowrap">${r.file_by ? esc(r.file_by) + `<div class="sub">${r.days_left < 0 ? Math.abs(r.days_left) + ' days late' : r.days_left === 0 ? 'today' : r.days_left + ' days left'}</div>` : '<span class="muted">no limit</span>'}</td></tr>`).join('');
  const draftCard = sel ? `<div class="box" style="background:#F9FAFB;flex:2 1 340px;min-width:0;padding:18px 20px">
      <div class="row between"><h2 style="font-size:15px">Draft claim from ${esc(sel.visits.join(', '))}</h2>${sel.outcome === 'ready' ? pill('Predicted paid', 'paid') : pill(sel.code ? 'Would be ' + sel.code : sel.outcome, 'denied')}</div>
      <dl class="kv"><dt>Payer</dt><dd>${esc(sel.payer_name)}</dd><dt>Member · service</dt><dd>${esc(sel.member)} · ${esc(sel.service)}</dd><dt>Date of care</dt><dd>${esc(sel.dos)}</dd>
      <dt>Authorization</dt><dd>${esc(sel.auth_id)} (covers this member, service and date)</dd><dt>Units</dt><dd>${esc(sel.units_note)}</dd><dt>Modifier</dt><dd>${esc(sel.modifier)}</dd>
      <dt>Amount</dt><dd><strong>${money(sel.amount)}</strong></dd><dt>File by</dt><dd>${sel.file_by ? esc(sel.file_by) : 'No filing limit'}</dd></dl>
      <p class="sub" style="margin-top:10px">${sel.outcome === 'ready' ? `✓ Checked like any claim, dated ${esc(a.as_of)}: passes all ${sel.rules_checked} rules in force.` : esc(sel.reason)}</p>
      <div class="row" style="margin-top:14px"><button type="button" class="btn primary" ${sel.outcome !== 'ready' ? 'disabled' : ''} data-action="note" data-text="Creating claims from drafts is the next step to wire to the billing system; nothing is submitted from this demo.">Create claim</button></div></div>` : '';
  return `<div class="page">
    <div class="head"><div><h1>EVV visits</h1><p>Every clock-in and clock-out, the claim it belongs to, and what still needs doing. A visit counts for the day the caregiver clocked in; same-day visits for one member and service are added together.</p></div></div>
    <div class="kpis">${kpi('Visits recorded', int(c.all), int(d.status.verified || 0) + ' verified')}
      ${kpi('EVV needs fixing', int(c.fix || 0), int(d.status.rejected || 0) + ' rejected · ' + int(d.status.manual_edit_pending || 0) + ' pending approval', 'orange')}
      ${kpi('In claims to submit', int(c.waiting || 0), 'linked to claims awaiting review')}
      ${kpi('Verified, not on any claim', int(c.unbilled || 0), int(Object.values(s).reduce((x, y) => x + y.drafts, 0)) + ' claims to draft')}
      ${kpi('Billable if filed today', money0(S('ready').amount), money0(S('late').amount) + ' already past filing limits', 'blue')}</div>
    <section class="card flush">${tabs([['all', 'All', c.all], ['paid', 'Paid', c.paid], ['denied', 'Denied', c.denied], ['waiting', 'In claims to submit', c.waiting], ['fix', 'EVV needs fixing', c.fix], ['unbilled', 'Not on any claim', c.unbilled]], tab, 'tab')}
      <div class="toolbar">${search(p.q, 'Search visit, member, caregiver or claim')}</div>
      <div class="scroll"><table class="data" style="min-width:1060px"><thead><tr><th>Visit</th><th>Member · service</th><th>Clock in – out</th><th>EVV</th><th>Claims</th><th style="width:30%">What to do</th></tr></thead><tbody>${rows}</tbody></table></div>${pager(d)}</section>
    <section class="card stack" style="gap:18px"><div class="head"><div><h2 style="font-size:18px">Automator: claims drafted from unbilled visits</h2>
      <p>For each verified visit on no claim, the payer and authorization come from the authorization on file, units from the payer's rounding, the modifier from its rules. Each draft goes through the same checker as a manual claim, dated ${esc(a.as_of)}. Nothing is submitted without you.</p></div></div>
      <div class="kpis" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
        ${[['ready', 'Ready, predicted paid', 'blue'], ['late', 'Past the filing limit (D03)', 'orange'], ['auth_short', 'Authorization too small (D02)', ''], ['other_denial', 'Other denial', '']].map(([k, l, tone]) =>
          `<button type="button" class="box ${tone}" aria-pressed="${outcome === k}" data-set="outcome=${k}"><div class="small" style="font-weight:600">${esc(l)}</div><div class="big">${int(S(k).drafts)} · ${money0(S(k).amount)}</div></button>`).join('')}</div>
      <div class="cols"><div class="main" style="flex:3 1 520px"><div class="scroll"><table class="data" style="min-width:620px"><thead><tr><th>Visit</th><th>Payer · authorization</th><th>Time → units</th><th class="right">Amount</th><th class="right">File by</th></tr></thead><tbody>${draftRows || '<tr><td colspan="5" class="muted">No drafts in this group.</td></tr>'}</tbody></table></div>
        ${pager({ ...a, page: a.page }, 'Sorted by filing deadline').replace(/data-set="page=/g, 'data-set="dpage=')}</div>${draftCard}</div></section></div>`;
};

/* ---------------------------------------------------------- Authorizations */
VIEWS.auth = async (_, p) => {
  const f = p.filter || 'current';
  const [d, errors] = await Promise.all([api(`authorizations?filter=${f}&q=${encodeURIComponent(p.q || '')}&page=${p.page || 1}`), api('errors')]);
  const open = p.open ? await api('authorizations/' + encodeURIComponent(p.open)).catch(() => null) : null;
  const errOpen = localStorage.getItem('errorsOpen') !== 'no';
  const c = d.counts;
  const errHtml = errors.length ? `<section class="card flush warn" aria-label="Errors"><div class="errbar"><div class="row"><h2>Errors · ${errors.length}</h2><span class="small" style="color:var(--orange-ink)">${errors.map((e) => esc(e.auth_id) + ' overdrawn by ' + e.overdrawn_by).join(' · ')}</span></div>
      <button type="button" class="btn small" aria-expanded="${errOpen}" data-action="toggle-errors">${errOpen ? 'Minimise' : 'Show'}</button></div>
      ${errOpen ? errors.map((e) => `<div style="padding:16px 20px" class="stack"><p><strong>${esc(e.auth_id)}</strong> (${esc(PAYERS[e.payer_id] || e.payer_id)}, member ${esc(e.patient_id)}, ${esc(e.service_code)}) authorizes ${e.units_authorized} units. The ledger counts ${e.units_authorized + e.overdrawn_by} as used: ${e.used_by_paid_claims} by claims the payer really paid, plus predicted draws by ${e.contradicted_predictions.map(claimLink).join(', ')}. Both can't be true.</p>
        <div class="scroll"><table class="mini" style="min-width:600px"><thead><tr><th>Submitted</th><th>Claim</th><th>Outcome</th><th class="right">Units drawn</th><th class="right">Left after</th></tr></thead><tbody>
        ${e.trace.map((t) => `<tr class="${t.claim_id === e.first_conflict_claim ? 'hot' : ''}"><td>${esc(t.submitted_date)}</td><td>${claimLink(t.claim_id)}${t.dataset === 'to_check' ? ' (to submit)' : ''}</td><td>${t.source === 'predicted' ? '<span style="color:var(--blue)">Predicted ' + esc(t.outcome) + '</span>' : esc(t.outcome === 'paid' ? 'Paid by payer' : 'Denied by payer')}</td><td class="right">${t.units_drawn}</td><td class="right" style="${t.remaining_after < 0 ? 'color:var(--orange)' : ''}">${t.remaining_after}</td></tr>`).join('')}</tbody></table></div>
        <p>The payer's own decisions only make sense if ${e.contradicted_predictions.map(esc).join(', ')} never drew ${e.contradicted_predictions.length === 1 ? 'its' : 'their'} units: not processed first, or denied for a reason no rule shows. Counting only paid claims, <strong>${e.left_from_paid_claims_only} units are left</strong>. The affected claim carries a "contradicted by history" review flag.</p>
        <div class="row">${e.contradicted_predictions.map((x) => `<a class="btn primary small" href="#/claim/${esc(x)}">Review ${esc(x)}</a>`).join('')}</div></div>`).join('') : ''}</section>` : '';
  const rows = d.rows.map((a) => {
    const leftTxt = a.category === 'ended' ? `${a.left} lapsed` : a.left < 0 ? `over by ${-a.left}` : String(a.left);
    const leftPill = a.left < 0 ? 'solid' : a.category === 'used' ? 'denied' : a.category === 'low' ? 'neutral' : '';
    const isOpen = open && open.id === a.id;
    return `<tr class="click ${isOpen ? 'sel' : ''}" data-set="open=${isOpen ? '' : esc(a.id)}"><td class="nowrap"><strong class="num">${esc(a.id)}</strong><div class="sub">member ${esc(a.member)}</div></td>
      <td class="nowrap">${esc(a.payer)}<div class="sub">${esc(a.service)}</div></td><td class="nowrap num">${esc(a.end)}<div class="sub">from ${esc(a.start)}</div></td>
      <td style="width:30%">${bar(a.paid, a.predicted, a.authorized)}<div class="sub num">${a.paid} paid + ${a.predicted} predicted of ${a.authorized}</div></td>
      <td class="right">${leftPill ? pill(leftTxt, leftPill) : `<strong class="num">${esc(leftTxt)}</strong>`}</td><td class="right num">${a.claims}<div class="sub">${a.denied} denied</div></td></tr>
      ${isOpen ? `<tr><td colspan="6" style="background:#F9FAFB"><div class="scroll"><table class="mini" style="min-width:560px"><thead><tr><th>Submitted</th><th>Claim</th><th>Outcome</th><th class="right">Units drawn</th><th class="right">Left after</th></tr></thead><tbody>
        ${open.trace.map((t) => `<tr><td>${esc(t.submitted)}</td><td>${claimLink(t.claim_id)}${t.dataset === 'to_check' ? ' (to submit)' : ''}</td><td>${t.source === 'predicted' ? '<span style="color:var(--blue)">Predicted ' + esc(t.outcome) + (t.code ? ' ' + esc(t.code) : '') + '</span>' : (t.outcome === 'paid' ? 'Paid' : 'Denied ' + esc(t.code))}</td><td class="right">${t.units_drawn}</td><td class="right">${t.remaining_after ?? ''}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">No claims on this authorization.</td></tr>'}</tbody></table></div></td></tr>` : ''}`;
  }).join('');
  return `<div class="page"><div class="head"><div><h1>Authorizations</h1><p>Units left on every authorization, counting paid claims and claims still to be submitted, in submission order. Click a row for its ledger.</p></div></div>
    <div class="kpis">${kpi('Current authorizations', int(c.current || 0), 'not yet ended on ' + d.as_of)}${kpi('Used up', int(c.used || 0), 'further claims will be D02', 'orange')}
      ${kpi('Under 10% left', int(c.low || 0), 'ask the payer early')}${kpi('Errors to check', int(c.error || 0), 'more units used than authorized')}</div>
    ${errHtml}
    <section class="card flush"><div class="toolbar">${chips([['current', 'All current', c.current], ['used', 'Used up', c.used || 0], ['low', 'Under 10% left', c.low || 0], ['error', 'Errors', c.error || 0], ['ended', 'Ended', c.ended || 0]], f, 'filter')}</div>
      <div class="toolbar">${search(p.q, 'Search authorization, member or payer')}</div>
      <div class="scroll"><table class="data" style="min-width:980px"><thead><tr><th>Authorization</th><th>Payer · service</th><th>Ends</th><th>Units</th><th class="right">Left</th><th class="right">Claims</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div style="padding:0 16px 10px">${legend}</div>${pager(d)}</section></div>`;
};

/* ------------------------------------------------------------- Payer rules */
VIEWS.rules = async (_, p) => {
  const list = await api('payers');
  const pid = p.payer || (list.payers[0] && list.payers[0].id);
  const r = pid ? await api(`payers/${pid}/rules`) : null;
  const tabsHtml = list.payers.map((x) => `<button type="button" class="box" style="min-width:220px" aria-pressed="${x.id === pid}" data-set="payer=${x.id}"><div style="font-weight:700;font-size:15px">${esc(x.name)}</div>
      <div class="sub">${x.rules} rules · ${x.rules - x.learned} from documents${x.learned ? ', ' + x.learned + ' learned' : ''}${x.onboarded ? ' · onboarded' : ''}</div></button>`).join('') +
    list.pending.map((x) => `<a class="box" style="min-width:220px;text-decoration:none;color:inherit" href="#/add-payer?pid=${esc(x.id)}"><div style="font-weight:700;font-size:15px">${esc(x.name)}</div><div class="sub">Onboarding: not approved yet</div></a>`).join('');
  const rulesHtml = r ? r.rules.map((x) => `<tr><td><strong>${esc(KIND[x.kind] || x.kind)}</strong><div class="mono small muted">${esc(x.rule_id)}</div>${params2(x) ? `<div class="sub">${esc(params2(x))}</div>` : ''}</td>
      <td>${pill(x.denial_code, 'denied')}</td><td class="nowrap num">${esc(effective(x))}</td>
      <td>${pill(x.source.type === 'document' ? 'From document' : 'Learned from history', x.source.type === 'document' ? 'paid' : 'neutral')}<div class="quote" style="margin-top:4px">“${esc(x.source.quote)}”</div><div class="sub">${esc(x.source.ref)}</div>${x.source.evidence ? `<div class="sub">${esc(x.source.evidence)}</div>` : ''}</td></tr>`).join('') : '';
  return `<div class="page"><div class="head"><div><h1>Payer rules</h1><p>The approved rules the checker runs, each with the words it comes from. Changes go through <a href="#/notices">Notices</a> and <a href="#/review">Rules &amp; AI review</a>.</p></div>
      <a class="btn primary" href="#/add-payer">+ Add payer</a></div>
    <div class="row" style="gap:10px">${tabsHtml}</div>
    ${r ? `<section class="card small" style="color:var(--ink-2)"><strong style="color:var(--ink)">Code reported first when several rules fail:</strong> ${esc(r.denial_precedence.join(' › '))}<div class="sub">${esc((r.precedence_source || {}).note || '')}</div>
      ${r.approval ? `<div class="sub">Approved by ${esc(r.approval.approved_by)} on ${esc(r.approval.approved_on)} from extraction ${esc((r.approval.extraction_request_sha256 || '').slice(0, 12))}.</div>` : ''}</section>
    <section class="card flush"><div class="scroll"><table class="data" style="min-width:980px"><thead><tr><th>Rule</th><th>Denies with</th><th>Dates of care</th><th style="width:46%">Source</th></tr></thead><tbody>${rulesHtml}</tbody></table></div></section>
    <div class="grid3"><section class="card"><h2>Rules this payer does not apply</h2>${(r.not_applied || []).length ? `<ul class="small" style="margin:10px 0 0;padding-left:18px">${r.not_applied.map((n) => `<li><strong>${esc(n.topic.replace(/_/g, ' '))}:</strong> ${esc(n.note)}</li>`).join('')}</ul>` : '<p class="muted" style="margin-top:8px">None recorded.</p>'}</section>
      <section class="card"><h2>Open questions</h2>${(r.open_questions || []).length ? r.open_questions.map((q) => `<div style="margin-top:10px"><div class="quote small">“${esc(q.quote)}”</div><div>${esc(q.question)}</div></div>`).join('') : '<p class="muted" style="margin-top:8px">None.</p>'}</section></div>` : ''}</div>`;
};

/* -------------------------------------------------------------- Add payer */
VIEWS['add-payer'] = async (_, p) => {
  const pid = (p.pid || 'PY04').toUpperCase();
  const o = await api('onboarding/' + pid);
  const err = p.err ? `<div class="err">${esc(p.err)}</div>` : '';
  const step1done = o.registered;
  const s1 = `<li class="card ${step1done ? '' : 'current'}"><div class="step-h"><span class="dot">1</span><h2>Payer details and rule document</h2></div>
    ${step1done ? `<dl class="kv"><dt>Payer</dt><dd>${esc(pid)} · ${esc(o.name)}</dd><dt>Claims spell it</dt><dd>${esc([o.name, ...(o.aliases || [])].join(', '))}</dd><dt>Documents</dt><dd>${(o.documents || []).map(esc).join(', ')}</dd></dl>`
    : `<form id="reg" class="stack" style="margin-top:14px"><div class="form">
        <label class="f">Payer ID<input type="text" name="payer_id" value="${esc(pid)}" required pattern="PY[0-9]{2}"></label>
        <label class="f">Payer name<input type="text" name="name" value="${pid === 'PY04' ? 'Lakeside Community Health' : ''}" required></label>
        <label class="f">How claims exports spell it (comma-separated)<input type="text" name="aliases" value="${pid === 'PY04' ? 'LAKESIDE CH' : ''}"></label></div>
        <div class="row"><label class="f" style="flex:1 1 260px">Rule document (.md or .txt)<input type="file" name="file" accept=".md,.txt"></label>
          <button type="button" class="btn" data-action="demo-doc" data-name="lakeside_billing_manual.md" data-target="reg">Use demo manual</button></div>
        <input type="hidden" name="file_name"><label class="f">Document text<textarea name="text" required placeholder="Choose a file, or use the demo manual"></textarea></label>
        <div><button type="submit" class="btn primary">Register payer</button></div></form>`}</li>`;
  const prop = o.proposal, meta = o.meta;
  const s2 = `<li class="card ${step1done && !prop ? 'current' : ''}"><div class="row between"><div class="step-h"><span class="dot ${step1done ? '' : 'off'}">2</span><h2>Draft the rules with Claude</h2></div>
      ${step1done && !o.approved ? `<button type="button" class="btn primary" data-action="extract" data-id="${esc(pid)}">${prop ? 'Draft again (cached)' : 'Draft rules'}</button>` : ''}</div>
    <ul class="small" style="margin:12px 0 0;padding-left:18px;color:var(--ink-2)"><li>Claude Opus 5.5 reads the document; an approved payer's file is the worked example.</li><li>It may only use the engine's 10 rule types. Anything else becomes an open question, never a made-up rule.</li>
      <li>Every rule must quote the document word for word; quotes are checked against the file.</li><li>The response is stored under the fingerprint of its exact request, so drafting again gives the same draft.</li></ul>
    ${prop ? `<div class="kpis" style="margin-top:14px">${kpi('Rules drafted', prop.rules.length, meta.valid ? 'schema valid' : 'invalid: ' + (meta.validation_error || ''))}
        ${kpi('Quotes verified', (prop.rules.length - (meta.unverified_quotes || []).filter((x) => !x.startsWith('open_question') && !x.startsWith('not_applied')).length) + ' / ' + prop.rules.length, (meta.unverified_quotes || []).length ? 'not found: ' + meta.unverified_quotes.join(', ') : 'all found word for word')}
        ${kpi('Open questions', (o.open_questions || []).length, 'for a person to resolve')}
        ${o.answer_key ? kpi('Match with answer key', o.answer_key.matched.length + ' / ' + o.answer_key.document_rules_in_approved_file, (o.answer_key.missing_from_proposal.length ? 'missing ' + o.answer_key.missing_from_proposal.join(', ') + '; ' : '') + (o.answer_key.extra_in_proposal.length ? 'extra ' + o.answer_key.extra_in_proposal.join(', ') : (o.answer_key.parameter_differences.length ? o.answer_key.parameter_differences.length + ' parameter differences' : 'exact')), 'blue') : ''}</div>
      <div class="scroll" style="margin-top:14px"><table class="data" style="min-width:860px"><thead><tr><th>Drafted rule</th><th>Code</th><th>Dates of care</th><th style="width:48%">Quote</th></tr></thead><tbody>
      ${prop.rules.map((x) => `<tr><td><strong>${esc(KIND[x.kind] || x.kind)}</strong><div class="mono small muted">${esc(x.rule_id)}</div>${params2(x) ? `<div class="sub">${esc(params2(x))}</div>` : ''}</td><td>${pill(x.denial_code, 'denied')}</td><td class="nowrap">${esc(effective(x))}</td>
        <td><span class="quote">“${esc(x.source.quote)}”</span>${(meta.unverified_quotes || []).includes(x.rule_id) ? ' ' + pill('quote not found', 'solid') : ''}<div class="sub">${esc(x.source.ref)}</div></td></tr>`).join('')}</tbody></table></div>
      ${(o.open_questions || []).length ? `<h3 style="margin-top:16px">Open questions</h3>${o.open_questions.map((q) => `<div class="box" style="margin-top:8px"><div class="quote small">“${esc(q.quote)}”</div><div>${esc(q.question)}</div></div>`).join('')}` : ''}
      <p class="sub" style="margin-top:10px">Model ${esc(meta.model)} · worked example ${esc(meta.worked_example)} · request ${esc((meta.request_sha256 || '').slice(0, 12))}${o.answer_key ? ' · answer key ' + esc(o.answer_key.file) : ''}</p>` : ''}</li>`;
  const s3 = `<li class="card ${prop && !o.approved ? 'current' : ''}"><div class="step-h"><span class="dot ${prop ? '' : 'off'}">3</span><h2>Review and approve</h2></div>
    ${o.approved ? `<div class="ok" style="margin-top:12px">${esc(o.name)}'s rules are approved and live. <a href="#/rules?payer=${esc(pid)}">Open its rules</a>, or apply its notices on <a href="#/notices">Notices</a>.</div>`
      : prop ? `<p class="small" style="margin-top:10px;color:var(--ink-2)">Check each rule against its quote. To change one, edit <span class="mono">rules/proposed/${esc(pid)}.json</span> before approving: the difference is logged as a lesson for later drafts. With no claims history yet, the payer starts with a default code order and no learned rules.</p>
      <form id="approve" class="row" style="margin-top:12px" data-id="${esc(pid)}"><label class="f">Your name<input type="text" name="by" required></label><label class="f" style="flex:1 1 260px">Note<input type="text" name="note" placeholder="What you checked or changed"></label><button type="submit" class="btn primary" style="align-self:flex-end">Approve rules</button></form>`
      : '<p class="muted" style="margin-top:10px">Available after the draft.</p>'}</li>`;
  return `<div class="page" style="max-width:1000px"><div class="crumb"><a href="#/rules">Payer rules</a> / Add payer</div>
    <div class="head"><div><h1>Add a payer</h1><p>Register the payer and its rule document, Claude drafts its rule file, and you review every rule against its quote before anything checks a claim.</p></div></div>
    ${err}<ol class="steps">${s1}${s2}${s3}</ol></div>`;
};

/* ----------------------------------------------------------------- Notices */
VIEWS.notices = async (_, p) => {
  const [list, payers] = await Promise.all([api('notices'), api('payers')]);
  const f = p.f || 'all';
  const shown = list.filter((n) => f === 'all' || (f === 'applied' ? n.applied : !n.applied));
  const err = p.err ? `<div class="err">${esc(p.err)}</div>` : '';
  const cards = shown.map((n) => {
    let body = '';
    if (n.data_pack) {
      const ev = n.evaluation || {};
      body = `<p class="sub">Claude's change rebuilt the approved file exactly: ${esc(ev.rules_matching_approved || '')} rules, ${esc(ev.rules_left_untouched || '')} untouched, end dates ${ev.end_dates_correct ? 'correct' : 'wrong'}.</p>
        ${Object.entries(ev.drafted_end_rules || {}).map(([k, v]) => `<div class="row"><span class="tag late" style="width:56px">Ends</span><span>${esc(k)} on ${esc(v)}</span></div>`).join('')}
        ${(ev.open_questions || []).map((q) => `<div class="row" style="align-items:baseline"><span class="tag not_fixable" style="width:56px">Asks</span><span>${esc(q.question)}</span></div>`).join('')}`;
    } else if (n.draft) {
      const d = n.draft;
      body = `${d.end_rules.map((e) => `<div class="row" style="align-items:baseline"><span class="tag late" style="width:56px">Ends</span><span><strong>${esc(e.rule_id)}</strong> on ${esc(e.effective_to)}<span class="sub"> — “${esc(e.quote)}”</span></span></div>`).join('')}
        ${d.add_rules.map((r) => `<div class="row" style="align-items:baseline"><span class="tag ready" style="width:56px">Adds</span><span><strong>${esc(r.rule_id)}</strong>: ${esc(KIND[r.kind] || r.kind)} (${esc(r.denial_code)}) ${esc([r.effective_from && 'from ' + r.effective_from, r.effective_to && 'to ' + r.effective_to].filter(Boolean).join(' '))}<span class="sub"> — “${esc(r.quote)}”</span></span></div>`).join('')}
        ${d.open_questions.map((q) => `<div class="row" style="align-items:baseline"><span class="tag not_fixable" style="width:56px">Asks</span><span>${esc(q.question)}<span class="sub"> — “${esc(q.quote)}”</span></span></div>`).join('')}
        <p class="sub">${d.valid ? 'Valid' : 'Invalid: ' + esc(d.validation_error)} · quotes ${d.unverified_quotes.length ? 'not found: ' + esc(d.unverified_quotes.join(', ')) : 'all found in the notice'} · ${esc(d.model || '')}</p>
        ${n.answer_key ? `<p class="sub">Answer key (${esc(n.answer_key.file)}): ${n.answer_key.matched.length} / ${n.answer_key.document_rules_in_approved_file} rules match${n.answer_key.missing_from_proposal.length ? '; missing ' + esc(n.answer_key.missing_from_proposal.join(', ')) : ''}${n.answer_key.extra_in_proposal.length ? '; extra ' + esc(n.answer_key.extra_in_proposal.join(', ')) : ''}.</p>` : ''}
        ${n.applied ? '' : `<form class="row approve-notice" data-id="${esc(n.id)}"><label class="f">Your name<input type="text" name="by" required></label><label class="f" style="flex:1 1 240px">Note<input type="text" name="note"></label><button type="submit" class="btn primary" style="align-self:flex-end">Approve change</button></form>`}`;
    } else {
      body = `<div class="row"><button type="button" class="btn primary" data-action="draft-notice" data-id="${esc(n.id)}">Draft the change with Claude</button><span class="sub">Claude lists only the rules this notice ends and adds.</span></div>`;
    }
    return `<article class="card stack"><div class="row between"><div><div class="small muted" style="font-weight:600">${esc(n.payer_name)} · ${esc(n.file)}</div>
      <h2 style="font-size:17px;margin-top:4px">${esc((n.text.match(/\*\*Subject:\*\*\s*(.+)/) || [])[1] || n.text.split('\n')[0].replace(/^#\s*/, ''))}</h2></div>${pill(n.applied ? 'Applied' : 'Waiting', n.applied ? 'paid' : 'denied')}</div>
      <details><summary class="small" style="cursor:pointer">Read the notice</summary><div class="msg">${esc(n.text)}</div></details>${body}</article>`;
  }).join('');
  const approved = payers.payers;
  return `<div class="page" style="max-width:1060px"><div class="head"><div><h1>Notices</h1><p>Payer bulletins and provider notices that change billing rules. Claude drafts only the change each one makes: the rules it ends and the rules it adds. Rules it doesn't mention can't move.</p></div></div>
    ${err}<section class="card"><h2>Upload a notice</h2><form id="notice" class="stack" style="margin-top:12px"><div class="row">
      <label class="f">Payer<select name="payer_id">${approved.map((x) => `<option value="${x.id}" ${x.id === 'PY04' ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select></label>
      <label class="f" style="flex:1 1 240px">Notice (.md or .txt)<input type="file" name="file" accept=".md,.txt"></label>
      <button type="button" class="btn" style="align-self:flex-end" data-action="demo-doc" data-name="lakeside_notice_2026-10.md" data-target="notice">Use demo notice</button></div>
      <input type="hidden" name="file_name"><label class="f">Notice text<textarea name="text" required style="min-height:110px"></textarea></label><div><button type="submit" class="btn primary">Upload</button></div></form></section>
    <div class="row">${chips([['all', 'All', list.length], ['waiting', 'Waiting', list.filter((n) => !n.applied).length], ['applied', 'Applied', list.filter((n) => n.applied).length]], f, 'f')}</div>${cards}</div>`;
};

/* ------------------------------------------------------ Rules & AI review */
VIEWS.review = async () => {
  const r = await api('review');
  const H = r.hypotheses, X = r.extraction;
  const payerCards = r.payers.map((x) => `<div class="card"><div class="row between"><h2 style="font-size:15px">${esc(x.name)}</h2><span class="small muted">${x.rules} rules</span></div>
      <p class="small" style="margin-top:8px;color:var(--ink-2)">${x.rules - x.learned} from documents${x.learned ? ', ' + x.learned + ' learned from history' : ''}</p>
      <p class="sub">Code order: ${esc(x.order.join(' › '))}</p><a class="small" style="font-weight:600" href="#/rules?payer=${x.id}">Open ${esc(x.name.split(' ')[0])} rules</a></div>`).join('');
  const hyp = H ? H.hypotheses.map((e) => {
    const h = e.hypothesis, t = e.test, b = e.backtest;
    return `<section class="card stack" style="gap:16px"><div class="row between"><div><div class="small muted" style="font-weight:600">Proposed rule · from payer remarks · ${esc(H.model)}</div>
        <h2 style="font-size:20px;margin-top:4px">${esc(PAYERS[h.payer_id] || h.payer_id)}: ${esc(KIND[h.kind] || h.kind)} (${esc(h.denial_code)})</h2>
        <p style="margin-top:6px;color:var(--ink-2)">${esc(params2(h.params ? { params: Object.fromEntries(Object.entries(h.params).filter(([, v]) => v != null)) } : h))}${t.start ? ` · history puts the start at <strong>${esc(t.start)}</strong> by ${esc(t.basis.replace(/_/g, ' '))}` : ''}</p></div>
        ${pill(t.verdict, t.verdict === 'supported' ? 'paid' : 'denied')}</div>
      <div class="cols"><div class="main" style="flex:1 1 380px"><h3>Claude's reasoning</h3><div class="msg" style="margin-top:0">${esc(h.rationale)}</div></div>
        <div class="aside" style="flex:1 1 380px"><h3>Tested against ${int(t.claims_failing_rule)} claims that break it (by code, no model)</h3>
        <table class="mini"><thead><tr><th>Start date tried</th><th class="right">Denied ${esc(h.denial_code)}</th><th class="right">Denied D99</th><th class="right">Paid (contradicts)</th></tr></thead><tbody>
          <tr><td>No start date</td><td class="right">${t.without_start_date.denied_with_code}</td><td class="right">${t.without_start_date.denied_d99}</td><td class="right">${t.without_start_date.paid}</td></tr>
          ${(t.start_date_options || []).map((o) => `<tr class="${o.basis === t.basis ? 'hot' : ''}"><td>From ${esc(o.start)} by ${esc(o.basis.replace(/_/g, ' '))}</td><td class="right">${o.after.denied_with_code}</td><td class="right">${o.after.denied_d99}</td><td class="right">${o.after.paid}</td></tr>`).join('')}</tbody></table>
        ${b ? `<div class="kpis" style="margin-top:12px">${kpi('Backtest exact accuracy', pct(b.exact_accuracy_before) + ' → ' + pct(b.exact_accuracy_after), 'documents-only rules, then with this rule')}${kpi('Claims fixed / broken', b.claims_fixed + ' / ' + b.claims_broken, '')}</div>` : ''}</div></div>
      ${e.proposed_rule ? `<form class="row approve-hyp" data-id="${esc(h.hypothesis_id)}"><label class="f">Your name<input type="text" name="by" required></label><button type="submit" class="btn primary" style="align-self:flex-end">Approve into ${esc(PAYERS[h.payer_id] || h.payer_id)}'s rules</button>
        <span class="sub" style="align-self:center">Refused if the payer's file already has this rule.</span></form>` : ''}</section>`;
  }).join('') + (H.not_explainable.length ? `<section class="card"><h2>Groups Claude marked as not explainable</h2><table class="mini" style="margin-top:10px"><tbody>${H.not_explainable.map((n) => `<tr><td class="nowrap">${esc(PAYERS[n.payer_id] || n.payer_id)}</td><td class="nowrap">${esc(n.remark || '(paid)')}</td><td>${esc(n.reason)}</td></tr>`).join('')}</tbody></table></section>` : '') : '';
  const qs = r.open_questions.map((q) => `<li style="padding:12px 0;border-top:1px solid var(--line-2)"><div class="small muted" style="font-weight:600">${esc(PAYERS[q.payer_id] || q.payer_id)} · ${esc(q.where)}</div><div class="quote small" style="margin-top:4px">“${esc(q.quote)}”</div><div style="margin-top:4px">${esc(q.question)}</div></li>`).join('');
  const ex = X ? Object.entries(X.payers).map(([pid, c]) => `<tr><td>${esc(PAYERS[pid] || pid)}</td><td class="right">${c.matched.length} / ${c.document_rules_in_approved_file} rules match</td><td class="right">${c.unverified_quotes.length ? c.unverified_quotes.length + ' unverified' : 'quotes verified'}</td></tr>`).join('') : '';
  const bl = Object.entries(r.bulletins || {}).map(([pid, b]) => `<tr><td>${esc(PAYERS[pid] || pid)}</td><td class="right">${esc(b.rules_matching_approved)} match · ${esc(b.rules_left_untouched)} untouched</td></tr>`).join('');
  return `<div class="page"><div class="head"><div><h1>Rules &amp; AI review</h1><p>Claude reads payer documents and remarks and proposes rules. The claims history tests every proposal, and a person approves it. Only approved rules decide claims, without any model.</p></div></div>
    <div class="grid3">${payerCards}</div>${hyp}
    <section class="card"><div class="row between"><h2>Open questions</h2><span class="small muted">Raised by Claude while drafting · no rule was invented for them</span></div><ul style="list-style:none;margin:10px 0 0;padding:0">${qs || '<li class="muted">None.</li>'}</ul></section>
    <div class="grid3"><section class="card"><h2 style="font-size:15px">Drafts from the documents</h2><table class="mini" style="margin-top:10px"><tbody>${ex}</tbody></table>
        ${X && X.backtest && X.backtest.extracted_rules ? `<p class="sub" style="margin-top:8px">Backtest on drafted rules ${pct(X.backtest.extracted_rules.exact_code_accuracy)} vs approved ${pct(X.backtest.approved_rules.exact_code_accuracy)}.</p>` : ''}</section>
      <section class="card"><h2 style="font-size:15px">Bulletin changes</h2><table class="mini" style="margin-top:10px"><tbody>${bl}</tbody></table></section>
      <section class="card"><h2 style="font-size:15px">Reviewer corrections</h2><div class="kpi" style="border:0;padding:0"><div class="v">${r.corrections}</div></div><p class="sub">Edits made to drafts before approval are saved as lessons and included in Claude's next draft.</p></section></div></div>`;
};

/* ----------------------------------------------------------------- events */
document.addEventListener('click', async (e) => {
  const setEl = e.target.closest('[data-set]');
  if (setEl && !e.target.closest('a')) {
    e.preventDefault();
    const [k, ...v] = setEl.dataset.set.split('=');
    return setParams({ [k]: v.join('=') });
  }
  const act = e.target.closest('[data-action]');
  if (!act) return;
  const a = act.dataset.action;
  if (a === 'toggle-errors') {
    localStorage.setItem('errorsOpen', localStorage.getItem('errorsOpen') === 'no' ? 'yes' : 'no');
    return render();
  }
  if (a === 'copy') { navigator.clipboard && navigator.clipboard.writeText(act.dataset.text); act.textContent = 'Copied'; return; }
  if (a === 'note') { alert(act.dataset.text); return; }
  if (a === 'demo-doc') {
    const doc = await api('demo/' + act.dataset.name);
    const form = document.getElementById(act.dataset.target);
    form.elements.text.value = doc.text; form.elements.file_name.value = doc.file_name;
    return;
  }
  const busy = (label) => { act.disabled = true; act.innerHTML = `<span class="spin" aria-hidden="true"></span> ${esc(label)}`; };
  try {
    if (a === 'extract') { busy('Drafting with Claude…'); await post(`onboarding/${act.dataset.id}/extract`); }
    if (a === 'draft-notice') { busy('Drafting the change…'); await post(`notices/${act.dataset.id}/draft`); }
    if (a === 'draft-message') { busy('Drafting…'); await post(`messages/${act.dataset.id}`); }
    setParams({ err: '' }); render();
  } catch (err) { setParams({ err: err.message }); if (a === 'draft-message') alert(err.message); render(); }
});
document.addEventListener('change', (e) => {
  if (e.target.matches('[data-select]')) setParams({ [e.target.dataset.select]: e.target.value });
  if (e.target.matches('input[type=file]')) {
    const file = e.target.files[0], form = e.target.form;
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { form.elements.text.value = reader.result; form.elements.file_name.value = file.name; };
    reader.readAsText(file);
  }
});
document.addEventListener('submit', async (e) => {
  const f = e.target;
  e.preventDefault();
  const btn = f.querySelector('button[type=submit]');
  const busy = (label) => { if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spin" aria-hidden="true"></span> ${esc(label)}`; } };
  try {
    if (f.matches('[data-search]')) return setParams({ q: f.elements.q.value });
    if (f.id === 'reg') {
      busy('Registering…');
      const pid = f.elements.payer_id.value.trim().toUpperCase();
      await post('onboarding', { payer_id: pid, name: f.elements.name.value, aliases: f.elements.aliases.value.split(',').map((s) => s.trim()).filter(Boolean),
        file_name: f.elements.file_name.value || 'document.md', text: f.elements.text.value });
      location.hash = hashFor('add-payer', null, { pid });
      return render();
    }
    if (f.id === 'approve') {
      busy('Approving and rebuilding…');
      await post(`onboarding/${f.dataset.id}/approve`, { by: f.elements.by.value, note: f.elements.note.value });
      await refreshOverview();
    }
    if (f.id === 'notice') {
      busy('Uploading…');
      await post('notices', { payer_id: f.elements.payer_id.value, file_name: f.elements.file_name.value || 'notice.md', text: f.elements.text.value });
    }
    if (f.matches('.approve-notice')) {
      busy('Approving and rebuilding…');
      await post(`notices/${f.dataset.id}/approve`, { by: f.elements.by.value, note: f.elements.note.value });
      await refreshOverview();
    }
    if (f.matches('.approve-hyp')) {
      busy('Approving…');
      await post(`hypotheses/${f.dataset.id}/approve`, { by: f.elements.by.value });
      await refreshOverview();
    }
    setParams({ err: '' }); render();
  } catch (err) {
    if (f.matches('.approve-hyp')) { alert(err.message); return render(); }
    setParams({ err: err.message }); render();
  }
});
window.addEventListener('hashchange', render);
render();
