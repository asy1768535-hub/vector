const ALLOWED_TAGS = ['h1','h2','h3','h4','h5','h6','p','br','strong','em','del','a',
    'ul','ol','li','table','thead','tbody','tr','th','td','blockquote','pre','code','hr',
    'sup','sub','span','button'];

const ALLOWED_ATTR = ['href','title','target','rel','class','type','data-citation-index','aria-label'];

function protectedRanges(text) {
    const ranges = [];
    const patterns = [/```[\s\S]*?```/g, /`[^`\n]*`/g, /\[[^\n]+\]\([^)]+\)/g, /https?:\/\/[^\s)]+/g];
    for (const pattern of patterns) {
        for (const match of text.matchAll(pattern)) {
            ranges.push([match.index, match.index + match[0].length]);
        }
    }
    return ranges;
}

function isProtected(index, ranges) {
    return ranges.some(([start, end]) => index >= start && index < end);
}

function citationBadge(n) {
    const index = n - 1;
    return `<sup><button type="button" class="chat-citation-badge" data-citation-index="${index}" aria-label="查看引用 ${n}">${n}</button></sup>`;
}

function replaceCitations(text, sources) {
    const ranges = protectedRanges(text);
    return text.replace(/\[(\d+)\]/g, (match, rawIndex, offset) => {
        const n = Number(rawIndex);
        if (!Number.isInteger(n) || n < 1 || n > sources.length || isProtected(offset, ranges)) {
            return match;
        }
        return citationBadge(n);
    });
}

export function renderAssistantMarkdown(text, sources = [], { marked, DOMPurify }) {
    if (!text) return '';
    const withCitations = replaceCitations(String(text), Array.isArray(sources) ? sources : []);
    const raw = marked.parse(withCitations, { breaks: true, gfm: true });
    return DOMPurify.sanitize(raw, {
        ALLOWED_TAGS,
        ALLOWED_ATTR,
        ALLOW_DATA_ATTR: false,
    });
}

export function extractCitationIndex(el) {
    const target = el && typeof el.closest === 'function' ? el.closest('.chat-citation-badge') : null;
    if (!target) return null;
    const index = Number(target.getAttribute('data-citation-index'));
    return Number.isInteger(index) && index >= 0 ? index : null;
}

export function highlightSourceWindow(source) {
    const text = String(source?.text_window || '');
    const windowStart = Number(source?.window_start ?? 0);
    const start = Number(source?.source_start ?? windowStart) - windowStart;
    const end = Number(source?.source_end ?? windowStart) - windowStart;
    const safeStart = Math.max(0, Math.min(text.length, start));
    const safeEnd = Math.max(safeStart, Math.min(text.length, end));
    return {
        before: text.slice(0, safeStart),
        match: text.slice(safeStart, safeEnd),
        after: text.slice(safeEnd),
    };
}
