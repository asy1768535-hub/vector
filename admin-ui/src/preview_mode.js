const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);

export const PREVIEW_USER = Object.freeze({
    id: '33333333-3333-4333-8333-333333333333',
    email: 'preview@localhost',
    username: 'preview',
    display_name: '本地预览管理员',
    is_superuser: true,
    is_active: true,
});

export const PREVIEW_PERMISSIONS = Object.freeze([Object.freeze({
    organization_id: '11111111-1111-4111-8111-111111111111',
    library_slug: 'preview-library',
    library_name: '示例知识库',
    actions: Object.freeze(['read', 'insert', 'delete', 'admin']),
})]);

export const PREVIEW_ORGANIZATIONS = Object.freeze([Object.freeze({
    organization_id: '11111111-1111-4111-8111-111111111111',
    slug: 'preview-org',
    name: '示例组织',
    role: 'organization_admin',
})]);

const PREVIEW_LIBRARY = Object.freeze({
    id: '22222222-2222-4222-8222-222222222222',
    organization_id: PREVIEW_ORGANIZATIONS[0].organization_id,
    slug: PREVIEW_PERMISSIONS[0].library_slug,
    name: PREVIEW_PERMISSIONS[0].library_name,
    description: '本地界面预览数据',
    graph_extraction_enabled: true,
    schema_mode: 'explore',
    schema_confirmation_policy: 'required',
    graph_extraction_build_mode: 'standard',
    external_llm_enabled: true,
    graph_extraction_allowed_security_levels: ['internal'],
    deleted_at: null,
});

const PREVIEW_ONTOLOGY_ID = '44444444-4444-4444-8444-444444444444';
const PREVIEW_PUBLICATION_ID = '55555555-5555-4555-8555-555555555555';
const PREVIEW_ENTITY_TYPE_ID = '66666666-6666-4666-8666-666666666666';
const PREVIEW_RELATION_TYPE_ID = '77777777-7777-4777-8777-777777777777';
const PREVIEW_HASH = 'c'.repeat(64);
const PREVIEW_ENTITY_IDS = [
    '88888888-8888-4888-8888-888888888888',
    '99999999-9999-4999-8999-999999999999',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
];

const PREVIEW_ENTITY_NAMES = new Map([
    [PREVIEW_ENTITY_IDS[0], '向量数据库'],
    [PREVIEW_ENTITY_IDS[1], '语义检索'],
    [PREVIEW_ENTITY_IDS[2], '知识图谱'],
]);

function previewEntity(id, overrides = {}) {
    const name = PREVIEW_ENTITY_NAMES.get(id) || '知识主题';
    return {
        id,
        library: PREVIEW_LIBRARY,
        ontology_version_id: PREVIEW_ONTOLOGY_ID,
        entity_type: {
            id: PREVIEW_ENTITY_TYPE_ID,
            key: 'knowledge_topic',
            label: '知识主题',
        },
        canonical_name: name,
        normalized_name: name,
        status: 'active',
        publication_state: 'published',
        publication: {
            id: PREVIEW_PUBLICATION_ID,
            status: 'active',
            item_hash: PREVIEW_HASH,
        },
        counts: { evidence: 0, documents: 0, relations: 2, aliases: 0 },
        confidence: 0.96,
        source_type: 'extracted',
        updated_at: '2026-07-27T08:00:00Z',
        governance_state_hash: PREVIEW_HASH,
        ...overrides,
    };
}

const PREVIEW_ENTITIES = PREVIEW_ENTITY_IDS.map((id) => previewEntity(id));

function previewRelation(id, sourceId, targetId, label, key) {
    return {
        id,
        library: PREVIEW_LIBRARY,
        ontology_version_id: PREVIEW_ONTOLOGY_ID,
        relation_type: {
            id: PREVIEW_RELATION_TYPE_ID,
            key,
            label,
            direction: 'directed',
        },
        source: {
            id: sourceId,
            canonical_name: PREVIEW_ENTITY_NAMES.get(sourceId),
        },
        target: {
            id: targetId,
            canonical_name: PREVIEW_ENTITY_NAMES.get(targetId),
        },
        source_entity_id: sourceId,
        target_entity_id: targetId,
        status: 'active',
        review_status: 'approved',
        publication_state: 'published',
        counts: { evidence: 0, documents: 0 },
        confidence: 0.93,
        source_type: 'extracted',
    };
}

const PREVIEW_RELATIONS = [
    previewRelation(
        'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        PREVIEW_ENTITY_IDS[0],
        PREVIEW_ENTITY_IDS[1],
        '提供能力',
        'enables',
    ),
    previewRelation(
        'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
        PREVIEW_ENTITY_IDS[0],
        PREVIEW_ENTITY_IDS[2],
        '支撑构建',
        'supports',
    ),
];

export function isLocalPreviewUrl(value) {
    try {
        const url = new URL(value);
        return LOOPBACK_HOSTS.has(url.hostname)
            && url.port === '5599'
            && url.searchParams.get('preview') === '1';
    } catch (_) {
        return false;
    }
}

function requestParts(input, init, previewOrigin) {
    try {
        const rawUrl = typeof input === 'string' || input instanceof URL
            ? String(input)
            : input?.url;
        const url = new URL(rawUrl, previewOrigin);
        if (url.origin !== previewOrigin) return null;
        return {
            method: String(init?.method || input?.method || 'GET').toUpperCase(),
            path: url.pathname,
            query: url.searchParams,
            body: typeof init?.body === 'string'
                ? JSON.parse(init.body)
                : null,
        };
    } catch (_) {
        return null;
    }
}

function previewFixture(parts) {
    if (!parts) return null;
    const { method, path, query, body: requestBody } = parts;

    if (method === 'POST' && ['/auth/jwt/login', '/auth/jwt/logout'].includes(path)) {
        return { status: 204, body: null };
    }
    if (method === 'GET' && path === '/users/me') return { body: PREVIEW_USER };
    if (method === 'GET' && path === '/me/permissions') {
        return { body: PREVIEW_PERMISSIONS };
    }
    if (method === 'GET' && path === '/me/organizations') {
        return { body: PREVIEW_ORGANIZATIONS };
    }
    if (method === 'GET' && path === '/me/api-keys') return { body: [] };

    if (method === 'GET' && path === '/admin/users') return { body: [PREVIEW_USER] };
    if (method === 'GET' && path === '/admin/libraries') return { body: [PREVIEW_LIBRARY] };
    if (method === 'GET' && path === '/admin/permissions') {
        return { body: PREVIEW_PERMISSIONS };
    }
    if (method === 'GET' && /^\/admin\/libraries\/[^/]+\/faqs$/.test(path)) {
        return { body: [] };
    }
    if (method === 'GET' && path === '/admin/jobs/monitor') {
        return {
            body: [{
                id: '12121212-1212-4212-8212-121212121212',
                task_type: 'graph',
                title: '产品知识手册.docx',
                library_id: PREVIEW_LIBRARY.id,
                document_id: '13131313-1313-4313-8313-131313131313',
                document_revision: 2,
                document_revision_id: '14141414-1414-4414-8414-141414141414',
                status: 'processing',
                raw_status: 'processing',
                stage: 'extracting',
                worker_id: 'graph-worker-preview',
                attempt_count: 0,
                last_error: null,
                created_at: '2026-07-30T08:00:00Z',
                claimed_at: '2026-07-30T08:01:00Z',
                finished_at: null,
                retryable: false,
                build_mode: 'standard',
                publication_status: 'publishing',
                progress: {
                    completed: 18, total: 30, percent: 60,
                    queued: 8, processing: 4,
                    completed_batches: 3, planned_batches: 5,
                },
                metrics: {
                    eta_seconds: 180,
                    cache_hits: 6,
                    configured_concurrency: 4,
                    effective_concurrency: 3,
                    in_flight: 3,
                    throttled_count: 1,
                    retry_count: 2,
                },
            }],
        };
    }
    if (method === 'GET' && path === '/admin/jobs/monitor/stats') {
        return {
            body: {
                pending: 0,
                processing: 0,
                done: 0,
                failed: 0,
                cancelled: 0,
                superseded: 0,
                retryable_failed: 0,
                total: 0,
            },
        };
    }
    if (method === 'GET' && path === '/admin/jobs') return { body: [] };
    if (method === 'GET' && path === '/admin/jobs/stats') {
        return { body: { pending: 0, processing: 0, done: 0, failed: 0, total: 0 } };
    }
    if (method === 'GET' && path === '/admin/audit-log') return { body: [] };
    if (method === 'GET' && path === '/admin/chat-logs') return { body: [] };
    if (method === 'GET' && path === '/admin/operations/status') {
        return {
            body: {
                now: new Date().toISOString(),
                services: [],
                embedding_jobs: {
                    pending: 0, processing: 0, done: 0, failed: 0, total: 0,
                },
                cleanup_outbox: { pending: 0, processing: 0, failed: 0, total: 0 },
                libraries: { active: 1, deleted: 0, total: 1 },
                rebuild_operations: [],
            },
        };
    }

    if (method === 'GET' && path === '/chat/libraries') {
        return { body: [PREVIEW_LIBRARY] };
    }
    if (method === 'GET' && path === '/chat/conversations') return { body: [] };

    if (method === 'GET' && /^\/libraries\/[^/]+\/documents$/.test(path)) {
        return { body: [] };
    }
    if (method === 'GET'
        && /^\/libraries\/[^/]+\/v04\/graph-extractions\/upload-configuration$/.test(path)) {
        return {
            body: {
                available: true,
                exploration_available: true,
                default_requested: true,
                default_build_mode: 'standard',
                allowed_security_levels: ['internal'],
                reasons: [],
                schema_mode: 'explore',
                schema_confirmation_policy: 'required',
                requires_active_schema: false,
            },
        };
    }
    if (method === 'GET' && /^\/libraries\/[^/]+\/stats$/.test(path)) {
        return {
            body: {
                document_count: 0,
                pending_jobs: 0,
                processing_jobs: 0,
                done_jobs: 0,
                failed_jobs: 0,
            },
        };
    }
    if (method === 'GET' && /^\/libraries\/[^/]+\/catalog\/documents$/.test(path)) {
        return { body: { items: [], total: 0, next_cursor: null } };
    }
    if (method === 'GET'
        && /^\/libraries\/[^/]+\/classifications\/reviews$/.test(path)) {
        return {
            body: {
                items: [],
                available_labels: [],
                taxonomy_version_id: '',
                total: 0,
                limit: Number(query.get('limit') || 20),
                offset: Number(query.get('offset') || 0),
            },
        };
    }
    if (method === 'GET'
        && /^\/libraries\/[^/]+\/schema-lifecycle\/versions$/.test(path)) {
        return {
            body: {
                library_id: PREVIEW_LIBRARY.id,
                library_slug: PREVIEW_LIBRARY.slug,
                versions: [],
            },
        };
    }
    if (method === 'GET'
        && /^\/libraries\/[^/]+\/graph-governance\/context$/.test(path)) {
        return {
            body: {
                contract_version: 'graph-governance-context-v1',
                library_id: PREVIEW_LIBRARY.id,
                library_slug: PREVIEW_LIBRARY.slug,
                ontology_versions: [],
            },
        };
    }
    if (method === 'GET'
        && /^\/libraries\/[^/]+\/graph-governance\/actions$/.test(path)) {
        return {
            body: {
                items: [],
                total: 0,
                limit: Number(query.get('limit') || 50),
                offset: Number(query.get('offset') || 0),
            },
        };
    }
    if (method === 'POST'
        && /^\/organizations\/[^/]+\/graph-catalog\/entities:search$/.test(path)) {
        const queryText = String(requestBody?.query || '').trim().toLocaleLowerCase('zh-CN');
        const items = queryText
            ? PREVIEW_ENTITIES.filter((item) => (
                item.canonical_name.toLocaleLowerCase('zh-CN').includes(queryText)
            ))
            : PREVIEW_ENTITIES;
        return {
            body: {
                contract_version: 'graph-catalog-entities-v1',
                items,
                next_cursor: null,
            },
        };
    }
    if (method === 'POST'
        && /^\/organizations\/[^/]+\/graph-catalog\/relations:search$/.test(path)) {
        return {
            body: {
                contract_version: 'graph-catalog-relations-v1',
                items: PREVIEW_RELATIONS,
                next_cursor: null,
            },
        };
    }
    const entityDetailMatch = path.match(
        /^\/organizations\/[^/]+\/graph-catalog\/libraries\/[^/]+\/entities\/([^/]+)$/,
    );
    if (method === 'GET' && entityDetailMatch) {
        const entity = previewEntity(entityDetailMatch[1]);
        const descriptions = {
            [PREVIEW_ENTITY_IDS[0]]: {
                定义: '面向向量表示进行存储、索引和相似度查询的数据系统。',
                核心能力: '向量索引、相似度检索、元数据过滤与知识关联。',
                典型用途: '语义搜索、检索增强生成和知识图谱检索。',
            },
            [PREVIEW_ENTITY_IDS[1]]: {
                定义: '依据语义相似度而不是仅依赖关键词匹配的信息检索方式。',
                依赖: '嵌入模型、向量索引和结果重排。',
            },
            [PREVIEW_ENTITY_IDS[2]]: {
                定义: '以实体和关系组织知识，并保留事实证据来源的结构化网络。',
                组成: '实体、关系、属性、证据和发布版本。',
            },
        };
        return {
            body: {
                contract_version: 'graph-catalog-entity-detail-v1',
                entity,
                properties: descriptions[entity.id] || {},
                aliases: [],
                alias_count: 0,
                aliases_truncated: false,
                evidence: [],
                evidence_count: 0,
                evidence_truncated: false,
                related_relations: PREVIEW_RELATIONS.filter((item) => (
                    item.source_entity_id === entity.id || item.target_entity_id === entity.id
                )),
                relation_count: PREVIEW_RELATIONS.filter((item) => (
                    item.source_entity_id === entity.id || item.target_entity_id === entity.id
                )).length,
                relations_truncated: false,
                documents: [],
                document_count: 0,
                documents_truncated: false,
                extraction: null,
            },
        };
    }
    const relationDetailMatch = path.match(
        /^\/organizations\/[^/]+\/graph-catalog\/libraries\/[^/]+\/relations\/([^/]+)$/,
    );
    if (method === 'GET' && relationDetailMatch) {
        const relation = PREVIEW_RELATIONS.find((item) => item.id === relationDetailMatch[1]);
        if (relation) {
            return {
                body: {
                    contract_version: 'graph-catalog-relation-detail-v1',
                    relation,
                    properties: {},
                    evidence: [],
                    evidence_count: 0,
                    evidence_truncated: false,
                    documents: [],
                    document_count: 0,
                    documents_truncated: false,
                    extraction: null,
                },
            };
        }
    }
    if (method === 'POST' && /^\/libraries\/[^/]+\/v06\/graph\/query$/.test(path)) {
        const requestedSeed = String(requestBody?.seeds?.[0]?.entity_id || PREVIEW_ENTITY_IDS[0]);
        const seedId = PREVIEW_ENTITY_NAMES.has(requestedSeed)
            ? requestedSeed
            : PREVIEW_ENTITY_IDS[0];
        const neighborIds = PREVIEW_ENTITY_IDS.filter((id) => id !== seedId);
        const nodes = [seedId, ...neighborIds].map((id, index) => ({
            id,
            item_hash: PREVIEW_HASH,
            entity_type: {
                id: PREVIEW_ENTITY_TYPE_ID,
                key: 'knowledge_topic',
                label: '知识主题',
            },
            canonical_name: PREVIEW_ENTITY_NAMES.get(id),
            normalized_name: PREVIEW_ENTITY_NAMES.get(id),
            source_type: 'extracted',
            confidence: 0.96,
            depth: index === 0 ? 0 : 1,
            evidence: [],
        }));
        const relations = neighborIds.map((targetId, index) => ({
            id: PREVIEW_RELATIONS[index].id,
            item_hash: PREVIEW_HASH,
            relation_type: PREVIEW_RELATIONS[index].relation_type,
            source_entity_id: seedId,
            target_entity_id: targetId,
            source_type: 'extracted',
            confidence: 0.93,
            depth: 1,
            evidence: [],
        }));
        return {
            body: {
                contract_version: 'v1',
                publication: {
                    id: PREVIEW_PUBLICATION_ID,
                    ontology_version_id: PREVIEW_ONTOLOGY_ID,
                    manifest_version: 'v1',
                    manifest_hash: PREVIEW_HASH,
                    activated_at: '2026-07-27T08:00:00Z',
                },
                seed_matches: [{ input_index: 0, entity_id: seedId }],
                nodes,
                relations,
                counts: {
                    seeds: 1,
                    nodes: nodes.length,
                    relations: relations.length,
                    evidence_locators: 0,
                },
                truncated: { nodes: false, relations: false, evidence: false },
            },
        };
    }
    if (method === 'GET' && path === '/health') {
        return { body: { status: 'ok', mode: 'preview' } };
    }
    return null;
}

function fixtureResponse(fixture) {
    const status = fixture.status || 200;
    if (status === 204) return new Response(null, { status });
    return new Response(JSON.stringify(fixture.body), {
        status,
        headers: { 'Content-Type': 'application/json' },
    });
}

export function createPreviewFetch(originalFetch, previewHref) {
    if (!isLocalPreviewUrl(previewHref)) return originalFetch;
    const previewOrigin = new URL(previewHref).origin;
    return async (input, init = {}) => {
        const fixture = previewFixture(requestParts(input, init, previewOrigin));
        if (fixture) return fixtureResponse(fixture);
        return originalFetch(input, init);
    };
}

if (typeof window !== 'undefined') {
    window.__DEV_PREVIEW__ = isLocalPreviewUrl(window.location.href);
    if (window.__DEV_PREVIEW__) {
        window.fetch = createPreviewFetch(window.fetch.bind(window), window.location.href);
    }
}
