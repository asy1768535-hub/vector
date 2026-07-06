const ALLOWED_TAGS = ['h1','h2','h3','h4','h5','h6','p','br','strong','em','del','a',
    'ul','ol','li','table','thead','tbody','tr','th','td','blockquote','pre','code','hr',
    'sup','sub','span'];

const ALLOWED_ATTR = ['href','title','target','rel'];

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


function injectCitationBadges(html, sources) {
    const tags = [];
    return String(html).split(/(<[^>]+>)/g).map((token) => {
        if (!token) return '';
        if (token.startsWith('<')) {
            const close = token.match(/^<\s*\/\s*([a-z0-9-]+)/i);
            if (close) {
                const name = close[1].toLowerCase();
                const idx = tags.lastIndexOf(name);
                if (idx >= 0) tags.splice(idx, 1);
                return token;
            }
            const open = token.match(/^<\s*([a-z0-9-]+)/i);
            if (open && !/\/\s*>$/.test(token)) {
                tags.push(open[1].toLowerCase());
            }
            return token;
        }
        if (tags.some((name) => name === 'a' || name === 'code' || name === 'pre')) {
            return token;
        }
        return replaceCitations(token, sources);
    }).join('');
}

export function renderAssistantMarkdown(text, sources = [], { marked, DOMPurify }) {
    if (!text) return '';
    const sourceList = Array.isArray(sources) ? sources : [];
    const raw = marked.parse(String(text), { breaks: true, gfm: true });
    const sanitized = DOMPurify.sanitize(raw, {
        ALLOWED_TAGS,
        ALLOWED_ATTR,
        ALLOW_DATA_ATTR: false,
    });
    return injectCitationBadges(sanitized, sourceList);
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
    const rawRanges = Array.isArray(source?.source_ranges) ? source.source_ranges : [];
    const ranges = rawRanges
        .map((range) => ({
            start: Number(range?.start ?? windowStart) - windowStart,
            end: Number(range?.end ?? windowStart) - windowStart,
        }))
        .filter((range) => Number.isFinite(range.start) && Number.isFinite(range.end))
        .map((range) => ({
            start: Math.max(0, Math.min(text.length, range.start)),
            end: Math.max(0, Math.min(text.length, range.end)),
        }))
        .filter((range) => range.end > range.start)
        .sort((a, b) => a.start - b.start);
    if (ranges.length) {
        const segments = [];
        let cursor = 0;
        for (const range of ranges) {
            const start = Math.max(cursor, range.start);
            const end = Math.max(start, range.end);
            if (start > cursor) segments.push({ text: text.slice(cursor, start), highlight: false });
            if (end > start) segments.push({ text: text.slice(start, end), highlight: true });
            cursor = end;
        }
        if (cursor < text.length) segments.push({ text: text.slice(cursor), highlight: false });
        return {
            before: text.slice(0, ranges[0].start),
            match: text.slice(ranges[0].start, ranges[0].end),
            after: text.slice(ranges[0].end),
            segments,
        };
    }
    const start = Number(source?.source_start ?? windowStart) - windowStart;
    const end = Number(source?.source_end ?? windowStart) - windowStart;
    const safeStart = Math.max(0, Math.min(text.length, start));
    const safeEnd = Math.max(safeStart, Math.min(text.length, end));
    return {
        before: text.slice(0, safeStart),
        match: text.slice(safeStart, safeEnd),
        after: text.slice(safeEnd),
        segments: [
            { text: text.slice(0, safeStart), highlight: false },
            { text: text.slice(safeStart, safeEnd), highlight: true },
            { text: text.slice(safeEnd), highlight: false },
        ].filter((part) => part.text),
    };
}
