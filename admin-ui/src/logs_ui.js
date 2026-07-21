// ── 共享日志工具：分页、CSV 导出、筛选 ──

import { paginate as paginateBase } from './common_ui.js';

/** 前端分页 */
export function paginate(items, page, pageSize) {
    return paginateBase(items, page, pageSize);
}

/** 判断某个 ISO 时间戳是否落在 [from, to] 区间 */
export function inDateRange(iso, from, to) {
    if (!iso) return !from && !to;
    const d = iso.slice(0, 10);
    if (from && d < from) return false;
    if (to && d > to) return false;
    return true;
}

// ── CSV ──

export function csvEscape(val) {
    if (val == null) return '';
    const s = String(val);
    const safe = /^[=+\-@]/.test(s) ? "'" + s : s;
    if (/[",\n\r]/.test(safe)) return '"' + safe.replace(/"/g, '""') + '"';
    return safe;
}

export function downloadCSV(filename, headers, rows) {
    const BOM = '﻿';
    const lines = [BOM + headers.map(csvEscape).join(',')];
    for (const row of rows) lines.push(row.map(csvEscape).join(','));
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
}

// ── 审计日志筛选 ──

import { targetTypeKey } from './admin_activity_ui.js';

export function filterAuditLogs(logs, f) {
    let list = logs;
    // 统一时间范围
    if (f.range && f.range.length === 2 && f.range[0] && f.range[1]) {
        const from = f.range[0].slice(0, 10);
        const to = f.range[1].slice(0, 10);
        list = list.filter((r) => inDateRange(r.at, from, to));
    }
    if (f.action) list = list.filter((r) => r.action === f.action);
    if (f.targetType) list = list.filter((r) => targetTypeKey(r.action) === f.targetType);
    if (f.actor) {
        const a = f.actor.trim().toLowerCase();
        list = list.filter((r) => (r.actor_user_id || '').toLowerCase().includes(a));
    }
    if (f.keyword) {
        const kw = f.keyword.trim().toLowerCase();
        list = list.filter((r) =>
            (r.action || '').toLowerCase().includes(kw)
            || (r.actor_user_id || '').toLowerCase().includes(kw)
            || JSON.stringify(r.target || '').toLowerCase().includes(kw)
        );
    }
    return list;
}

// ── 问答日志筛选 ──

export function filterChatLogs(rows, f) {
    let list = rows;
    if (f.keyword) {
        const kw = f.keyword.trim().toLowerCase();
        list = list.filter((r) => (r.question || '').toLowerCase().includes(kw));
    }
    if (f.rewrite === 'yes') list = list.filter((r) => !!r.rewritten_query);
    else if (f.rewrite === 'no') list = list.filter((r) => !r.rewritten_query);
    if (f.hasSources === 'yes') list = list.filter((r) => r.sources && r.sources.length > 0);
    else if (f.hasSources === 'no') list = list.filter((r) => !r.sources || !r.sources.length);
    return list;
}
