export function beginGraphOverviewLoad(overview, { scopeKey, loadedScopeKey, hasScope }) {
    overview.loading = false;
    overview.error = null;
    overview.partialError = null;
    if (!hasScope) {
        overview.data = null;
        return '';
    }
    overview.loading = true;
    return loadedScopeKey;
}

export function failGraphOverviewLoad(overview, error) {
    overview.error = error;
    overview.loading = false;
}
