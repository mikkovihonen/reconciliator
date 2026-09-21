// Reconciliator Web UI Core Application Logic

let currentRole = localStorage.getItem('reconciliator_role') || 'Admin';
let searchMode = 'lexical';
let currentView = 'search';
let activeChangeset = null;
let systemReadiness = null;
let cy = null;

// Helper: HTTP request with Auth and Dev Role headers
async function fetchWithAuth(url, options = {}) {
  const headers = options.headers || {};
  headers['X-Dev-Role'] = currentRole;
  if (!headers['Content-Type'] && options.body && typeof options.body === 'string') {
    headers['Content-Type'] = 'application/json';
  }
  return fetch(url, { ...options, headers });
}

// ---------------------------------------------------------------------------
// Initialization & Theme
// ---------------------------------------------------------------------------

function initTheme() {
  const isDark = document.documentElement.classList.contains('dark');
  updateThemeIcon(isDark);
}

function toggleTheme() {
  const isDark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('reconciliator_theme', isDark ? 'dark' : 'light');
  updateThemeIcon(isDark);
  if (cy) {
    updateCytoscapeTheme(isDark);
  }
}

function updateThemeIcon(isDark) {
  // Theme SVG icons switch automatically via dark: CSS utility classes
}

document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  updateRoleUI();
  await loadAuthConfig();
  await loadSystemReadiness();
  await initActiveChangeset();
  setupSSE();
  switchView('search');
});

// ---------------------------------------------------------------------------
// Role & Auth State
// ---------------------------------------------------------------------------

async function loadAuthConfig() {
  try {
    const res = await fetch('/api/v1/auth/config');
    const data = await res.json();
    if (!data.dev_mode) {
      document.getElementById('dev-role-container').classList.add('hidden');
    }
  } catch (e) {
    console.warn('Failed to load auth config:', e);
  }
}

function updateRoleUI() {
  document.getElementById('current-role-badge').innerText = currentRole;
  const btnCommit = document.getElementById('btn-commit-changeset');
  const guardMsg = document.getElementById('commit-guard-msg');
  const btnPipeline = document.getElementById('btn-pipeline-drawer');

  if (currentRole === 'Admin') {
    if (btnCommit) btnCommit.disabled = false;
    if (guardMsg) guardMsg.classList.add('hidden');
    if (btnPipeline) btnPipeline.classList.remove('opacity-50', 'pointer-events-none');
  } else {
    if (btnCommit) btnCommit.disabled = true;
    if (guardMsg) guardMsg.classList.remove('hidden');
    if (btnPipeline) btnPipeline.classList.add('opacity-50', 'pointer-events-none');
  }
}

function toggleRoleDropdown() {
  const dropdown = document.getElementById('role-dropdown');
  dropdown.classList.toggle('hidden');
}

function setDevRole(role) {
  currentRole = role;
  localStorage.setItem('reconciliator_role', role);
  updateRoleUI();
  document.getElementById('role-dropdown').classList.add('hidden');
  // Refresh current view
  if (currentView === 'search') executeSearch();
  else if (currentView === 'consolidation') loadConsolidations();
  else if (currentView === 'reconciliation') loadReconciledRecords();
  else if (currentView === 'changesets') loadChangesets();
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

function switchView(viewName) {
  currentView = viewName;
  ['search', 'consolidation', 'reconciliation', 'changesets'].forEach(v => {
    const el = document.getElementById(`view-${v}`);
    const navBtn = document.getElementById(`nav-${v}`);
    if (v === viewName) {
      el.classList.remove('hidden');
      navBtn.className = 'nav-btn px-3 py-1.5 rounded-md text-sm font-medium text-white bg-brand-teal-600 shadow-sm transition';
    } else {
      el.classList.add('hidden');
      navBtn.className = 'nav-btn px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 dark:text-slate-300 hover:text-slate-900 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-slate-800/60 transition';
    }
  });

  if (viewName === 'search') {
    if (!document.getElementById('search-container').hasChildNodes()) {
      executeSearch();
    }
  } else if (viewName === 'consolidation') {
    loadConsolidations();
  } else if (viewName === 'reconciliation') {
    loadReconciledRecords();
  } else if (viewName === 'changesets') {
    loadChangesets();
  }
}

// ---------------------------------------------------------------------------
// Data Readiness & System State
// ---------------------------------------------------------------------------

async function loadSystemReadiness() {
  try {
    const res = await fetch('/api/v1/system/readiness');
    systemReadiness = await res.json();
    renderReadiness();
  } catch (e) {
    console.error('Failed to load system readiness:', e);
  }
}

function renderReadiness() {
  if (!systemReadiness) return;
  const { overall_readiness, entity_counts, vector_store, pipeline_readiness } = systemReadiness;

  const ind = document.getElementById('readiness-indicator');
  const txt = document.getElementById('readiness-text');
  const badge = document.getElementById('overall-badge');

  if (overall_readiness === 'unpopulated') {
    ind.className = 'w-2.5 h-2.5 rounded-full bg-brand-amber-500';
    txt.innerText = 'Unpopulated Database — Ready for Ingest';
    badge.className = 'px-2 py-0.5 rounded text-xs font-semibold bg-brand-amber-50 dark:bg-amber-950/80 text-brand-amber-800 dark:text-amber-300 border border-brand-amber-200 dark:border-amber-800';
    badge.innerText = 'Unpopulated';
  } else if (overall_readiness === 'partially_populated') {
    ind.className = 'w-2.5 h-2.5 rounded-full bg-sky-500';
    txt.innerText = 'Partially Populated';
    badge.className = 'px-2 py-0.5 rounded text-xs font-semibold bg-sky-50 dark:bg-sky-950/80 text-sky-800 dark:text-sky-300 border border-sky-200 dark:border-sky-800';
    badge.innerText = 'Partially Ready';
  } else {
    ind.className = 'w-2.5 h-2.5 rounded-full bg-emerald-500';
    txt.innerText = 'Fully Ready for Reconciliation';
    badge.className = 'px-2 py-0.5 rounded text-xs font-semibold bg-emerald-50 dark:bg-emerald-950/80 text-emerald-800 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800';
    badge.innerText = 'Ready';
  }

  // Micro stats
  document.getElementById('stat-works').innerText = `Works: ${(entity_counts.works || 0).toLocaleString()}`;
  document.getElementById('stat-agents').innerText = `Agents: ${(entity_counts.agents || 0).toLocaleString()}`;
  document.getElementById('stat-vocab').innerText = `Concepts: ${(entity_counts.concepts || 0).toLocaleString()}`;
  document.getElementById('stat-consolidations').innerText = `Consolidated: ${(entity_counts.work_consolidations || 0).toLocaleString()}`;
  document.getElementById('stat-vector').innerText = `Vectors: ${(vector_store.work_vectors || 0).toLocaleString()}`;

  // Vector status badge
  const vBadge = document.getElementById('vector-status-badge');
  if (vector_store.available) {
    vBadge.innerText = `Vector Store: Ready (${vector_store.work_vectors.toLocaleString()} vectors)`;
    vBadge.className = 'ml-auto text-[11px] text-emerald-700 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-950/60 px-2 py-0.5 rounded border border-emerald-200 dark:border-emerald-800';
  } else {
    vBadge.innerText = `Vector Store: Empty (0 vectors) — Lexical fallback active`;
    vBadge.className = 'ml-auto text-[11px] text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-950/60 px-2 py-0.5 rounded border border-amber-200 dark:border-amber-800';
  }

  // Grid details
  const grid = document.getElementById('readiness-grid');
  grid.innerHTML = '';
  const stages = [
    { key: 'vocabularies', label: '1. Controlled Vocabularies' },
    { key: 'authorities', label: '2. Authority Catalogues' },
    { key: 'vector_index', label: '3. Vector Store Indexes' },
    { key: 'work_consolidation', label: '4. Work Consolidations' },
    { key: 'agent_clustering', label: '5. Agent Clusters' },
    { key: 'corpus_reconciliation', label: '6. Corpus Reconciliation' },
  ];

  stages.forEach(st => {
    const info = pipeline_readiness[st.key] || { ready: false, message: 'Uninitialized' };
    const statusBg = info.ready ? 'border-emerald-200 dark:border-emerald-700/60 bg-emerald-50/60 dark:bg-emerald-950/30' : 'border-slate-200 dark:border-slate-700 bg-white/70 dark:bg-slate-900/60';
    const statusText = info.ready ? 'text-emerald-700 dark:text-emerald-400 font-semibold' : 'text-slate-500 dark:text-slate-400';
    const card = document.createElement('div');
    card.className = `p-3 rounded-lg border ${statusBg} flex flex-col justify-between shadow-xs`;
    card.innerHTML = `
      <div class="flex items-center justify-between mb-1">
        <span class="font-semibold text-slate-800 dark:text-slate-200">${st.label}</span>
        <span class="${statusText}">${info.ready ? 'Ready' : 'Pending'}</span>
      </div>
      <p class="text-[11px] text-slate-500 dark:text-slate-400 mt-1">${info.message}</p>
    `;
    grid.appendChild(card);
  });
}

function toggleReadinessDetails() {
  const details = document.getElementById('readiness-details');
  const arrow = document.getElementById('readiness-arrow');
  details.classList.toggle('hidden');
  arrow.innerText = details.classList.contains('hidden') ? '▼' : '▲';
}

// ---------------------------------------------------------------------------
// Search & Database Explorer
// ---------------------------------------------------------------------------

function setSearchMode(mode) {
  searchMode = mode;
  ['lexical', 'semantic', 'hybrid'].forEach(m => {
    const btn = document.getElementById(`mode-${m}`);
    if (m === mode) {
      btn.className = 'px-3 py-1.5 rounded-md font-medium bg-brand-teal-600 text-white shadow-sm transition';
    } else {
      btn.className = 'px-3 py-1.5 rounded-md font-medium text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white transition';
    }
  });
  executeSearch();
}

async function executeSearch() {
  const query = document.getElementById('search-input').value.trim();
  const entityType = document.getElementById('filter-type').value;
  const workType = document.getElementById('filter-work-type')?.value;
  const source = document.getElementById('filter-source').value.trim();
  const container = document.getElementById('search-container');

  // If query is empty and DB is unpopulated, show guided welcome state
  if (!query && systemReadiness && systemReadiness.overall_readiness === 'unpopulated') {
    container.innerHTML = `
      <div class="min-h-[50vh] flex flex-col items-center justify-center text-center px-4 py-8">
        <div class="max-w-lg space-y-4">
          <div>
            <h3 class="text-base font-bold text-slate-900 dark:text-white tracking-tight">Database Is Currently Unpopulated</h3>
            <p class="text-xs text-slate-600 dark:text-slate-400 mt-1 leading-relaxed max-w-md mx-auto">
              Reconciliator has connected to PostgreSQL, but no bibliographic authority records or vocabularies have been ingested yet.
            </p>
          </div>
          <div class="text-xs font-mono text-slate-600 dark:text-slate-400 space-y-1">
            <div class="text-slate-500 dark:text-slate-400 font-sans font-semibold text-[11px] uppercase tracking-wider mb-1.5">Quick Ingest via CLI:</div>
            <div><span class="text-brand-teal-600 dark:text-brand-teal-400 font-bold">$</span> reconciliator ingest-vocab</div>
            <div><span class="text-brand-teal-600 dark:text-brand-teal-400 font-bold">$</span> reconciliator ingest-oai</div>
            <div><span class="text-brand-teal-600 dark:text-brand-teal-400 font-bold">$</span> reconciliator run</div>
          </div>
        </div>
      </div>
    `;
    return;
  }

  container.innerHTML = `<div class="min-h-[40vh] flex items-center justify-center text-slate-500 dark:text-slate-400 text-xs animate-pulse">Searching database...</div>`;

  try {
    const effectiveQuery = query || '*';
    const params = new URLSearchParams({
      q: effectiveQuery,
      mode: searchMode,
      type: entityType,
      limit: '30'
    });
    if (source) params.append('source', source);
    if (workType) params.append('work_type', workType);

    const res = await fetchWithAuth(`/api/v1/search?${params.toString()}`);
    const data = await res.json();
    renderSearchResults(data);
  } catch (e) {
    container.innerHTML = `<div class="p-6 bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 rounded-lg text-red-700 dark:text-red-300 text-xs">Search error: ${e.message}</div>`;
  }
}

function renderSearchResults(data) {
  const container = document.getElementById('search-container');
  const { works = [], manifestations = [], agents = [], total = 0, mode, vector_available } = data;

  if (total === 0) {
    container.innerHTML = `
      <div class="min-h-[50vh] flex flex-col items-center justify-center text-center px-4 space-y-1.5">
        <p class="text-base font-semibold text-slate-800 dark:text-slate-200">No matching records found</p>
        <p class="text-xs text-slate-500 dark:text-slate-400">Try broadening your search term or clearing source filters.</p>
      </div>
    `;
    return;
  }

  let html = `
    <div class="flex items-center justify-between text-xs text-slate-600 dark:text-slate-400 mb-3 px-1">
      <span>Found <strong class="text-slate-900 dark:text-white">${total}</strong> matching entities (${mode} mode)</span>
      ${!vector_available && mode === 'semantic' ? '<span class="text-brand-amber-600 dark:text-amber-400">Vector store unpopulated; fell back to lexical</span>' : ''}
    </div>
    <div class="space-y-3">
  `;

  // Render Works
  works.forEach(w => {
    html += `
      <div class="bg-white dark:bg-slate-800/80 border border-slate-200 dark:border-slate-700/80 hover:border-brand-teal-300 dark:hover:border-slate-600 rounded-lg p-4 transition space-y-2 shadow-sm">
        <div class="flex items-start justify-between">
          <div>
            <div class="flex items-center space-x-2">
              <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-brand-teal-50 dark:bg-brand-teal-950/80 text-brand-teal-700 dark:text-brand-teal-300 border border-brand-teal-200 dark:border-brand-teal-800">
                Work • ${w.work_type || 'text'}
              </span>
              <span class="text-xs text-slate-500 dark:text-slate-400 font-mono">${w.source}:${w.source_id}</span>
              ${w.score ? `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-emerald-50 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800">score: ${w.score}</span>` : ''}
            </div>
            <h4 class="text-sm font-bold text-slate-900 dark:text-white mt-1.5">${w.title}</h4>
          </div>
          <div class="flex items-center space-x-2">
            <button onclick="viewWorkTree(${w.id})" class="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-700 dark:text-white rounded text-xs transition">
              Tree View
            </button>
            <button onclick="promptStageOperation('work_merge', 'work', '${w.id}', '${w.title.replace(/'/g, "\\'")}')" class="px-2.5 py-1 bg-brand-amber-50 hover:bg-brand-amber-100 dark:bg-indigo-900/60 dark:hover:bg-indigo-800 border border-brand-amber-200 dark:border-indigo-700/60 text-brand-amber-900 dark:text-indigo-200 rounded text-xs transition">
              + Changeset
            </button>
          </div>
        </div>
      </div>
    `;
  });

  // Render Manifestations
  manifestations.forEach(m => {
    html += `
      <div class="bg-white dark:bg-slate-800/80 border border-slate-200 dark:border-slate-700/80 rounded-lg p-4 space-y-2 shadow-sm">
        <div class="flex items-center space-x-2">
          <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-purple-50 dark:bg-purple-950/80 text-purple-700 dark:text-purple-300 border border-purple-200 dark:border-purple-800">
            Manifestation • ${m.carrier_type || 'volume'}
          </span>
          <span class="text-xs text-slate-500 dark:text-slate-400 font-mono">${m.source}:${m.source_id}</span>
          ${m.publication_year ? `<span class="text-xs text-slate-500 dark:text-slate-400">(${m.publication_year})</span>` : ''}
        </div>
        <h4 class="text-sm font-semibold text-slate-900 dark:text-white">${m.title}</h4>
        ${m.publisher ? `<p class="text-xs text-slate-500 dark:text-slate-400">Publisher: ${m.publisher}</p>` : ''}
      </div>
    `;
  });

  // Render Agents
  agents.forEach(a => {
    html += `
      <div class="bg-white dark:bg-slate-800/80 border border-slate-200 dark:border-slate-700/80 rounded-lg p-4 space-y-2 shadow-sm">
        <div class="flex items-center space-x-2">
          <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-brand-amber-50 dark:bg-brand-amber-950/80 text-brand-amber-800 dark:text-brand-amber-300 border border-brand-amber-200 dark:border-brand-amber-800">
            Agent
          </span>
          <span class="text-xs text-slate-500 dark:text-slate-400 font-mono">${a.source}:${a.source_id}</span>
        </div>
        <h4 class="text-sm font-bold text-slate-900 dark:text-white">${a.name}</h4>
        ${a.dates ? `<p class="text-xs text-slate-500 dark:text-slate-400 font-mono">Dates: ${a.dates}</p>` : ''}
      </div>
    `;
  });

  html += `</div>`;
  container.innerHTML = html;
}

// ---------------------------------------------------------------------------
// WEMI Hierarchy Tree Modal
// ---------------------------------------------------------------------------

async function viewWorkTree(workId) {
  try {
    const res = await fetchWithAuth(`/api/v1/works/${workId}/tree`);
    const data = await res.json();
    alert(`WEMI Tree for "${data.title}":\n\n` + JSON.stringify(data, null, 2));
  } catch (e) {
    alert(`Failed to load WEMI tree: ${e.message}`);
  }
}

// ---------------------------------------------------------------------------
// Consolidations & Cytoscape Graph
// ---------------------------------------------------------------------------

async function loadConsolidations() {
  const content = document.getElementById('consolidation-content');
  content.innerHTML = `<div class="min-h-[40vh] flex items-center justify-center text-slate-400 text-xs animate-pulse">Loading work consolidation groups...</div>`;

  try {
    const res = await fetchWithAuth('/api/v1/consolidation/works');
    const data = await res.json();
    renderConsolidationGroups(data.items || []);
  } catch (e) {
    content.innerHTML = `<div class="p-6 bg-red-950/40 border border-red-800 rounded-lg text-red-300 text-xs">Error loading consolidations: ${e.message}</div>`;
  }
}

function renderConsolidationGroups(groups) {
  const content = document.getElementById('consolidation-content');

  if (groups.length === 0) {
    content.innerHTML = `
      <div class="min-h-[55vh] flex flex-col items-center justify-center text-center px-4">
        <div class="max-w-md space-y-1.5">
          <h3 class="text-base font-bold text-slate-900 dark:text-white tracking-tight">No Work Consolidations Found</h3>
          <p class="text-xs text-slate-600 dark:text-slate-400 leading-relaxed">
            Cross-source duplicate work identity consolidation has not been computed yet. Run <code class="text-brand-teal-700 dark:text-sky-400 font-mono">consolidate-works</code> via CLI or the Pipeline Jobs panel.
          </p>
        </div>
      </div>
    `;
    return;
  }

  let html = `
    <div class="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden shadow-sm">
      <table class="w-full text-left text-xs">
        <thead class="bg-slate-50 dark:bg-slate-900/80 text-slate-500 dark:text-slate-400 border-b border-slate-200 dark:border-slate-700 uppercase tracking-wider text-[10px]">
          <tr>
            <th class="py-3 px-4">Canonical Work</th>
            <th class="py-3 px-4">Catalogue</th>
            <th class="py-3 px-4">Type</th>
            <th class="py-3 px-4 text-center">Members</th>
            <th class="py-3 px-4 text-right">Actions</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100 dark:divide-slate-700/60">
  `;

  groups.forEach(g => {
    html += `
      <tr class="hover:bg-slate-50 dark:hover:bg-slate-750 transition">
        <td class="py-3 px-4">
          <div class="font-bold text-slate-900 dark:text-white">${g.canonical_title}</div>
          <div class="text-[11px] text-slate-500 dark:text-slate-400 font-mono">${g.canonical_source}:${g.canonical_source_id}</div>
        </td>
        <td class="py-3 px-4 text-slate-700 dark:text-slate-300 uppercase font-medium">${g.canonical_source}</td>
        <td class="py-3 px-4 text-slate-500 dark:text-slate-400 font-mono">${g.work_type}</td>
        <td class="py-3 px-4 text-center">
          <span class="px-2 py-0.5 rounded-full bg-brand-teal-50 dark:bg-sky-950 text-brand-teal-800 dark:text-sky-300 font-bold border border-brand-teal-200 dark:border-sky-800 text-[11px]">
            ${g.member_count} works
          </span>
        </td>
        <td class="py-3 px-4 text-right space-x-2">
          <button onclick="openGraphModal(${g.canonical_work_id}, '${g.canonical_title.replace(/'/g, "\\'")}')" class="px-2.5 py-1 bg-brand-teal-600 hover:bg-brand-teal-500 text-white rounded font-medium transition shadow-xs">
            View Graph
          </button>
        </td>
      </tr>
    `;
  });

  html += `</tbody></table></div>`;
  content.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Cytoscape Graph Modal & Theming
// ---------------------------------------------------------------------------

async function openGraphModal(canonicalId, title) {
  document.getElementById('graph-title').innerText = `Equivalence Graph: ${title}`;
  document.getElementById('graph-modal').classList.remove('hidden');

  try {
    const res = await fetchWithAuth(`/api/v1/consolidation/works/${canonicalId}/graph`);
    const graphData = await res.json();
    initCytoscape(graphData);
  } catch (e) {
    alert(`Failed to load graph: ${e.message}`);
  }
}

function getCytoscapeStyles(isDark) {
  return [
    {
      selector: 'node',
      style: {
        'label': 'data(label)',
        'color': isDark ? '#f8fafc' : '#0f172a',
        'font-size': '10px',
        'font-weight': isDark ? 'normal' : '600',
        'text-valign': 'bottom',
        'text-margin-y': 4,
        'background-color': '#2c6f6a', // Baltic Teal
        'width': 28,
        'height': 28,
        'border-width': 2,
        'border-color': isDark ? '#55a398' : '#1d4e4a',
      }
    },
    {
      selector: 'node[?is_canonical]',
      style: {
        'background-color': '#c5753a', // Warm Amber
        'border-color': isDark ? '#e3ac55' : '#934c12',
        'width': 36,
        'height': 36,
        'font-weight': 'bold',
      }
    },
    {
      selector: 'edge',
      style: {
        'width': 2,
        'line-color': isDark ? '#64748b' : '#94a3b8',
        'curve-style': 'bezier',
        'label': 'data(relationship)',
        'color': isDark ? '#94a3b8' : '#475569',
        'font-size': '8px',
      }
    }
  ];
}

function updateCytoscapeTheme(isDark) {
  if (!cy) return;
  const styles = getCytoscapeStyles(isDark);
  cy.style().fromJson(styles).update();
}

function initCytoscape(graphData) {
  const container = document.getElementById('cy-container');
  if (cy) {
    cy.destroy();
  }

  const isDark = document.documentElement.classList.contains('dark');

  cy = cytoscape({
    container: container,
    elements: graphData,
    style: getCytoscapeStyles(isDark),
    layout: {
      name: 'cose',
      animate: false,
      padding: 30,
    }
  });

  cy.on('tap', 'node', function(evt) {
    const node = evt.target;
    const d = node.data();
    if (confirm(`Work: ${d.label}\nSource: ${d.source}:${d.source_id}\nCanonical: ${d.is_canonical}\n\nElect this work as Canonical in active changeset?`)) {
      stageElectCanonical(d.id);
    }
  });
}

function closeGraphModal() {
  document.getElementById('graph-modal').classList.add('hidden');
}

// ---------------------------------------------------------------------------
// Reconciliation Review
// ---------------------------------------------------------------------------

async function loadReconciledRecords() {
  const content = document.getElementById('reconciliation-content');
  content.innerHTML = `<div class="min-h-[40vh] flex items-center justify-center text-slate-400 text-xs animate-pulse">Loading reconciled records...</div>`;

  try {
    const res = await fetchWithAuth('/api/v1/reconciliation/records');
    const data = await res.json();
    renderReconciliationRecords(data.items || []);
  } catch (e) {
    content.innerHTML = `<div class="p-6 bg-red-950/40 border border-red-800 rounded-lg text-red-300 text-xs">Error loading reconciled records: ${e.message}</div>`;
  }
}

function renderReconciliationRecords(records) {
  const content = document.getElementById('reconciliation-content');

  if (records.length === 0) {
    content.innerHTML = `
      <div class="min-h-[55vh] flex flex-col items-center justify-center text-center px-4">
        <div class="max-w-md space-y-1.5">
          <h3 class="text-base font-bold text-slate-900 dark:text-white tracking-tight">No Reconciled Records Available</h3>
          <p class="text-xs text-slate-600 dark:text-slate-400 leading-relaxed">
            Target corpus reconciliation has not been executed yet. Run <code class="text-brand-teal-700 dark:text-sky-400 font-mono">reconciliator reconcile</code> to match target records against authority data and populate the review queue.
          </p>
        </div>
      </div>
    `;
    return;
  }

  let html = `
    <div class="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden shadow-sm">
      <table class="w-full text-left text-xs">
        <thead class="bg-slate-50 dark:bg-slate-900/80 text-slate-500 dark:text-slate-400 border-b border-slate-200 dark:border-slate-700 uppercase tracking-wider text-[10px]">
          <tr>
            <th class="py-3 px-4">Record ID</th>
            <th class="py-3 px-4">Matched Authority Work</th>
            <th class="py-3 px-4">Work Cluster</th>
            <th class="py-3 px-4">Expression Cluster</th>
            <th class="py-3 px-4 text-right">Actions</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100 dark:divide-slate-700/60">
  `;

  records.forEach(r => {
    html += `
      <tr class="hover:bg-slate-50 dark:hover:bg-slate-750 transition">
        <td class="py-3 px-4 font-mono text-slate-900 dark:text-white font-semibold">${r.record_id}</td>
        <td class="py-3 px-4 font-mono text-brand-teal-700 dark:text-sky-400">${r.final_work_source_id || '—'}</td>
        <td class="py-3 px-4 font-mono text-slate-500 dark:text-slate-400">${r.work_cluster_id || '—'}</td>
        <td class="py-3 px-4 font-mono text-slate-500 dark:text-slate-400">${r.expression_cluster_id || '—'}</td>
        <td class="py-3 px-4 text-right">
          <button onclick="promptRelinkRecord('${r.record_id}')" class="px-2.5 py-1 bg-brand-amber-50 hover:bg-brand-amber-100 dark:bg-indigo-900/60 dark:hover:bg-indigo-800 border border-brand-amber-200 dark:border-indigo-700 text-brand-amber-900 dark:text-indigo-200 rounded font-medium text-xs transition">
            Relink / Override
          </button>
        </td>
      </tr>
    `;
  });

  html += `</tbody></table></div>`;
  content.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Review Changesets & Curation Staging
// ---------------------------------------------------------------------------

async function initActiveChangeset() {
  try {
    const res = await fetchWithAuth('/api/v1/changesets?status=draft&limit=1');
    const data = await res.json();
    if (data.items && data.items.length > 0) {
      activeChangeset = data.items[0];
      renderActiveChangesetPill();
    }
  } catch (e) {
    console.debug('No active draft changeset found:', e);
  }
}

function renderActiveChangesetPill() {
  const pill = document.getElementById('active-changeset-pill');
  if (activeChangeset) {
    document.getElementById('active-changeset-name').innerText = activeChangeset.title;
    document.getElementById('active-changeset-op-count').innerText = activeChangeset.operations_count || 0;
    pill.classList.remove('hidden');
    pill.classList.add('flex');
  } else {
    pill.classList.add('hidden');
    pill.classList.remove('flex');
  }
}

async function loadChangesets() {
  const content = document.getElementById('changesets-content');
  content.innerHTML = `<div class="min-h-[40vh] flex items-center justify-center text-slate-500 dark:text-slate-400 text-xs animate-pulse">Loading review changesets...</div>`;

  try {
    const res = await fetchWithAuth('/api/v1/changesets');
    const data = await res.json();
    renderChangesetsList(data.items || []);
  } catch (e) {
    content.innerHTML = `<div class="p-6 bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 rounded-lg text-red-700 dark:text-red-300 text-xs">Error loading changesets: ${e.message}</div>`;
  }
}

function renderChangesetsList(changesets) {
  const content = document.getElementById('changesets-content');

  if (changesets.length === 0) {
    content.innerHTML = `
      <div class="min-h-[55vh] flex flex-col items-center justify-center text-center px-4">
        <div class="max-w-md space-y-1.5">
          <h3 class="text-base font-bold text-slate-900 dark:text-white tracking-tight">No Review Changesets Yet</h3>
          <p class="text-xs text-slate-600 dark:text-slate-400 leading-relaxed">
            Changesets allow curators to stage and review manual overrides before committing them atomically to the database. Click "+ New Changeset" above to start.
          </p>
        </div>
      </div>
    `;
    return;
  }

  let html = `<div class="space-y-3">`;
  changesets.forEach(cs => {
    const statusColors = {
      draft: 'bg-brand-teal-50 dark:bg-indigo-950 text-brand-teal-800 dark:text-indigo-300 border-brand-teal-200 dark:border-indigo-800',
      committed: 'bg-emerald-50 dark:bg-emerald-950 text-emerald-800 dark:text-emerald-300 border-emerald-200 dark:border-emerald-800',
      reverted: 'bg-rose-50 dark:bg-rose-950 text-rose-800 dark:text-rose-300 border-rose-200 dark:border-rose-800',
    };
    const badgeColor = statusColors[cs.status] || 'bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-400 border-slate-200 dark:border-slate-700';

    html += `
      <div class="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 flex items-center justify-between shadow-sm">
        <div>
          <div class="flex items-center space-x-2">
            <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider border ${badgeColor}">
              ${cs.status}
            </span>
            <h4 class="text-sm font-bold text-slate-900 dark:text-white">${cs.title}</h4>
          </div>
          ${cs.description ? `<p class="text-xs text-slate-600 dark:text-slate-400 mt-1">${cs.description}</p>` : ''}
          <div class="flex items-center space-x-4 text-[11px] text-slate-500 dark:text-slate-500 mt-2 font-mono">
            <span>By: ${cs.created_by}</span>
            <span>Operations: ${cs.operations_count || 0}</span>
            ${cs.committed_at ? `<span>Committed: ${cs.committed_at}</span>` : ''}
          </div>
        </div>
        <div class="flex items-center space-x-2">
          ${cs.status === 'draft' ? `
            <button onclick="setActiveChangeset(${cs.id})" class="px-3 py-1.5 bg-brand-teal-600 hover:bg-brand-teal-500 text-white rounded text-xs font-medium shadow-xs transition">
              Set Active
            </button>
          ` : ''}
          ${cs.status === 'committed' && currentRole === 'Admin' ? `
            <button onclick="revertChangeset(${cs.id})" class="px-3 py-1.5 bg-rose-600 hover:bg-rose-500 text-white rounded text-xs font-medium transition">
              Revert (Admin)
            </button>
          ` : ''}
        </div>
      </div>
    `;
  });
  html += `</div>`;
  content.innerHTML = html;
}

function openNewChangesetModal() {
  document.getElementById('new-changeset-modal').classList.remove('hidden');
}

function closeNewChangesetModal() {
  document.getElementById('new-changeset-modal').classList.add('hidden');
}

async function submitNewChangeset() {
  const title = document.getElementById('new-cs-title').value.trim();
  const desc = document.getElementById('new-cs-desc').value.trim();
  if (!title) return alert('Please provide a changeset title');

  try {
    const res = await fetchWithAuth('/api/v1/changesets', {
      method: 'POST',
      body: JSON.stringify({ title, description: desc }),
    });
    if (!res.ok) throw new Error(await res.text());
    activeChangeset = await res.json();
    renderActiveChangesetPill();
    closeNewChangesetModal();
    loadChangesets();
  } catch (e) {
    alert(`Failed to create changeset: ${e.message}`);
  }
}

async function setActiveChangeset(changesetId) {
  try {
    const res = await fetchWithAuth(`/api/v1/changesets/${changesetId}`);
    activeChangeset = await res.json();
    renderActiveChangesetPill();
    loadChangesets();
  } catch (e) {
    alert(`Failed to activate changeset: ${e.message}`);
  }
}

async function openChangesetDrawer() {
  if (!activeChangeset) return;
  const drawer = document.getElementById('changeset-drawer');
  document.getElementById('drawer-cs-title').innerText = activeChangeset.title;

  try {
    const res = await fetchWithAuth(`/api/v1/changesets/${activeChangeset.id}`);
    const data = await res.json();
    activeChangeset = data;
    renderDrawerOps(data.operations || []);
    drawer.classList.remove('hidden');
  } catch (e) {
    alert(`Failed to fetch changeset operations: ${e.message}`);
  }
}

function closeChangesetDrawer() {
  document.getElementById('changeset-drawer').classList.add('hidden');
}

function renderDrawerOps(ops) {
  const list = document.getElementById('drawer-ops-list');
  if (ops.length === 0) {
    list.innerHTML = `<div class="text-slate-500 text-center py-6">No operations staged yet.</div>`;
    return;
  }

  let html = '';
  ops.forEach(op => {
    html += `
      <div class="p-3 bg-slate-50 dark:bg-slate-900 rounded-lg border border-slate-200 dark:border-slate-700/80 space-y-1">
        <div class="flex items-center justify-between">
          <span class="font-bold text-brand-teal-700 dark:text-sky-400 uppercase text-[10px]">${op.operation_type}</span>
          <button onclick="discardOperation(${op.id})" class="text-slate-400 hover:text-rose-600 dark:hover:text-rose-400 text-xs font-medium">Discard</button>
        </div>
        <div class="font-mono text-slate-800 dark:text-slate-300 text-[11px]">${op.entity_type} ID: ${op.entity_id}</div>
        ${op.notes ? `<div class="text-slate-500 dark:text-slate-400 text-[10px]">${op.notes}</div>` : ''}
      </div>
    `;
  });
  list.innerHTML = html;
}

async function discardOperation(operationId) {
  if (!activeChangeset) return;
  try {
    await fetchWithAuth(`/api/v1/changesets/${activeChangeset.id}/operations/${operationId}`, {
      method: 'DELETE',
    });
    openChangesetDrawer();
  } catch (e) {
    alert(`Failed to discard operation: ${e.message}`);
  }
}

async function commitActiveChangeset() {
  if (!activeChangeset) return;
  if (!confirm(`Are you sure you want to commit changeset "${activeChangeset.title}"? This will atomically update the live database and pin all decisions.`)) {
    return;
  }

  try {
    const res = await fetchWithAuth(`/api/v1/changesets/${activeChangeset.id}/commit`, {
      method: 'POST',
    });
    if (!res.ok) throw new Error(await res.text());
    alert('Changeset committed successfully!');
    activeChangeset = null;
    renderActiveChangesetPill();
    closeChangesetDrawer();
    loadSystemReadiness();
    if (currentView === 'changesets') loadChangesets();
  } catch (e) {
    alert(`Commit failed: ${e.message}`);
  }
}

async function revertChangeset(changesetId) {
  if (!confirm('Are you sure you want to revert this committed changeset? This will roll back operational tables and release pins.')) {
    return;
  }
  try {
    const res = await fetchWithAuth(`/api/v1/changesets/${changesetId}/revert`, {
      method: 'POST',
    });
    if (!res.ok) throw new Error(await res.text());
    alert('Changeset reverted.');
    loadChangesets();
    loadSystemReadiness();
  } catch (e) {
    alert(`Revert failed: ${e.message}`);
  }
}

// Staging Actions
async function stageElectCanonical(workId) {
  if (!activeChangeset) {
    alert('Please create or activate a Review Changeset first!');
    switchView('changesets');
    return;
  }

  try {
    await fetchWithAuth(`/api/v1/changesets/${activeChangeset.id}/operations`, {
      method: 'POST',
      body: JSON.stringify({
        operation_type: 'work_elect_canonical',
        entity_type: 'work',
        entity_id: String(workId),
        notes: 'Manually designated as canonical work via graph viewer',
      }),
    });
    alert(`Staged "Elect Canonical Work (${workId})" in ${activeChangeset.title}`);
    activeChangeset.operations_count = (activeChangeset.operations_count || 0) + 1;
    renderActiveChangesetPill();
  } catch (e) {
    alert(`Failed to stage operation: ${e.message}`);
  }
}

async function promptRelinkRecord(recordId) {
  if (!activeChangeset) {
    alert('Please create or activate a Review Changeset first!');
    switchView('changesets');
    return;
  }

  const newWorkUri = prompt(`Enter new authority Work URI or QID for target record ${recordId}:`);
  if (!newWorkUri) return;

  try {
    await fetchWithAuth(`/api/v1/changesets/${activeChangeset.id}/operations`, {
      method: 'POST',
      body: JSON.stringify({
        operation_type: 'reconciliation_relink',
        entity_type: 'corpus_linkage',
        entity_id: String(recordId),
        parameters: { work_uri: newWorkUri },
        notes: `Relinked to ${newWorkUri}`,
      }),
    });
    alert(`Staged relink in ${activeChangeset.title}`);
    activeChangeset.operations_count = (activeChangeset.operations_count || 0) + 1;
    renderActiveChangesetPill();
  } catch (e) {
    alert(`Failed to stage relink: ${e.message}`);
  }
}

// ---------------------------------------------------------------------------
// Pipeline Drawer & SSE Telemetry
// ---------------------------------------------------------------------------

function openPipelineDrawer() {
  document.getElementById('pipeline-drawer').classList.remove('hidden');
}

function closePipelineDrawer() {
  document.getElementById('pipeline-drawer').classList.add('hidden');
}

async function triggerTask(taskType) {
  if (currentRole !== 'Admin') {
    return alert('Administrative privileges required to trigger pipeline tasks.');
  }

  try {
    const res = await fetchWithAuth(`/api/v1/pipeline/run/${taskType}`, {
      method: 'POST',
    });
    if (!res.ok) throw new Error(await res.text());
    alert(`Task '${taskType}' triggered in background!`);
  } catch (e) {
    alert(`Failed to trigger task: ${e.message}`);
  }
}

function setupSSE() {
  const eventSource = new EventSource('/api/v1/pipeline/stream');
  eventSource.onmessage = function(event) {
    try {
      const tasks = JSON.parse(event.data);
      renderPipelineTasks(tasks);
    } catch (e) {
      console.debug('Error parsing SSE tasks:', e);
    }
  };
}

function renderPipelineTasks(tasks) {
  const list = document.getElementById('pipeline-tasks-list');
  if (!tasks || tasks.length === 0) {
    list.innerHTML = `<div class="text-slate-500 text-center py-4">No active pipeline tasks.</div>`;
    return;
  }

  let html = '';
  tasks.slice(0, 5).forEach(t => {
    const isRunning = t.status === 'in_progress';
    const badgeColor = isRunning ? 'text-brand-teal-700 dark:text-sky-400 bg-brand-teal-50 dark:bg-sky-950 border-brand-teal-200 dark:border-sky-800' : 'text-slate-600 dark:text-slate-400 bg-slate-100 dark:bg-slate-900 border-slate-200 dark:border-slate-700';
    html += `
      <div class="p-3 bg-slate-50 dark:bg-slate-900 rounded-lg border border-slate-200 dark:border-slate-700 space-y-1">
        <div class="flex items-center justify-between">
          <span class="font-bold text-slate-900 dark:text-white">${t.task_type}</span>
          <span class="px-1.5 py-0.5 rounded text-[10px] font-mono border ${badgeColor}">${t.status}</span>
        </div>
        <div class="text-[11px] text-slate-500 dark:text-slate-400">Stage: ${t.current_stage}</div>
        <div class="w-full bg-slate-200 dark:bg-slate-800 rounded-full h-1.5 mt-1 overflow-hidden">
          <div class="bg-brand-teal-600 dark:bg-sky-500 h-1.5 rounded-full" style="width: ${Math.round(t.percentage || 0)}%"></div>
        </div>
      </div>
    `;
  });
  list.innerHTML = html;
}

