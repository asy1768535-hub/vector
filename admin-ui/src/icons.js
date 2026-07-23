// 本地 SVG 图标集 — 不依赖任何公网 CDN/API。
// 注册自定义元素 <local-icon icon="prefix:name">，行为兼容 iconify-icon 的基础用法。
// 支持 style、title、class 属性。

const ICONS = {
    // ── Material Design Icons (mdi) ──
    'mdi:archive-arrow-down-outline':
        '<path fill="currentColor" d="M20 21H4V10h2v9h12v-9h2v11M3 3h18v6H3V3m2 2v2h14V5H5m5 8h4v-3l4 4-4 4v-3h-4v-2Z"/>',
    'mdi:trash-can-outline':
        '<path fill="currentColor" d="M9 3v1H4v2h1v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6h1V4h-5V3H9m0 5h2v9H9V8m4 0h2v9h-2V8Z"/>',
    'mdi:email-outline':
        '<path fill="currentColor" d="M22 6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6m-2 0l-8 5-8-5h16m0 12H4V8l8 5 8-5v10Z"/>',
    'mdi:lock-outline':
        '<path fill="currentColor" d="M12 17a2 2 0 0 0 2-2 2 2 0 0 0-2-2 2 2 0 0 0-2 2 2 2 0 0 0 2 2m6-9h-1V6a5 5 0 0 0-10 0v2H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2M8 6a4 4 0 0 1 8 0v2H8V6m11 14H5V10h14v10Z"/>',
    'mdi:view-dashboard-outline':
        '<path fill="currentColor" d="M19 5v2h-4V5h4M9 5v6H5V5h4m10 8v6h-4v-6h4M9 13v6H5v-6h4m12-10h-6v6h6V3m-8 0H3v10h10V3m8 14h-6v6h6v-6m-8 0H3v6h10v-6Z"/>',
    'mdi:file-document-outline':
        '<path fill="currentColor" d="M6 2h8l6 6v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2m0 2v16h12V9h-5V4H6m2 4h4v2H8v-2m0 4h8v2H8v-2m0 4h5v2H8v-2Z"/>',
    'mdi:text-search':
        '<path fill="currentColor" d="M19.31 18.9l3.08 3.1L21 23.39l-3.12-3.07A6.99 6.99 0 0 1 5 16a7 7 0 0 1 9.12-6.74A7.97 7.97 0 0 0 6 16a6 6 0 0 0 10.91 3.5H20v1.4h-1.57l.88.88M16 6a6 6 0 1 0 0 12A6 6 0 0 0 16 6m0 2a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z"/>',
    'mdi:chat-question-outline':
        '<path fill="currentColor" d="M12 3C6.5 3 2 6.58 2 11a7.2 7.2 0 0 0 2.75 5.5c0 .6-.42 1.5-.92 2.1l-.31.4c-.11.13-.22.28-.29.41-.08.13-.16.28-.19.44-.04.16-.04.33 0 .5.04.16.14.3.26.41.11.12.25.2.4.25.15.04.32.06.49.02.2-.04.4-.14.55-.27.17-.13.32-.28.45-.45.13-.18.24-.38.33-.58l.05-.12c.05-.14.1-.28.13-.43.58.25 1.21.38 1.86.38 1.25 0 2.42-.38 3.38-1.04l2.7 1.35.45-1.69C20.88 15.14 22 13.18 22 11c0-4.42-4.5-8-10-8m0 2c4.42 0 8 3.13 8 6a5.8 5.8 0 0 1-2.09 4.36l-.3.22.36 1.37-1.92-.96-.33.17A5.86 5.86 0 0 1 12 17c-1.05 0-2.04-.28-2.89-.76L8.5 16l-.61.24c-.02.04-.04.08-.07.12a1.2 1.2 0 0 1-.18.2c-.05.04-.09.06-.12.07h-.02c.12-.28.23-.6.23-.98v-.51l-.3-.23A5.99 5.99 0 0 1 4 11c0-2.87 3.58-6 8-6m-1 5v2h2v-2h-2m0 3v2h2v-2h-2Z"/>',
    'mdi:database-import-outline':
        '<path fill="currentColor" d="M12 3C8.59 3 5.69 4.07 4.53 5.23 3.37 6.39 3 7.62 3 9s.37 2.61 1.53 3.77S8.59 15 12 15s6.31-1.07 7.47-2.23S21 10.38 21 9s-.37-2.61-1.53-3.77S15.41 3 12 3m0 2c3.01 0 5.54.88 6.72 1.78C19.46 7.34 17.86 8 15.5 8c-.17 0-.33 0-.5-.03V6.5L9 10l6 3.5v-1.53c.33.02.66.03 1 .03 2.22 0 3.74-.58 4.5-1.22C19.46 11.66 17.01 13 12 13s-7.46-1.34-8.5-2.22C3.54 11.66 4.78 13 7 13v2c-2.34 0-4-.96-4-2v3c0 1.38.37 2.61 1.53 3.77S8.59 21 12 21s6.31-1.07 7.47-2.23S21 17.38 21 16v-3c0 .88-.78 1.64-2 2.22V17c0 .34-.16.78-.78 1.28C17.54 18.82 16.07 19 12 19s-5.54-.18-6.22-.72C5.16 17.78 5 17.34 5 17v-1.16c.99.64 2.71 1.16 5 1.16v-2c-2.34 0-4-.96-4-2v-3c0 .88.54 1.64 1.5 2.22S8.59 12 12 12Z"/>',
    'mdi:key-variant':
        '<path fill="currentColor" d="M22 19h-6v-4h-2.68A6.98 6.98 0 0 1 6 20a7 7 0 0 1-7-7 7 7 0 0 1 7-7c3 0 5.56 1.92 6.58 4.5H22v6M6 9a4 4 0 0 0 0 8 4 4 0 0 0 0-8m0 2a2 2 0 1 1 0 4 2 2 0 0 1 0-4Z"/>',
    'mdi:account-group-outline':
        '<path fill="currentColor" d="M12 5a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7m0 2a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3m-5 3.5A2.5 2.5 0 0 1 9.5 8 2.5 2.5 0 0 1 12 10.5a2.5 2.5 0 0 1-2.5 2.5A2.5 2.5 0 0 1 7 10.5M19 13a2 2 0 0 1-2-2 2 2 0 0 1 2-2 2 2 0 0 1 2 2 2 2 0 0 1-2 2m-7 1c2.25 0 6 1.12 6 3.33V20H6v-2.67C6 15.12 9.75 14 12 14m0 2c-2.01 0-4 .9-4 2h8c0-1.1-1.99-2-4-2Z"/>',
    'mdi:bookshelf':
        '<path fill="currentColor" d="M9 3v15h3V3H9m3 2 4 13 3-1-4-13-3 1M5 5v13h3V5H5M3 19v2h18v-2H3Z"/>',
    'mdi:shield-key-outline':
        '<path fill="currentColor" d="M21 11c0 5.55-3.84 10.74-9 12-5.16-1.26-9-6.45-9-12V5l9-4 9 4v6m-9 10c3.75-1 7-5.46 7-9.78V6.3l-7-3.12L5 6.3v4.92C5 15.54 8.25 20 12 21m0-4a3 3 0 0 0 3-3 3 3 0 0 0-3-3 3 3 0 0 0-3 3 3 3 0 0 0 3 3m0-2a1 1 0 0 1-1-1 1 1 0 0 1 1-1 1 1 0 0 1 1 1 1 1 0 0 1-1 1Z"/>',
    'mdi:cog-sync-outline':
        '<path fill="currentColor" d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8m0 2a2 2 0 1 1 0 4 2 2 0 0 1 0-4m-1-7.35 2.83.97.36-2.78 2.45.78-.36 2.78 2.97.48.56 2.75-2.97-.48 1.4 2.41-.82.46 2.18 1.85-.85-.48-2.17-1.84.35-2.79-2.45-.78.35 2.79L12 1Z"/>',
    'mdi:heart-pulse':
        '<path fill="currentColor" d="M18 8.17c.82.6 2 1.53 2 3.83h-3.17L16 13.88l-.71-.71L12 9.88l-3.29 3.29-.71.71L5.17 12H4c0-2.3 1.18-3.23 2-3.83V3h12v5.17M12 2l2.83 2.83L12 7.66 9.17 4.83 12 2M8 14h2.59L12 15.41 13.41 14H16v-2h-2.59L12 10.59 10.59 12H8v2Z"/>',
    'mdi:history':
        '<path fill="currentColor" d="M13.5 8H12v5l4.28 2.54.72-1.21-3.5-2.08V8M13 3a9 9 0 0 0-9 9H1l3.96 4.03L9 12H6a7 7 0 0 1 7-7 7 7 0 0 1 7 7 7 7 0 0 1-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42A8.9 8.9 0 0 0 13 21a9 9 0 0 0 9-9 9 9 0 0 0-9-9Z"/>',
    'mdi:comment-text-multiple-outline':
        '<path fill="currentColor" d="M12 23a1 1 0 0 1-1-1v-2h7a2 2 0 0 0 2-2V7h2a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-4v1l-4-2-2 2M8 3h12a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2m0 2v11h12V5H8m2 2h8v2h-8V7m0 4h5v2h-5v-2Z"/>',
    'mdi:chevron-down':
        '<path fill="currentColor" d="M7.41 8.58 12 13.17l4.59-4.59L18 10l-6 6-6-6 1.41-1.42Z"/>',
    'mdi:lock-reset':
        '<path fill="currentColor" d="M12.63 2c5.53 0 10.01 4.5 10.01 10s-4.48 10-10.01 10c-3.52 0-6.59-1.84-8.38-4.6l1.72-1.02a8 8 0 0 0 6.66 3.62c4.43 0 8-3.59 8-8s-3.57-8-8-8a7.98 7.98 0 0 0-7.16 4.56L7.5 9.5H1.29L1 4.79l2.13 1.69A9.97 9.97 0 0 1 12.63 2M13 7v4.17l2.59 2.59L14.18 15 11 11.83V7h2Z"/>',
    'mdi:logout':
        '<path fill="currentColor" d="M16 17v-3H9v-4h7V7l5 5-5 5M14 2a2 2 0 0 1 2 2v2h-2V4H5v16h9v-2h2v2a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9Z"/>',
    'mdi:menu':
        '<path fill="currentColor" d="M3 6h18v2H3V6m0 5h18v2H3v-2m0 5h18v2H3v-2Z"/>',
    'mdi:backburger':
        '<path fill="currentColor" d="M5 13h10.59l-3.3 3.3 1.42 1.42L19.42 12l-5.71-5.72-1.42 1.42 3.3 3.3H5v2Z"/>',
    'mdi:plus':
        '<path fill="currentColor" d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2Z"/>',
    'mdi:content-copy':
        '<path fill="currentColor" d="M19 21H8a2 2 0 0 1-2-2V8h2v11h11v2m3-5H11a2 2 0 0 1-2-2V3a2 2 0 0 1 2-2h8l5 5v8a2 2 0 0 1-2 2m-3-13v4h4l-4-4Z"/>',
    'mdi:refresh':
        '<path fill="currentColor" d="M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.75 10h-2.1A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35Z"/>',
    'mdi:certificate-outline':
        '<path fill="currentColor" d="M23 12l-2.44-2.79.34-3.69-3.61-.82L15.4 1.5 12 2.96 8.6 1.5 6.71 4.69l-3.61.82.34 3.69L1 12l2.44 2.79-.34 3.69 3.61.82 1.89 3.19L12 21.03l3.4 1.46 1.89-3.19 3.61-.82-.34-3.69L23 12m-12.91 4.72-3.8-3.81 1.48-1.48 2.32 2.33 5.85-5.87 1.48 1.48-7.33 7.35Z"/>',
    'mdi:publish':
        '<path fill="currentColor" d="M9 16v-6H5l7-7 7 7h-4v6H9m-4 4v-2h14v2H5Z"/>',
    'mdi:chevron-left':
        '<path fill="currentColor" d="M15.41 16.58 10.83 12l4.58-4.59L14 6l-6 6 6 6 1.41-1.42Z"/>',
    'mdi:chevron-right':
        '<path fill="currentColor" d="M8.59 16.58 13.17 12 8.59 7.41 10 6l6 6-6 6-1.41-1.42Z"/>',

    // ── Carbon Icons ──
    'carbon:chart-relationship':
        '<path fill="currentColor" d="M26 6a3.996 3.996 0 0 0-3.858 3H17.93A4.98 4.98 0 0 0 14 5a4.99 4.99 0 0 0-3.93 2H6a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h4.07A4.98 4.98 0 0 0 14 17a4.99 4.99 0 0 0 3.93-2h4.21A3.993 3.993 0 1 0 26 16h-4.21a4.97 4.97 0 0 0-5.86-2H6v-4h9.93a4.98 4.98 0 0 0 5.86-2H26V6ZM6 9h4v4H6Zm8 7a3 3 0 1 1 3-3 3.003 3.003 0 0 1-3 3Zm12-7a2 2 0 1 1 2-2 2.002 2.002 0 0 1-2 2Z"/>',

    // ── Sidebar Icons (stroke-based, viewBox 0 0 24 24) ──
    'sidebar:chat':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 5.5h13a2.5 2.5 0 0 1 2.5 2.5v6.6a2.5 2.5 0 0 1-2.5 2.5H11l-4.8 3.1v-3.1h-.7A2.5 2.5 0 0 1 3 14.6V8a2.5 2.5 0 0 1 2.5-2.5z"/><path d="M8 11h.01"/><path d="M12 11h.01"/><path d="M16 11h.01"/></g>',
    'sidebar:document':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3.5h7l4 4v13H7a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2z"/><path d="M14 3.5v4h4"/><path d="M8.5 12h7"/><path d="M8.5 16h5"/></g>',
    'sidebar:search':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6.8 4.5h6.4l3.3 3.3v2.3"/><path d="M13.2 4.5v3.3h3.3"/><path d="M6.8 4.5a1.8 1.8 0 0 0-1.8 1.8v11.4a1.8 1.8 0 0 0 1.8 1.8h4.1"/><path d="M8 11h4.2"/><circle cx="15.2" cy="15.2" r="3.4"/><path d="M17.7 17.7l2.8 2.8"/></g>',
    'sidebar:import':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15.5v2.2A2.3 2.3 0 0 0 6.3 20h11.4a2.3 2.3 0 0 0 2.3-2.3v-2.2"/><path d="M8 10l4-4 4 4"/><path d="M12 6v10"/><path d="M7 15.5h10"/></g>',
    'sidebar:api-key':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="12" r="3.2"/><path d="M11.2 12h8.3"/><path d="M15.5 12v2.2"/><path d="M18 12v1.6"/></g>',
    'sidebar:overview':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="6" height="6" rx="1.5"/><rect x="14" y="4" width="6" height="6" rx="1.5"/><rect x="4" y="14" width="6" height="6" rx="1.5"/><rect x="14" y="14" width="6" height="6" rx="1.5"/></g>',
    'sidebar:user':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="3.2"/><path d="M5.5 20a6.5 6.5 0 0 1 13 0"/></g>',
    'sidebar:library':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5.5" rx="6.5" ry="2.5"/><path d="M5.5 5.5v5c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5v-5"/><path d="M5.5 10.5v5c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5v-5"/><path d="M5.5 15.5v3c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5v-3"/></g>',
    'sidebar:permission':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5 19 6v5.5c0 4.5-2.9 7.7-7 9-4.1-1.3-7-4.5-7-9V6l7-2.5z"/><path d="m8.8 12.2 2.1 2.1 4.4-4.7"/></g>',
    'sidebar:task':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2.2"/><path d="m8 8.5 1 1 2-2"/><path d="M13.5 8.5H17"/><path d="m8 13 1 1 2-2"/><path d="M13.5 13H17"/><circle cx="9" cy="17" r="1"/><path d="M13.5 17H17"/></g>',
    'sidebar:runtime':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="14" height="5.5" rx="1.4"/><rect x="5" y="13.5" width="14" height="5.5" rx="1.4"/><path d="M8 7.75h.01"/><path d="M8 16.25h.01"/><path d="M11 7.75h5"/><path d="M11 16.25h5"/></g>',
    'sidebar:audit':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4.8 12a7.2 7.2 0 1 0 2.1-5.1"/><path d="M4.8 6.2v3.5h3.5"/><path d="M12 8.2v4.1l2.8 1.7"/><path d="M17.5 19.2h2.2"/></g>',
    'sidebar:qa-log':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 6.5h10.5a2.2 2.2 0 0 1 2.2 2.2v6.1a2.2 2.2 0 0 1-2.2 2.2h-5.5l-3.8 2.4V17H6.5a2.2 2.2 0 0 1-2.2-2.2V8.7a2.2 2.2 0 0 1 2.2-2.2z"/><path d="M8 10h7"/><path d="M8 13h5"/><path d="M6.5 6.5V5.2A2.2 2.2 0 0 1 8.7 3h8.2"/></g>',

    // ── Overview (Dashboard) Icons (stroke-based) ──
    'overview:kb-count':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 7.5 12 4l6.5 3.5-6.5 3.5-6.5-3.5z"/><path d="M5.5 12 12 15.5 18.5 12"/><path d="M5.5 16.5 12 20l6.5-3.5"/></g>',
    'overview:pending-jobs':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="7.5"/><path d="M12 7.5v5l3.2 1.8"/><path d="M5.4 5.4 4 4"/><path d="M18.6 5.4 20 4"/></g>',
    'overview:processing-jobs':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 0 1-13.6 5.7"/><path d="M4 12A8 8 0 0 1 17.6 6.3"/><path d="M17.5 3.8v3h-3"/><path d="M6.5 20.2v-3h3"/></g>',
    'overview:failed-jobs':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4.2 20 18.5H4L12 4.2z"/><path d="M12 9v4"/><path d="M12 16h.01"/></g>',
    'overview:online-services':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.8 19 6.2v5.6c0 4.4-2.9 7.4-7 8.6-4.1-1.2-7-4.2-7-8.6V6.2l7-2.4z"/><path d="M9 12.2 11.1 14.3 15.4 9.7"/></g>',
    'overview:service-status':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="5.5" rx="1.4"/><rect x="4" y="13.5" width="16" height="5.5" rx="1.4"/><path d="M7.2 7.75h.01"/><path d="M7.2 16.25h.01"/><path d="M10.2 7.75h6"/><path d="M10.2 16.25h6"/></g>',
    'overview:recent-activity':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 6.5h9"/><path d="M5 12h6"/><path d="M5 17.5h7"/><path d="M16 14.5 18 16.5 21 12.5"/></g>',
    'overview:rebuild':
        '<g stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19.5 12a7.5 7.5 0 0 1-12.7 5.4"/><path d="M4.5 12A7.5 7.5 0 0 1 17.2 6.6"/><path d="M17 3.5v3.2h-3.2"/><path d="M7 20.5v-3.2h3.2"/><path d="M12 8.5v7"/><path d="M8.5 12h7"/></g>',

    // ── Status Icons (stroke-based, fixed semantic colors, viewBox 0 0 24 24) ──
    'status:pending':
        '<circle cx="12" cy="12" r="8.2" stroke="#8A98A8" stroke-width="1.9"/><path d="M12 7.8V12l3 1.9" stroke="#8A98A8" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>',
    'status:processing':
        '<path d="M18.4 12A6.4 6.4 0 1 1 12 5.6" stroke="#3976C5" stroke-width="2" stroke-linecap="round"/><path d="M15.7 5.6H18.8V8.7" stroke="#3976C5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    'status:success':
        '<circle cx="12" cy="12" r="8.2" stroke="#176B57" stroke-width="1.9"/><path d="M8.2 12.2l2.4 2.4 5.2-5.3" stroke="#176B57" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    'status:failed':
        '<circle cx="12" cy="12" r="8.2" stroke="#D92D20" stroke-width="1.9"/><path d="M12 7.6v5.2" stroke="#D92D20" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.6" r="1" fill="#D92D20"/>',
    'status:skipped':
        '<circle cx="12" cy="12" r="8.2" stroke="#8A98A8" stroke-width="1.9"/><path d="M8.7 15.3l6.6-6.6" stroke="#8A98A8" stroke-width="1.9" stroke-linecap="round"/>',
    'status:retry':
        '<path d="M18.4 12A6.4 6.4 0 1 1 7.2 7.6" stroke="#C89B3C" stroke-width="2" stroke-linecap="round"/><path d="M8.3 5.5H5.2v3.1" stroke="#C89B3C" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    'status:partial-failed':
        '<path d="M12 4.5 20 18.5H4L12 4.5Z" stroke="#C89B3C" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 9v4.1" stroke="#C89B3C" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="15.6" r="1" fill="#C89B3C"/>',

    // ── Service Icons (stroke-based, fixed semantic colors, viewBox 0 0 24 24) ──
    'service:api':
        '<path d="M7.2 16.5h9.6a3.2 3.2 0 0 0 .2-6.4 4.8 4.8 0 0 0-9-1.5A3.6 3.6 0 0 0 7.2 16.5Z" stroke="#8A98A8" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><text x="12" y="14.1" font-size="4.1" font-family="Arial, Helvetica, sans-serif" text-anchor="middle" fill="#8A98A8" font-weight="700">API</text>',
    'service:embedding-worker':
        '<rect x="6" y="6" width="12" height="12" rx="2" stroke="#176B57" stroke-width="1.8"/><path d="M9.2 9.2h5.6v5.6H9.2z" stroke="#176B57" stroke-width="1.6"/><path d="M12 3.8v2M12 18.2v2M3.8 12h2M18.2 12h2M6.4 6.4l1.2 1.2M16.4 16.4l1.2 1.2M16.4 7.6l1.2-1.2M6.4 17.6l1.2-1.2" stroke="#176B57" stroke-width="1.6" stroke-linecap="round"/>',
    'service:cleanup-worker':
        '<path d="M8.5 7.5h7M6.9 7.5h10.2" stroke="#176B57" stroke-width="1.8" stroke-linecap="round"/><path d="M9.2 5.6h5.6" stroke="#176B57" stroke-width="1.8" stroke-linecap="round"/><path d="M7.8 7.5l.7 10a1.5 1.5 0 0 0 1.5 1.4h4a1.5 1.5 0 0 0 1.5-1.4l.7-10" stroke="#176B57" stroke-width="1.8" stroke-linejoin="round"/><path d="M10.2 10.1v5.3M13.8 10.1v5.3" stroke="#176B57" stroke-width="1.7" stroke-linecap="round"/>',
    'service:qdrant':
        '<path d="M12 4.8 18 8.1v7.8L12 19.2 6 15.9V8.1L12 4.8Z" stroke="#3976C5" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 9.4 15.4 11.3v4l-3.4 1.9-3.4-1.9v-4L12 9.4Z" stroke="#D92D20" stroke-width="1.6" stroke-linejoin="round"/>',
    'service:postgresql':
        '<path d="M9.5 6.2c-1.9 0-3.3 1.3-3.3 3v5c0 1.8 1.4 3.1 3.2 3.1 1.2 0 1.8-.4 2.6-1.2.9-.9 1.7-2 2.6-2.8 1.1-1 2-1.3 3.2-1.3" stroke="#8A98A8" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M10.7 8.1c1.2 0 2.4.5 3.1 1.4.7.8 1 1.8 1 2.9v5.2" stroke="#8A98A8" stroke-width="1.8" stroke-linecap="round"/><circle cx="9.7" cy="10.1" r=".95" fill="#8A98A8"/>',
    'service:embedding-service':
        '<circle cx="8" cy="12" r="2" stroke="#176B57" stroke-width="1.7"/><circle cx="16" cy="7.8" r="2" stroke="#3976C5" stroke-width="1.7"/><circle cx="16" cy="16.2" r="2" stroke="#176B57" stroke-width="1.7"/><path d="M9.8 11 14.1 8.8M9.8 13l4.3 2.2" stroke="#8A98A8" stroke-width="1.7" stroke-linecap="round"/>',
};

// 备选回退图标（问号）
const FALLBACK =
    '<path fill="currentColor" d="M11 18h2v-2h-2v2m1-16A10 10 0 0 0 2 12a10 10 0 0 0 10 10 10 10 0 0 0 10-10A10 10 0 0 0 12 2m0 18a8 8 0 0 1-8-8 8 8 0 0 1 8-8 8 8 0 0 1 8 8 8 8 0 0 1-8 8m0-14a2 2 0 0 0-2 2h2a.5.5 0 0 1-.5-.5.5.5 0 0 1 .5-.5 2 2 0 0 1 0 4h-1v2h1a4 4 0 0 0 0-8Z"/>';

// 部分图标（如 Carbon）坐标超出 24×24，使用更大的 viewBox
const VIEWBOX_MAP = {
    'carbon:chart-relationship': '0 0 32 32',
};
const DEFAULT_VIEWBOX = '0 0 24 24';

/**
 * 返回指定图标的 SVG 内联 HTML 字符串。
 * @param {string} name 图标名，如 "mdi:view-dashboard-outline"
 * @returns {string} SVG 标记
 */
export function iconSvg(name) {
    const entry = ICONS[name];
    const path = entry || FALLBACK;
    const vb = VIEWBOX_MAP[name] || DEFAULT_VIEWBOX;
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${vb}" width="1em" height="1em" fill="none" aria-hidden="true">${path}</svg>`;
}

// ── 自定义元素 <local-icon>（仅浏览器环境） ──
const _hasBrowser = typeof window !== 'undefined' && typeof HTMLElement !== 'undefined';
const LocalIconElement = _hasBrowser ? class extends HTMLElement {
    static get observedAttributes() { return ['icon']; }
    attributeChangedCallback() { this._render(); }
    connectedCallback() { this._render(); }
    _render() {
        const name = this.getAttribute('icon') || '';
        this.innerHTML = iconSvg(name);
        const t = this.getAttribute('title');
        const svg = this.firstElementChild;
        if (svg && t) svg.setAttribute('title', t);
    }
} : class { /* Node: no-op */ };

if (_hasBrowser) {
    if (!customElements.get('local-icon')) {
        customElements.define('local-icon', LocalIconElement);
    }
}
