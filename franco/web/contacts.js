// Contact workspace: query the complete local database, with browser-local views.
let contactPage = 1, contactRequest = 0, contactTimer;
const contactSelection = new Map();
const contactFields = ['q', 'status', 'industry', 'score', 'sort', 'verified'];
function contactFilters() {
  return Object.fromEntries(contactFields.map(k => {
    const el = document.getElementById('contact-' + k);
    return [k, k === 'verified' ? el.checked : el.value];
  }));
}
function queueContactSearch() {
  // Invalidate an outstanding request immediately, before the debounce fires.
  contactRequest++;
  clearTimeout(contactTimer);
  contactTimer = setTimeout(resetContactSearch, 220);
}
function resetContactSearch() { contactPage = 1; contactSelection.clear(); loadProspects(); }
function clearContactFilters() {
  setContactFilters({});
  document.getElementById('contact-views').value = '';
  resetContactSearch();
}
function setContactFilters(f) {
  contactFields.forEach(k => {
    const el = document.getElementById('contact-' + k);
    if (k === 'verified') el.checked = f[k] === true;
    else el.value = f[k] ?? (k === 'score' ? '0' : k === 'sort' ? 'newest' : '');
  });
}
function readContactViews() {
  try {
    const views = JSON.parse(localStorage.getItem('franco-contact-views') || '[]');
    return Array.isArray(views) ? views.filter(v => v && typeof v.name === 'string' && v.filters && typeof v.filters === 'object') : [];
  } catch { return []; }
}
function renderContactViews() {
  const el = document.getElementById('contact-views'), selected = el.value;
  el.innerHTML = '<option value="">Choose a view</option>' + readContactViews().map((v, i) =>
    `<option value="${i}">${escHtml(v.name)}</option>`).join('');
  el.value = selected;
}
function saveContactView() {
  const name = document.getElementById('contact-view-name').value;
  if (!name.trim()) { showToast('Enter a name for this view.', 'error'); document.getElementById('contact-view-name').focus(); return; }
  const views = readContactViews();
  const view = {name: name.trim().slice(0, 80), filters: contactFilters()};
  const i = views.findIndex(v => v.name === view.name);
  if (i >= 0) views[i] = view; else views.push(view);
  try { localStorage.setItem('franco-contact-views', JSON.stringify(views)); }
  catch { showToast('Browser storage is unavailable. View was not saved.', 'error'); return; }
  renderContactViews();
  document.getElementById('contact-views').value = String(i >= 0 ? i : views.length - 1);
  showToast('View saved in this browser.', 'success');
}
function applyContactView() {
  const value = document.getElementById('contact-views').value;
  const view = value === '' ? null : readContactViews()[Number(value)];
  if (!view) return;
  setContactFilters(view.filters); resetContactSearch();
}
function deleteContactView() {
  const el = document.getElementById('contact-views');
  if (el.value === '') return;
  const views = readContactViews(); views.splice(Number(el.value), 1);
  try { localStorage.setItem('franco-contact-views', JSON.stringify(views)); }
  catch { showToast('Could not update browser storage.', 'error'); return; }
  el.value = ''; renderContactViews();
}
async function loadProspects() {
  const request = ++contactRequest;
  const f = contactFilters();
  const params = new URLSearchParams({q: f.q, status: f.status, industry: f.industry,
    min_score: f.score || '0', verified: f.verified, sort: f.sort, page: contactPage});
  const data = await api('/api/contacts/search?' + params);
  if (request !== contactRequest) return;
  const el = document.getElementById('prospects-table');
  if (!data || !Array.isArray(data.items)) {
    _prospects = []; contactSelection.clear(); updateContactSelection();
    el.innerHTML = offlineState();
    document.getElementById('contact-count').textContent = 'Search unavailable. Check your filters and refresh.';
    document.getElementById('contact-page').textContent = '';
    document.getElementById('contact-prev').disabled = true;
    document.getElementById('contact-next').disabled = true;
    return;
  }
  _prospects = data.items; contactPage = data.page;
  const industry = document.getElementById('contact-industry');
  industry.innerHTML = '<option value="">All industries</option>' + data.industries.map(v => `<option>${escHtml(v)}</option>`).join('');
  industry.value = f.industry;
  renderContactViews();
  document.getElementById('contact-count').textContent = `${data.total.toLocaleString()} matching contacts`;
  document.getElementById('contact-page').textContent = `Page ${data.page} of ${Math.max(1, Math.ceil(data.total / data.page_size))}`;
  document.getElementById('contact-prev').disabled = data.page <= 1;
  document.getElementById('contact-next').disabled = data.page * data.page_size >= data.total;
  el.innerHTML = !data.items.length ? emptyState('◎', 'No matching contacts', 'Try broader filters, import a CSV, or discover new prospects.') :
    '<div class="table-card"><table><thead><tr><th><input type="checkbox" id="contact-select-page" aria-label="Select this page" onchange="selectContactPage(this.checked)"></th><th>Contact</th><th>Company</th><th>Email</th><th>Score</th><th>Status</th><th></th></tr></thead><tbody>' +
    data.items.map((p, i) => `<tr><td><input class="contact-row-check" type="checkbox" aria-label="Select ${escHtml(p.first_name + ' ' + p.last_name)}" ${contactSelection.has(p.id) ? 'checked' : ''} onchange="selectContact(${i}, this.checked)"></td>
      <td><button class="contact-name" onclick="openContact(${i})"><span class="avatar">${escHtml((p.first_name || '?').slice(0, 1) + (p.last_name || '').slice(0, 1))}</span>${escHtml(p.first_name + ' ' + p.last_name)}</button><div class="contact-title">${escHtml(p.title)}</div></td>
      <td>${escHtml(p.company)}<div class="muted">${escHtml(p.industry)}</div></td><td>${escHtml(p.email || 'No email yet')} ${p.email ? emailTag(p) : ''}</td>
      <td><span class="score-pill">${escHtml(p.score)}</span></td><td>${badge(p.status)}</td><td><button class="btn btn-secondary btn-sm" onclick="fbProspect(${i})">Feedback</button></td></tr>`).join('') + '</tbody></table></div>';
  updateContactSelection();
}
function changeContactPage(delta) { contactPage += delta; loadProspects(); }
function selectContact(i, checked) {
  const p = _prospects[i];
  if (checked) contactSelection.set(p.id, p); else contactSelection.delete(p.id);
  updateContactSelection();
}
function selectContactPage(checked) {
  _prospects.forEach(p => { if (checked) contactSelection.set(p.id, p); else contactSelection.delete(p.id); });
  document.querySelectorAll('.contact-row-check').forEach(el => { el.checked = checked; });
  updateContactSelection();
}
function updateContactSelection() {
  const el = document.getElementById('contact-export');
  el.disabled = !contactSelection.size; el.textContent = `Export selected (${contactSelection.size})`;
  const check = document.getElementById('contact-select-page');
  if (check) {
    const count = _prospects.filter(p => contactSelection.has(p.id)).length;
    check.checked = count === _prospects.length && count > 0;
    check.indeterminate = count > 0 && count < _prospects.length;
  }
}
function contactCsvCell(value) {
  let s = String(value ?? '');
  // Prevent spreadsheet formulas, including formulas preceded by whitespace.
  if (/^[\s]*[=+@-]/.test(s) || /^[\t\r\n]/.test(s)) s = "'" + s;
  return '"' + s.replace(/"/g, '""') + '"';
}
function exportContactSelection() {
  if (!contactSelection.size) return;
  const fields = ['first_name','last_name','title','company','industry','email','email_verified','phone','score','status'];
  const csv = [fields, ...Array.from(contactSelection.values(), p => fields.map(k => p[k]))]
    .map(row => row.map(contactCsvCell).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob(['\ufeff' + csv], {type:'text/csv;charset=utf-8'}));
  const a = document.createElement('a'); a.href = url; a.download = 'franco-selected-contacts.csv'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function openContact(i) {
  const p = _prospects[i];
  const fields = [['Company',p.company],['Title',p.title],['Industry',p.industry],['Company size',p.company_size],['Email',p.email],['Phone',p.phone],['Source',p.source],['Score',p.score],['Research notes',p.personalization_notes]];
  document.getElementById('contact-detail').innerHTML = `<div class="eyebrow">CONTACT PROFILE</div><h2 id="contact-detail-name">${escHtml(p.first_name + ' ' + p.last_name)}</h2>${badge(p.status)}<dl>` +
    fields.map(([k,v]) => `<dt>${k}</dt><dd>${escHtml(v ?? '') || '—'}</dd>`).join('') + '</dl>' +
    (safeUrl(p.linkedin_url) ? `<a class="btn btn-secondary" href="${escHtml(safeUrl(p.linkedin_url))}" target="_blank" rel="noopener">View LinkedIn ↗</a>` : '');
  document.getElementById('contact-dialog').showModal();
}
