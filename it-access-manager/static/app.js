function switchTab(tabId) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelector(`[data-tab="${tabId}"]`).classList.add('active');
    document.getElementById(tabId).classList.add('active');

    if (tabId === 'tab-slack')  loadSlackUsers();
    if (tabId === 'tab-audit')  loadAuditLogs();
    if (tabId === 'tab-review') loadAccessReview();
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

function statusBadge(status) {
    const s = (status || 'unknown').toLowerCase();
    let cls = 'badge-unknown';
    if (s === 'active')      cls = 'badge-active';
    if (s === 'suspended' || s === 'deactivated') cls = 'badge-suspended';
    return `<span class="badge ${cls}">${s}</span>`;
}

// ---------------------------------------------------------------------------
// User Search
// ---------------------------------------------------------------------------
let lastSearchResult = null;

async function searchUser() {
    const email = document.getElementById('search-email').value.trim();
    if (!email) { showToast('Please enter an email address.', 'error'); return; }

    const btn = document.getElementById('btn-search');
    const results = document.getElementById('search-results');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Searching…';
    results.innerHTML = '<div class="loading-overlay">Looking up user across services…</div>';

    try {
        const resp = await fetch(`/api/search?email=${encodeURIComponent(email)}`);
        if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
        const data = await resp.json();
        lastSearchResult = data;
        renderSearchResults(data);
    } catch (err) {
        results.innerHTML = '';
        showToast(`Search failed: ${err.message}`, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = 'Search';
    }
}

function renderSearchResults(data) {
    const el = document.getElementById('search-results');
    const jc = data.jumpcloud;
    const sl = data.slack;

    if (!jc && !sl) {
        el.innerHTML = `
            <div class="empty-state">
                <div class="icon">&#128269;</div>
                <p>No user found for <strong>${data.email}</strong> in any connected service.</p>
            </div>`;
        return;
    }

    let html = '<div class="result-grid">';

    html += '<div class="result-card">';
    html += '<h3>JumpCloud</h3>';
    if (jc) {
        html += field('Name', jc.name);
        html += field('Email', jc.email);
        html += field('Status', statusBadge(jc.status));
    } else {
        html += '<p style="color:var(--text-dim);font-size:13px;">User not found in JumpCloud.</p>';
    }
    html += '</div>';

    html += '<div class="result-card">';
    html += '<h3>Slack</h3>';
    if (sl) {
        html += field('Name', sl.name);
        html += field('Email', sl.email);
        html += field('Status', statusBadge(sl.status));
        html += field('Role', sl.role);
    } else {
        html += '<p style="color:var(--text-dim);font-size:13px;">User not found in Slack.</p>';
    }
    html += '</div>';
    html += '</div>';

    if (data.errors && data.errors.length) {
        html += '<div style="margin-bottom:16px;">';
        data.errors.forEach(e => {
            html += `<p style="color:var(--warning);font-size:13px;">Warning: ${e}</p>`;
        });
        html += '</div>';
    }

    const isActive = (jc && jc.status === 'active') || (sl && sl.status === 'active');
    if (isActive) {
        html += `
            <div class="deactivate-section">
                <p>Deactivate this user across all connected services?</p>
                <button class="btn btn-danger" id="btn-deactivate" onclick="deactivateUser()">
                    Deactivate User
                </button>
            </div>`;
    }

    el.innerHTML = html;
}

function field(label, value) {
    return `<div class="field"><span class="label">${label}:</span> ${value}</div>`;
}

// ---------------------------------------------------------------------------
// Deactivate User
// ---------------------------------------------------------------------------
async function deactivateUser() {
    if (!lastSearchResult) return;
    const email = lastSearchResult.email;
    if (!confirm(`Are you sure you want to deactivate ${email}? This will disable the user in JumpCloud and Slack.`)) return;

    const btn = document.getElementById('btn-deactivate');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Deactivating…';

    try {
        const resp = await fetch(`/api/deactivate?email=${encodeURIComponent(email)}`, { method: 'POST' });
        if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
        const data = await resp.json();

        if (data.results.jumpcloud) showToast('JumpCloud: user suspended.', 'success');
        else showToast('JumpCloud: no change.', 'info');

        if (data.results.slack) showToast('Slack: user deactivated.', 'success');
        else showToast('Slack: no change.', 'info');

        if (data.errors.length) data.errors.forEach(e => showToast(e, 'error'));

        await searchUser();
    } catch (err) {
        showToast(`Deactivation failed: ${err.message}`, 'error');
        btn.disabled = false;
        btn.innerHTML = 'Deactivate User';
    }
}

// ---------------------------------------------------------------------------
// Slack Users Table
// ---------------------------------------------------------------------------
let slackUsersLoaded = false;

async function loadSlackUsers() {
    if (slackUsersLoaded) return;
    const el = document.getElementById('slack-users-body');
    el.innerHTML = '<tr><td colspan="5" class="loading-overlay">Loading Slack users…</td></tr>';

    try {
        const resp = await fetch('/api/slack/users');
        if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
        const data = await resp.json();

        if (!data.users.length) {
            el.innerHTML = '<tr><td colspan="5" class="empty-state">No Slack users found.</td></tr>';
            return;
        }

        el.innerHTML = data.users.map(u => `
            <tr>
                <td>${u.name}</td>
                <td>${u.email}</td>
                <td>${statusBadge(u.status)}</td>
                <td>${u.role}</td>
                <td>${u.id}</td>
            </tr>`).join('');
        slackUsersLoaded = true;
    } catch (err) {
        el.innerHTML = `<tr><td colspan="5" style="color:var(--danger);">Failed to load: ${err.message}</td></tr>`;
    }
}

// ---------------------------------------------------------------------------
// Audit Logs
// ---------------------------------------------------------------------------
let auditLogsLoaded = false;

async function loadAuditLogs() {
    if (auditLogsLoaded) return;
    const el = document.getElementById('audit-logs-body');
    el.innerHTML = '<tr><td colspan="5" class="loading-overlay">Loading audit logs…</td></tr>';

    try {
        const resp = await fetch('/api/audit-logs');
        if (!resp.ok) throw new Error('Failed to load audit logs');
        const data = await resp.json();

        if (!data.length) {
            el.innerHTML = '<tr><td colspan="5" class="empty-state">No audit logs yet.</td></tr>';
            return;
        }

        el.innerHTML = data.map(l => `
            <tr>
                <td>${new Date(l.timestamp).toLocaleString()}</td>
                <td>${l.performed_by}</td>
                <td><span class="badge badge-suspended">${l.action}</span></td>
                <td>${l.target_email}</td>
                <td style="font-size:12px;">${l.details || '—'}</td>
            </tr>`).join('');
        auditLogsLoaded = true;
    } catch (err) {
        el.innerHTML = `<tr><td colspan="5" style="color:var(--danger);">Failed to load: ${err.message}</td></tr>`;
    }
}

// ---------------------------------------------------------------------------
// Access Review
// ---------------------------------------------------------------------------
let accessReviewLoaded = false;

async function loadAccessReview() {
    if (accessReviewLoaded) return;
    const el = document.getElementById('access-review-body');
    el.innerHTML = '<tr><td colspan="5" class="loading-overlay">Loading access data…</td></tr>';

    try {
        const resp = await fetch('/api/users');
        if (!resp.ok) throw new Error('Failed to load users');
        const data = await resp.json();

        if (!data.length) {
            el.innerHTML = '<tr><td colspan="5" class="empty-state">No users in database yet. Search for a user first.</td></tr>';
            return;
        }

        el.innerHTML = data.map(u => `
            <tr>
                <td>${u.name || '—'}</td>
                <td>${u.email}</td>
                <td>${statusBadge(u.status)}</td>
                <td>${u.source}</td>
                <td>${u.updated_at ? new Date(u.updated_at).toLocaleString() : '—'}</td>
            </tr>`).join('');
        accessReviewLoaded = true;
    } catch (err) {
        el.innerHTML = `<tr><td colspan="5" style="color:var(--danger);">Failed to load: ${err.message}</td></tr>`;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('search-email').addEventListener('keydown', e => {
        if (e.key === 'Enter') searchUser();
    });
});
