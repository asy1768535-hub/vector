"""Patch only the batch-retry request headers in an existing release image."""

from pathlib import Path


target = Path("/app/admin-ui/src/api.js")
source = target.read_text(encoding="utf-8")
old = """export const retryMonitoredTasks = (items) => request('/admin/jobs/monitor/retry', {
    method: 'POST',
    body: JSON.stringify({ items }),
});
"""
new = """export const retryMonitoredTasks = (items) => request(
    '/admin/jobs/monitor/retry',
    jsonBody('POST', { items }),
);
"""
if source.count(old) != 1:
    raise SystemExit("expected monitored-retry request block exactly once")
if "function jsonBody(method, body)" not in source:
    raise SystemExit("expected JSON request helper")
target.write_text(source.replace(old, new), encoding="utf-8")
