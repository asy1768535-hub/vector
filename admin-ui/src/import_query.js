export function resolveImportEntry(query, libraries, currentSlug = null) {
    const allowed = new Set((libraries || []).map((library) => library.slug));
    const requested = typeof query?.library === 'string' ? query.library : '';
    const slug = allowed.has(requested)
        ? requested
        : allowed.has(currentSlug)
            ? currentSlug
            : (libraries?.[0]?.slug ?? null);
    const mode = query?.mode === 'replace' ? 'replace' : 'add';
    return { slug, mode };
}
