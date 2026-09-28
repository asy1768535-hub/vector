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

export const PREVIEW_LIBRARY = Object.freeze({
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

const PREVIEW_FOLDER_ID_PROJECT = '23232323-2323-4232-8232-232323232323';
const PREVIEW_FOLDER_ID_TECH = '23232323-2323-4232-8232-232323232324';

const INITIAL_DOCUMENTS = [
    {
        document_id: '24242424-2424-4242-8242-242424242424',
        library_id: PREVIEW_LIBRARY.id,
        title: '产品知识手册.docx',
        document_status: 'ready',
        revision_id: '25252525-2525-4252-8252-252525252525',
        revision_no: 3,
        revision_status: 'ready',
        revision_content_hash: 'a'.repeat(64),
        updated_at: '2026-09-12T08:00:00Z',
        uploaded_at: '2026-09-10T08:00:00Z',
        uploader: { display_name: '本地预览用户', username: 'preview', email: null, is_system: false },
        can_delete: true,
        overall_state: 'usable',
        capabilities: {
            source: 'ready', search: 'ready', chat: 'ready', summary: 'ready', outline: 'ready',
            classification: 'pending_review', graph: 'ready',
        },
        classification: {
            state: 'pending_review', decision_set_id: null, taxonomy_version_id: null,
            source: 'model', labels: ['核心架构', '产品文档'], latest_run_id: null, latest_run_status: 'pending_review',
        },
        summary_excerpt: '介绍产品能力、部署方式和常见使用流程。',
        graph_counts: { entities: 12, relations: 8 },
        source_text: '产品知识手册 - 知识库与向量检索管理平台。\n一、系统架构：平台支持企业级知识资产归档、层级目录管理与向量化多路召回。\n二、图谱构建：支持知识主题实体发现与关联关系自动抽取，支撑高精度问答与实体详情探索。\n三、权限管控：提供细粒度的知识库读、写、删与超管授权策略。',
    },
    {
        document_id: '26262626-2626-4262-8262-262626262626',
        library_id: PREVIEW_LIBRARY.id,
        title: '检索接口说明.md',
        document_status: 'ready',
        revision_id: '27272727-2727-4272-8272-272727272727',
        revision_no: 1,
        revision_status: 'ready',
        revision_content_hash: 'b'.repeat(64),
        updated_at: '2026-09-11T08:00:00Z',
        uploaded_at: '2026-09-11T08:00:00Z',
        uploader: { display_name: '本地预览用户', username: 'preview', email: null, is_system: false },
        can_delete: true,
        overall_state: 'partial',
        capabilities: {
            source: 'ready', search: 'ready', chat: 'ready', summary: 'ready', outline: 'disabled',
            classification: 'disabled', graph: 'processing',
        },
        classification: {
            state: 'unclassified', decision_set_id: null, taxonomy_version_id: null,
            source: null, labels: ['技术接口'], latest_run_id: null, latest_run_status: null,
        },
        summary_excerpt: '检索请求参数、过滤条件和返回结果说明。',
        graph_counts: { entities: 4, relations: 2 },
        source_text: '# 知识库检索接口规范与使用说明\n\n请求路径：POST /libraries/{slug}/query\n核心能力：语义相似度检索、混合检索重排、知识图谱关联。',
    },
    {
        document_id: '28282828-2828-4282-8282-282828282828',
        library_id: PREVIEW_LIBRARY.id,
        title: '系统架构设计规范.pdf',
        document_status: 'ready',
        revision_id: '28282828-2828-4282-8282-282828282829',
        revision_no: 2,
        revision_status: 'ready',
        revision_content_hash: 'c'.repeat(64),
        updated_at: '2026-09-12T09:30:00Z',
        uploaded_at: '2026-09-12T08:30:00Z',
        uploader: { display_name: '本地预览用户', username: 'preview', email: null, is_system: false },
        can_delete: true,
        overall_state: 'usable',
        capabilities: {
            source: 'ready', search: 'ready', chat: 'ready', summary: 'ready', outline: 'ready',
            classification: 'disabled', graph: 'ready',
        },
        classification: {
            state: 'unclassified', decision_set_id: null, taxonomy_version_id: null,
            source: null, labels: ['系统架构', '高可用规范'], latest_run_id: null, latest_run_status: null,
        },
        summary_excerpt: '企业级 RAG 知识检索增强流水线与高可用服务架构规范。',
        graph_counts: { entities: 8, relations: 5 },
        source_text: '系统架构设计规范：Vector Database and RAG Pipeline Standard v2.0。\n包含文档解析、分块向量化、Qdrant 存储及混合重排服务。',
    },
    {
        document_id: '29292929-2929-4292-8292-292929292929',
        library_id: PREVIEW_LIBRARY.id,
        title: '常见问题FAQ.txt',
        document_status: 'failed',
        revision_id: '29292929-2929-4292-8292-292929292930',
        revision_no: 1,
        revision_status: 'failed',
        revision_content_hash: 'd'.repeat(64),
        updated_at: '2026-09-14T10:15:00Z',
        uploaded_at: '2026-09-14T10:15:00Z',
        uploader: { display_name: '本地预览用户', username: 'preview', email: null, is_system: false },
        can_delete: true,
        overall_state: 'failed',
        capabilities: {
            source: 'ready', search: 'failed', chat: 'failed', summary: 'failed', outline: 'disabled',
            classification: 'disabled', graph: 'failed',
        },
        classification: {
            state: 'unclassified', decision_set_id: null, taxonomy_version_id: null,
            source: null, labels: [], latest_run_id: null, latest_run_status: null,
        },
        summary_excerpt: '知识库系统常见问题解答，文本分块解析异常。',
        graph_counts: { entities: 0, relations: 0 },
        source_text: '=== 知识库系统常见问题解答 (FAQ) ===\nQ1: 如何上传不同格式的文件？\nQ2: 为什么有些文件显示“仅保存”？\nQ3: 向量化任务失败后如何处理？',
    },
    {
        document_id: '30303030-3030-4030-8030-303030303030',
        library_id: PREVIEW_LIBRARY.id,
        title: '部署运维指南.md',
        document_status: 'ready',
        revision_id: '30303030-3030-4030-8030-303030303031',
        revision_no: 1,
        revision_status: 'ready',
        revision_content_hash: 'e'.repeat(64),
        updated_at: '2026-09-15T09:00:00Z',
        uploaded_at: '2026-09-15T09:00:00Z',
        uploader: { display_name: '本地预览用户', username: 'preview', email: null, is_system: false },
        can_delete: true,
        overall_state: 'usable',
        capabilities: {
            source: 'ready', search: 'ready', chat: 'ready', summary: 'ready', outline: 'ready',
            classification: 'disabled', graph: 'ready',
        },
        classification: {
            state: 'unclassified', decision_set_id: null, taxonomy_version_id: null,
            source: null, labels: ['运维部署'], latest_run_id: null, latest_run_status: null,
        },
        summary_excerpt: '环境准备、本地前端预览模式与容器化集群部署指南。',
        graph_counts: { entities: 6, relations: 3 },
        source_text: '# 知识库系统部署与运维指南\n\n使用 ?preview=1 参数启动轻量化预览模式：python -m http.server 5599',
    },
    {
        document_id: '31313131-3131-4131-8131-313131313131',
        library_id: PREVIEW_LIBRARY.id,
        title: '项目立项报告.docx',
        document_status: 'ready',
        revision_id: '31313131-3131-4131-8131-313131313132',
        revision_no: 1,
        revision_status: 'ready',
        revision_content_hash: 'f'.repeat(64),
        updated_at: '2026-09-16T11:20:00Z',
        uploaded_at: '2026-09-16T11:20:00Z',
        uploader: { display_name: '本地预览用户', username: 'preview', email: null, is_system: false },
        can_delete: true,
        overall_state: 'usable',
        capabilities: {
            source: 'ready', search: 'ready', chat: 'ready', summary: 'ready', outline: 'ready',
            classification: 'disabled', graph: 'ready',
        },
        classification: {
            state: 'unclassified', decision_set_id: null, taxonomy_version_id: null,
            source: null, labels: ['项目管理'], latest_run_id: null, latest_run_status: null,
        },
        summary_excerpt: '企业级自适应知识底座建设目标与里程碑规划。',
        graph_counts: { entities: 5, relations: 3 },
        source_text: '知识库与智能问答协同平台立项报告。\n项目名称：企业级自适应知识底座建设。\n目标：解决非结构化文档孤岛、提高检索召回率、赋能业务智能问答场景。',
    },
    {
        document_id: '32323232-3232-4232-8232-323232323232',
        library_id: PREVIEW_LIBRARY.id,
        title: 'API接口安全规范.pdf',
        document_status: 'processing',
        revision_id: '32323232-3232-4232-8232-323232323233',
        revision_no: 1,
        revision_status: 'processing',
        revision_content_hash: '1'.repeat(64),
        updated_at: '2026-09-17T14:00:00Z',
        uploaded_at: '2026-09-17T14:00:00Z',
        uploader: { display_name: '本地预览用户', username: 'preview', email: null, is_system: false },
        can_delete: true,
        overall_state: 'processing',
        capabilities: {
            source: 'ready', search: 'processing', chat: 'disabled', summary: 'ready', outline: 'ready',
            classification: 'disabled', graph: 'disabled',
        },
        classification: {
            state: 'unclassified', decision_set_id: null, taxonomy_version_id: null,
            source: null, labels: ['网络安全', '鉴权规范'], latest_run_id: null, latest_run_status: null,
        },
        summary_excerpt: '企业 JWT 鉴权与 Casbin 访问控制权限规范。',
        graph_counts: { entities: 0, relations: 0 },
        source_text: 'API Security and Authentication Specification: Enterprise JWT and Casbin Access Control v1.5',
    },
];

export let PREVIEW_DOCUMENTS = [...INITIAL_DOCUMENTS];

const INITIAL_STORED_FILES = [
    {
        file_resource_id: 'res-001-doc',
        document_id: '24242424-2424-4242-8242-242424242424',
        file_name: '产品知识手册.docx',
        relative_path: '/产品知识手册.docx',
        download_url: '/sample-files/产品知识手册.docx',
        content_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        size_bytes: 24576,
        storage_status: 'verified',
        processing_status: 'succeeded',
        processing_stage: 'completed',
        result_operation: 'asset_ready',
        created_at: '2026-09-10T08:00:00Z',
    },
    {
        file_resource_id: 'res-002-md',
        document_id: '26262626-2626-4262-8262-262626262626',
        file_name: '检索接口说明.md',
        relative_path: '/检索接口说明.md',
        download_url: '/sample-files/检索接口说明.md',
        content_type: 'text/markdown',
        size_bytes: 8192,
        storage_status: 'verified',
        processing_status: 'succeeded',
        processing_stage: 'completed',
        result_operation: 'asset_ready',
        created_at: '2026-09-11T08:00:00Z',
    },
    {
        file_resource_id: 'res-003-pdf',
        document_id: '28282828-2828-4282-8282-282828282828',
        file_name: '系统架构设计规范.pdf',
        relative_path: '/系统架构设计规范.pdf',
        download_url: '/sample-files/系统架构设计规范.pdf',
        content_type: 'application/pdf',
        size_bytes: 65536,
        storage_status: 'verified',
        processing_status: 'succeeded',
        processing_stage: 'completed',
        result_operation: 'asset_ready',
        created_at: '2026-09-12T08:30:00Z',
    },
    {
        file_resource_id: 'res-004-xlsx',
        document_id: null,
        file_name: '知识库分类标签表.xlsx',
        relative_path: '/知识库分类标签表.xlsx',
        download_url: '/sample-files/知识库分类标签表.xlsx',
        content_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        size_bytes: 18432,
        storage_status: 'verified',
        processing_status: 'succeeded',
        processing_stage: 'completed',
        result_operation: 'stored_only',
        created_at: '2026-09-13T09:30:00Z',
    },
    {
        file_resource_id: 'res-005-txt',
        document_id: '29292929-2929-4292-8292-292929292929',
        file_name: '常见问题FAQ.txt',
        relative_path: '/常见问题FAQ.txt',
        download_url: '/sample-files/常见问题FAQ.txt',
        content_type: 'text/plain',
        size_bytes: 4096,
        storage_status: 'verified',
        processing_status: 'failed',
        processing_stage: 'chunking',
        result_operation: 'asset_ready',
        created_at: '2026-09-14T10:15:00Z',
    },
    {
        file_resource_id: 'res-006-deploy',
        document_id: '30303030-3030-4030-8030-303030303030',
        file_name: '部署运维指南.md',
        relative_path: '/项目资料/部署运维指南.md',
        download_url: '/sample-files/项目资料/部署运维指南.md',
        content_type: 'text/markdown',
        size_bytes: 12288,
        storage_status: 'verified',
        processing_status: 'succeeded',
        processing_stage: 'completed',
        result_operation: 'asset_ready',
        created_at: '2026-09-15T09:00:00Z',
    },
    {
        file_resource_id: 'res-007-report',
        document_id: '31313131-3131-4131-8131-313131313131',
        file_name: '项目立项报告.docx',
        relative_path: '/项目资料/项目立项报告.docx',
        download_url: '/sample-files/项目资料/项目立项报告.docx',
        content_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        size_bytes: 32768,
        storage_status: 'verified',
        processing_status: 'succeeded',
        processing_stage: 'completed',
        result_operation: 'asset_ready',
        created_at: '2026-09-16T11:20:00Z',
    },
    {
        file_resource_id: 'res-008-sec',
        document_id: '32323232-3232-4232-8232-323232323232',
        file_name: 'API接口安全规范.pdf',
        relative_path: '/技术规范/API接口安全规范.pdf',
        download_url: '/sample-files/技术规范/API接口安全规范.pdf',
        content_type: 'application/pdf',
        size_bytes: 45056,
        storage_status: 'verified',
        processing_status: 'processing',
        processing_stage: 'embedding',
        result_operation: 'asset_ready',
        created_at: '2026-09-17T14:00:00Z',
    },
];

let previewStoredFiles = [...INITIAL_STORED_FILES];

const INITIAL_TASKS = [
    {
        id: 'tsk-001',
        document_id: '24242424-2424-4242-8242-242424242424',
        file_name: '产品知识手册.docx',
        relative_path: '/产品知识手册.docx',
        library_id: PREVIEW_LIBRARY.id,
        library_name: PREVIEW_LIBRARY.name,
        library_slug: PREVIEW_LIBRARY.slug,
        operation_type: 'import',
        status: 'succeeded',
        stage: 'completed',
        result_operation: 'asset_ready',
        created_at: '2026-09-10T08:00:00Z',
        finished_at: '2026-09-10T08:02:15Z',
        failure_message: null,
        failure_action: null,
        can_retry: false,
    },
    {
        id: 'tsk-002',
        document_id: '26262626-2626-4262-8262-262626262626',
        file_name: '检索接口说明.md',
        relative_path: '/检索接口说明.md',
        library_id: PREVIEW_LIBRARY.id,
        library_name: PREVIEW_LIBRARY.name,
        library_slug: PREVIEW_LIBRARY.slug,
        operation_type: 'import',
        status: 'succeeded',
        stage: 'completed',
        result_operation: 'asset_ready',
        created_at: '2026-09-11T08:00:00Z',
        finished_at: '2026-09-11T08:00:45Z',
        failure_message: null,
        failure_action: null,
        can_retry: false,
    },
    {
        id: 'tsk-003',
        document_id: '28282828-2828-4282-8282-282828282828',
        file_name: '系统架构设计规范.pdf',
        relative_path: '/系统架构设计规范.pdf',
        library_id: PREVIEW_LIBRARY.id,
        library_name: PREVIEW_LIBRARY.name,
        library_slug: PREVIEW_LIBRARY.slug,
        operation_type: 'import',
        status: 'succeeded',
        stage: 'completed',
        result_operation: 'asset_ready',
        created_at: '2026-09-12T08:30:00Z',
        finished_at: '2026-09-12T08:33:10Z',
        failure_message: null,
        failure_action: null,
        can_retry: false,
    },
    {
        id: 'tsk-004',
        document_id: null,
        file_name: '知识库分类标签表.xlsx',
        relative_path: '/知识库分类标签表.xlsx',
        library_id: PREVIEW_LIBRARY.id,
        library_name: PREVIEW_LIBRARY.name,
        library_slug: PREVIEW_LIBRARY.slug,
        operation_type: 'import',
        status: 'succeeded',
        stage: 'completed',
        result_operation: 'stored_only',
        created_at: '2026-09-13T09:30:00Z',
        finished_at: '2026-09-13T09:30:10Z',
        failure_message: null,
        failure_action: null,
        can_retry: false,
    },
    {
        id: 'tsk-005',
        document_id: '29292929-2929-4292-8292-292929292929',
        file_name: '常见问题FAQ.txt',
        relative_path: '/常见问题FAQ.txt',
        library_id: PREVIEW_LIBRARY.id,
        library_name: PREVIEW_LIBRARY.name,
        library_slug: PREVIEW_LIBRARY.slug,
        operation_type: 'import',
        status: 'failed',
        stage: 'chunking',
        result_operation: 'asset_ready',
        created_at: '2026-09-14T10:15:00Z',
        finished_at: '2026-09-14T10:16:02Z',
        failure_message: '文本分块解析异常，格式不兼容',
        failure_action: '建议检查文件编码为 UTF-8',
        can_retry: true,
    },
    {
        id: 'tsk-006',
        document_id: '30303030-3030-4030-8030-303030303030',
        file_name: '部署运维指南.md',
        relative_path: '/项目资料/部署运维指南.md',
        library_id: PREVIEW_LIBRARY.id,
        library_name: PREVIEW_LIBRARY.name,
        library_slug: PREVIEW_LIBRARY.slug,
        operation_type: 'import',
        status: 'succeeded',
        stage: 'completed',
        result_operation: 'asset_ready',
        created_at: '2026-09-15T09:00:00Z',
        finished_at: '2026-09-15T09:00:30Z',
        failure_message: null,
        failure_action: null,
        can_retry: false,
    },
    {
        id: 'tsk-007',
        document_id: '31313131-3131-4131-8131-313131313131',
        file_name: '项目立项报告.docx',
        relative_path: '/项目资料/项目立项报告.docx',
        library_id: PREVIEW_LIBRARY.id,
        library_name: PREVIEW_LIBRARY.name,
        library_slug: PREVIEW_LIBRARY.slug,
        operation_type: 'import',
        status: 'succeeded',
        stage: 'completed',
        result_operation: 'asset_ready',
        created_at: '2026-09-16T11:20:00Z',
        finished_at: '2026-09-16T11:22:00Z',
        failure_message: null,
        failure_action: null,
        can_retry: false,
    },
    {
        id: 'tsk-008',
        document_id: '32323232-3232-4232-8232-323232323232',
        file_name: 'API接口安全规范.pdf',
        relative_path: '/技术规范/API接口安全规范.pdf',
        library_id: PREVIEW_LIBRARY.id,
        library_name: PREVIEW_LIBRARY.name,
        library_slug: PREVIEW_LIBRARY.slug,
        operation_type: 'import',
        status: 'processing',
        stage: 'embedding',
        result_operation: 'asset_ready',
        created_at: '2026-09-17T14:00:00Z',
        finished_at: null,
        failure_message: null,
        failure_action: null,
        can_retry: false,
    },
];

let previewTasks = [...INITIAL_TASKS];

let previewImportConfiguration = {
    max_file_bytes: 500 * 1024 * 1024,
    doc_max_file_bytes: 200 * 1024 * 1024,
    chunk_bytes: 32 * 1024 * 1024,
    max_files_per_selection: 1000,
    max_configurable_file_bytes: 50 * 1024 * 1024 * 1024,
    max_configurable_files_per_selection: 100_000,
    upload_concurrency: 1,
    allowed_extensions: [
        '.txt', '.md', '.markdown', '.rst', '.log', '.ini', '.cfg', '.conf',
        '.json', '.yaml', '.yml', '.xml', '.html', '.htm', '.csv', '.tsv',
        '.doc', '.docx', '.pptx', '.xls', '.xlsx', '.pdf',
        '.bmp', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp',
    ],
};

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

const PREVIEW_FAQS = [
    { question: '向量数据库的核心能力是什么？', answer: '支持面向高维密集向量的索引构建与毫秒级余弦/欧式相似度检索。' },
    { question: '如何使用轻量级前端预览模式？', answer: '在 URL 中附加 ?preview=1，即可在不需要真实数据库的情况下模拟全部功能。' },
    { question: '知识图谱与向量检索如何结合？', answer: '检索时先通过向量召回相关分块，再借助图谱关联实体与关系进行多跳事实核验。' },
    { question: '文件支持哪些格式上传与解析？', answer: '支持 PDF、Word (.docx/.doc)、Markdown、TXT、Excel 表格等主流格式。' },
];

let previewConversations = [
    {
        id: 'conv-preview-01',
        title: '关于向量数据库架构咨询',
        library_id: PREVIEW_LIBRARY.id,
        created_at: '2026-09-15T10:00:00Z',
        updated_at: '2026-09-15T10:05:00Z',
    },
];

let previewMessages = {
    'conv-preview-01': [
        {
            role: 'user',
            content: '请介绍一下知识库的核心架构和检索流程。',
            created_at: '2026-09-15T10:00:00Z',
        },
        {
            role: 'assistant',
            content: '平台由三层核心结构组成：\n\n1. **知识资产管理层**：支持层级目录、原文件多副本校验归档以及断点续传。\n2. **向量与图谱检索层**：集成 Qdrant 密集检索与自动图谱抽取引擎，支持多路召回重排。\n3. **问答与应用协同层**：提供 SSE 流式问答、出处溯源和实体详情查看。',
            created_at: '2026-09-15T10:00:15Z',
            sources: [
                { title: '产品知识手册.docx', document_id: '24242424-2424-4242-8242-242424242424', chunk_id: 'chunk-1', similarity: 0.95 },
                { title: '系统架构设计规范.pdf', document_id: '28282828-2828-4282-8282-282828282828', chunk_id: 'chunk-3', similarity: 0.89 },
            ],
        },
    ],
};

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
        const isSameOrLoopback = url.origin === previewOrigin || (LOOPBACK_HOSTS.has(url.hostname) && LOOPBACK_HOSTS.has(new URL(previewOrigin).hostname));
        if (!isSameOrLoopback) return null;
        let parsedBody = null;
        if (typeof init?.body === 'string') {
            try {
                parsedBody = JSON.parse(init.body);
            } catch (_) {
                parsedBody = init.body;
            }
        } else if (init?.body) {
            parsedBody = init.body;
        }
        return {
            method: String(init?.method || input?.method || 'GET').toUpperCase(),
            path: url.pathname,
            query: url.searchParams,
            body: parsedBody,
        };
    } catch (_) {
        return null;
    }
}

function normalizePath(raw) {
    if (!raw) return '';
    let p = String(raw).trim();
    if (!p.startsWith('/')) p = '/' + p;
    return p.replace(/\/+$/, '');
}

function getStoredFilePage(queryPath, page = 1, pageSize = 50) {
    const targetPath = normalizePath(queryPath);
    let folders = [];
    let files = [];

    if (!targetPath) {
        folders = [
            { id: PREVIEW_FOLDER_ID_PROJECT, name: '项目资料', path: '/项目资料', file_total: 2 },
            { id: PREVIEW_FOLDER_ID_TECH, name: '技术规范', path: '/技术规范', file_total: 1 },
        ];
        files = previewStoredFiles.filter((f) => {
            const rel = f.relative_path || '';
            return !rel.startsWith('/项目资料/') && !rel.startsWith('/技术规范/');
        });
    } else if (targetPath === '/项目资料') {
        folders = [];
        files = previewStoredFiles.filter((f) => (f.relative_path || '').startsWith('/项目资料/'));
    } else if (targetPath === '/技术规范') {
        folders = [];
        files = previewStoredFiles.filter((f) => (f.relative_path || '').startsWith('/技术规范/'));
    }

    const offset = (page - 1) * pageSize;
    const pagedFiles = files.slice(offset, offset + pageSize);
    return {
        library_slug: PREVIEW_LIBRARY.slug,
        path: targetPath,
        folders,
        files: pagedFiles,
        folder_total: folders.length,
        file_total: files.length,
        page,
        page_size: pageSize,
    };
}

function makeSseResponse(events) {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
        start(controller) {
            for (const ev of events) {
                controller.enqueue(encoder.encode(`data: ${JSON.stringify(ev)}\n\n`));
            }
            controller.close();
        },
    });
    return new Response(stream, {
        status: 200,
        headers: {
            'Content-Type': 'text/event-stream; charset=utf-8',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        },
    });
}

function previewFixture(parts) {
    if (!parts) return null;
    const { method, path, query, body: requestBody } = parts;

    // ── 认证与基础身份 ──────────────────────────────────────────
    if (method === 'POST' && ['/auth/jwt/login', '/auth/jwt/logout'].includes(path)) {
        return { status: 204, body: null };
    }
    if (method === 'GET' && path === '/users/me') return { body: PREVIEW_USER };
    if (method === 'GET' && path === '/me/permissions') return { body: PREVIEW_PERMISSIONS };
    if (method === 'GET' && path === '/me/organizations') return { body: PREVIEW_ORGANIZATIONS };
    if (method === 'GET' && path === '/me/api-keys') return { body: [] };

    // ── 系统管理与知识库列表 ───────────────────────────────────
    if (method === 'GET' && path === '/admin/users') return { body: [PREVIEW_USER] };
    if (method === 'GET' && path === '/admin/libraries') return { body: [PREVIEW_LIBRARY] };
    if (method === 'GET' && path === '/admin/permissions') return { body: PREVIEW_PERMISSIONS };
    if (method === 'GET' && /^\/(?:admin\/)?libraries\/[^/]+\/faqs$/.test(path)) {
        return { body: PREVIEW_FAQS };
    }
    if (method === 'GET' && path === '/admin/audit-log') return { body: [] };
    if (method === 'GET' && path === '/admin/chat-logs') return { body: [] };
    if (method === 'GET' && path === '/admin/operations/status') {
        return {
            body: {
                now: new Date().toISOString(),
                services: [{ name: 'api', status: 'healthy' }, { name: 'worker', status: 'healthy' }],
                embedding_jobs: { pending: 0, processing: 1, done: 6, failed: 1, total: 8 },
                cleanup_outbox: { pending: 0, processing: 0, failed: 0, total: 0 },
                libraries: { active: 1, deleted: 0, total: 1 },
                rebuild_operations: [],
            },
        };
    }
    if (method === 'GET' && path === '/admin/jobs/monitor') {
        return {
            body: [{
                id: '12121212-1212-4212-8212-121212121212',
                task_type: 'graph',
                title: '产品知识手册.docx',
                library_id: PREVIEW_LIBRARY.id,
                document_id: '24242424-2424-4242-8242-242424242424',
                document_revision: 2,
                document_revision_id: '25252525-2525-4252-8252-252525252525',
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
                processing: 1,
                done: 6,
                failed: 1,
                cancelled: 0,
                superseded: 0,
                retryable_failed: 1,
                retryable_embedding_failed: 0,
                total: 8,
                queues: {
                    import: { pending_count: 0, oldest_pending_age_seconds: null, processing_count: 0, throughput_per_minute: 0, failure_rate: null, window_seconds: 900 },
                    doc_conversion: { pending_count: 0, oldest_pending_age_seconds: null, processing_count: 0, throughput_per_minute: null, failure_rate: null, window_seconds: 900 },
                    embedding: { pending_count: 0, oldest_pending_age_seconds: null, processing_count: 1, throughput_per_minute: 2.5, failure_rate: 0.12, window_seconds: 900 },
                    graph: { pending_count: 0, oldest_pending_age_seconds: null, processing_count: 0, throughput_per_minute: 0, failure_rate: null, window_seconds: 900 },
                },
            },
        };
    }

    // ── 网盘原文件管理 (/me/stored-files) ────────────────────────
    if (method === 'GET' && (path === '/me/stored-files' || path === '/me/files')) {
        const pathValue = String(query.get('path') || '');
        const page = Number(query.get('page') || 1);
        const pageSize = Number(query.get('page_size') || 50);
        return { body: getStoredFilePage(pathValue, page, pageSize) };
    }

    const downloadStoredMatch = path.match(/^\/me\/stored-files\/([^/]+)\/download$/);
    if (method === 'GET' && downloadStoredMatch) {
        const resId = downloadStoredMatch[1];
        const file = previewStoredFiles.find((f) => f.file_resource_id === resId);
        const downloadUrl = file?.download_url || `/sample-files/${encodeURIComponent(file?.file_name || '产品知识手册.docx')}`;
        return { body: { url: downloadUrl, expires_in_seconds: 3600 } };
    }

    const deleteStoredMatch = path.match(/^\/me\/stored-files\/([^/]+)$/);
    if (method === 'DELETE' && deleteStoredMatch) {
        const resId = deleteStoredMatch[1];
        previewStoredFiles = previewStoredFiles.filter((f) => f.file_resource_id !== resId);
        return { status: 202, body: { status: 'deleting' } };
    }

    if (method === 'DELETE' && path === '/me/files/folder') {
        const folderPath = normalizePath(query.get('path') || '');
        if (folderPath) {
            const before = previewStoredFiles.length;
            previewStoredFiles = previewStoredFiles.filter((f) => !(f.relative_path || '').startsWith(folderPath + '/'));
            const deletedCount = before - previewStoredFiles.length;
            return { body: { deleted_count: deletedCount, folder_deleted: true } };
        }
        return { body: { deleted_count: 0, folder_deleted: false } };
    }

    // ── 个人任务列表与统计 (/me/import-tasks) ─────────────────────
    if (method === 'GET' && path === '/me/import-tasks') {
        const status = String(query.get('status') || 'all');
        let filtered = previewTasks;
        if (status && status !== 'all') {
            filtered = previewTasks.filter((t) => t.status === status);
        }
        const limit = Number(query.get('limit') || 50);
        return { body: { items: filtered.slice(0, limit), next_cursor: null } };
    }

    if (method === 'GET' && path === '/me/import-task-summary') {
        const summary = {
            scope: String(query.get('scope') || 'all'),
            total: previewTasks.length,
            pending: previewTasks.filter((t) => t.status === 'queued' || t.status === 'uploading').length,
            processing: previewTasks.filter((t) => t.status === 'processing').length,
            succeeded: previewTasks.filter((t) => t.status === 'succeeded').length,
            failed: previewTasks.filter((t) => t.status === 'failed').length,
        };
        return { body: summary };
    }

    const retryTaskMatch = path.match(/^\/me\/import-tasks\/([^/]+)\/retry$/);
    if (method === 'POST' && retryTaskMatch) {
        const taskId = retryTaskMatch[1];
        const task = previewTasks.find((t) => t.id === taskId);
        if (task) {
            task.status = 'succeeded';
            task.stage = 'completed';
            task.can_retry = false;
            task.failure_message = null;
        }
        return { body: { id: taskId, status: 'queued', message: '已提交重试' } };
    }

    if (method === 'GET' && path === '/me/import-task-files') {
        const failedTasks = previewTasks.filter((t) => t.status === 'failed');
        return {
            body: {
                items: failedTasks,
                total: failedTasks.length,
                page: 1,
                page_size: 20,
            },
        };
    }

    // ── 文件夹与知识库结构 ─────────────────────────────────────
    if (method === 'GET' && /^\/libraries\/[^/]+\/folders$/.test(path)) {
        return {
            body: [
                { id: PREVIEW_FOLDER_ID_PROJECT, name: '项目资料', path: '/项目资料' },
                { id: PREVIEW_FOLDER_ID_TECH, name: '技术规范', path: '/技术规范' },
            ],
        };
    }

    if (method === 'GET' && /^\/libraries\/[^/]+\/stats$/.test(path)) {
        return {
            body: {
                document_count: PREVIEW_DOCUMENTS.length,
                pending_jobs: 0,
                processing_jobs: 1,
                done_jobs: 6,
                failed_jobs: 1,
            },
        };
    }

    if (method === 'GET' && /^\/libraries\/[^/]+\/documents$/.test(path)) {
        return { body: [] };
    }

    // ── 知识资产目录与文档详情 (/catalog/documents) ────────────
    if (method === 'GET' && /^\/libraries\/[^/]+\/catalog\/documents$/.test(path)) {
        return {
            body: {
                items: PREVIEW_DOCUMENTS,
                total: PREVIEW_DOCUMENTS.length,
                next_cursor: null,
            },
        };
    }

    const catalogDocMatch = path.match(/^\/libraries\/[^/]+\/catalog\/documents\/([^/]+)$/);
    if (method === 'GET' && catalogDocMatch) {
        const docId = catalogDocMatch[1];
        const doc = PREVIEW_DOCUMENTS.find((d) => d.document_id === docId) || PREVIEW_DOCUMENTS[0];
        return { body: doc };
    }

    const docDetailMatch = path.match(/^\/libraries\/[^/]+\/documents\/([^/]+)$/);
    if (method === 'GET' && docDetailMatch && !path.includes('/source') && !path.includes('/file')) {
        const docId = docDetailMatch[1];
        const doc = PREVIEW_DOCUMENTS.find((d) => d.document_id === docId) || PREVIEW_DOCUMENTS[0];
        return { body: doc };
    }

    const docProcessingMatch = path.match(/^\/libraries\/[^/]+\/catalog\/documents\/([^/]+)\/processing$/);
    if (method === 'GET' && docProcessingMatch) {
        const docId = docProcessingMatch[1];
        const doc = PREVIEW_DOCUMENTS.find((d) => d.document_id === docId);
        return {
            body: {
                document_id: docId,
                status: doc?.document_status || 'ready',
                stages: [
                    { stage: 'upload', status: 'succeeded', message: null },
                    { stage: 'convert', status: 'succeeded', message: null },
                    { stage: 'parse', status: 'succeeded', message: null },
                    { stage: 'embedding', status: doc?.document_status === 'failed' ? 'failed' : 'succeeded', message: doc?.document_status === 'failed' ? '文本分块解析异常' : null },
                ],
            },
        };
    }

    // ── 文档出处查看与原文件下载 ────────────────────────────────
    const docSourceMatch = path.match(/^\/libraries\/[^/]+\/documents\/([^/]+)\/source$/);
    if (method === 'GET' && docSourceMatch) {
        const docId = docSourceMatch[1];
        const doc = PREVIEW_DOCUMENTS.find((d) => d.document_id === docId) || PREVIEW_DOCUMENTS[0];
        return {
            body: {
                document_title: doc.title,
                file_type: doc.title.endsWith('.pdf') ? 'pdf' : (doc.title.endsWith('.docx') ? 'docx' : 'markdown'),
                text_window: doc.source_text || doc.summary_excerpt,
                window_start: 0,
                window_end: 200,
            },
        };
    }

    const docFullSourceMatch = path.match(/^\/libraries\/[^/]+\/documents\/([^/]+)\/source\/full$/);
    if (method === 'GET' && docFullSourceMatch) {
        const docId = docFullSourceMatch[1];
        const doc = PREVIEW_DOCUMENTS.find((d) => d.document_id === docId) || PREVIEW_DOCUMENTS[0];
        return {
            body: {
                document_title: doc.title,
                content: doc.source_text || doc.summary_excerpt,
                chunks: [
                    { chunk_id: 'chunk-1', text: doc.source_text || doc.summary_excerpt, chunk_index: 0 },
                ],
            },
        };
    }

    const docFileDownloadMatch = path.match(/^\/libraries\/[^/]+\/documents\/([^/]+)\/file$/);
    if (method === 'GET' && docFileDownloadMatch) {
        const docId = docFileDownloadMatch[1];
        const doc = PREVIEW_DOCUMENTS.find((d) => d.document_id === docId) || PREVIEW_DOCUMENTS[0];
        const sampleUrl = `/sample-files/${encodeURIComponent(doc.title)}`;
        return new Response(doc.source_text || 'Sample file content', {
            status: 200,
            headers: {
                'Content-Disposition': `attachment; filename="${encodeURIComponent(doc.title)}"`,
                'Content-Type': 'application/octet-stream',
            },
        });
    }

    const deleteDocMatch = path.match(/^\/libraries\/[^/]+\/documents\/([^/]+)$/);
    if (method === 'DELETE' && deleteDocMatch) {
        const docId = deleteDocMatch[1];
        PREVIEW_DOCUMENTS = PREVIEW_DOCUMENTS.filter((d) => d.document_id !== docId);
        previewStoredFiles = previewStoredFiles.filter((f) => f.document_id !== docId);
        return { status: 204, body: null };
    }

    // ── 导入配置与上传会话 ─────────────────────────────────────
    if (method === 'GET' && /^\/libraries\/[^/]+\/import-configuration$/.test(path)) {
        return { body: { ...previewImportConfiguration } };
    }
    if (method === 'PUT' && /^\/libraries\/[^/]+\/import-configuration$/.test(path)) {
        previewImportConfiguration = {
            ...previewImportConfiguration,
            max_file_bytes: requestBody?.max_file_bytes || previewImportConfiguration.max_file_bytes,
            max_files_per_selection: requestBody?.max_files_per_selection || previewImportConfiguration.max_files_per_selection,
        };
        return { body: { ...previewImportConfiguration } };
    }
    if (method === 'GET' && /^\/libraries\/[^/]+\/v04\/graph-extractions\/upload-configuration$/.test(path)) {
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

    // ── 文件上传会话模拟 ───────────────────────────────────────
    if (method === 'POST' && /^\/libraries\/[^/]+\/import-sessions$/.test(path)) {
        const jobId = 'mock-job-' + Math.random().toString(36).slice(2, 9);
        return {
            body: {
                id: jobId,
                job_id: jobId,
                upload_id: 'mock-up-' + Math.random().toString(36).slice(2, 9),
                chunk_bytes: 32 * 1024 * 1024,
                status: 'uploading',
            },
        };
    }
    if (method === 'PUT' && /^\/libraries\/[^/]+\/import-sessions\/[^/]+\/content$/.test(path)) {
        return { body: { next_offset: 32 * 1024 * 1024 } };
    }
    if (method === 'POST' && /^\/libraries\/[^/]+\/import-sessions\/[^/]+\/complete$/.test(path)) {
        const newDocId = 'doc-' + Math.random().toString(36).slice(2, 10);
        const fileName = requestBody?.file_name || '新上传文件.docx';
        const newDoc = {
            document_id: newDocId,
            library_id: PREVIEW_LIBRARY.id,
            title: fileName,
            document_status: 'ready',
            revision_id: 'rev-' + Math.random().toString(36).slice(2, 10),
            revision_no: 1,
            revision_status: 'ready',
            revision_content_hash: '9'.repeat(64),
            updated_at: new Date().toISOString(),
            uploaded_at: new Date().toISOString(),
            uploader: { display_name: '本地预览用户', username: 'preview', email: null, is_system: false },
            can_delete: true,
            overall_state: 'usable',
            capabilities: {
                source: 'ready', search: 'ready', chat: 'ready', summary: 'ready', outline: 'ready',
                classification: 'unclassified', graph: 'ready',
            },
            classification: { state: 'unclassified', decision_set_id: null, taxonomy_version_id: null, source: null, labels: [], latest_run_id: null, latest_run_status: null },
            summary_excerpt: '本地模拟新上传的文档。',
            graph_counts: { entities: 2, relations: 1 },
            source_text: '这是本地上传模拟生成的文档正文。',
        };
        PREVIEW_DOCUMENTS.unshift(newDoc);
        previewStoredFiles.unshift({
            file_resource_id: 'res-' + Math.random().toString(36).slice(2, 10),
            document_id: newDocId,
            file_name: fileName,
            relative_path: '/' + fileName,
            download_url: `/sample-files/${encodeURIComponent(fileName)}`,
            content_type: 'application/octet-stream',
            size_bytes: 16384,
            storage_status: 'verified',
            processing_status: 'succeeded',
            processing_stage: 'completed',
            result_operation: 'asset_ready',
            created_at: new Date().toISOString(),
        });
        previewTasks.unshift({
            id: 'tsk-' + Math.random().toString(36).slice(2, 9),
            document_id: newDocId,
            file_name: fileName,
            relative_path: '/' + fileName,
            library_id: PREVIEW_LIBRARY.id,
            library_name: PREVIEW_LIBRARY.name,
            library_slug: PREVIEW_LIBRARY.slug,
            operation_type: 'import',
            status: 'succeeded',
            stage: 'completed',
            result_operation: 'asset_ready',
            created_at: new Date().toISOString(),
            finished_at: new Date().toISOString(),
            failure_message: null,
            failure_action: null,
            can_retry: false,
        });
        return { body: { id: newDocId, status: 'succeeded' } };
    }

    // ── 知识检索 (/query) ───────────────────────────────────────
    if (method === 'POST' && /^\/libraries\/[^/]+\/query$/.test(path)) {
        const q = String(requestBody?.query || '').trim();
        const limit = Number(requestBody?.limit || 5);
        const folderId = requestBody?.folder_id;

        const allResults = [
            {
                document_id: '24242424-2424-4242-8242-242424242424',
                chunk_id: 'chk-doc-01',
                title: '产品知识手册.docx',
                similarity: 0.93,
                text: '【产品知识手册】平台支持企业级知识资产归档、层级目录管理与向量化多路召回，面向密集向量提供毫秒级检索响应。',
                metadata: { title: '产品知识手册.docx', page_no: 1, chunk_index: 0 },
            },
            {
                document_id: '26262626-2626-4262-8262-262626262626',
                chunk_id: 'chk-api-01',
                title: '检索接口说明.md',
                similarity: 0.88,
                text: '【检索接口规范】端点 POST /libraries/{slug}/query。支持语义相似度搜索、BM25 全文检索混合重排与过滤限定。',
                metadata: { title: '检索接口说明.md', page_no: 1, chunk_index: 0 },
            },
            {
                document_id: '28282828-2828-4282-8282-282828282828',
                chunk_id: 'chk-arch-01',
                title: '系统架构设计规范.pdf',
                similarity: 0.82,
                text: '【系统架构设计规范】包含解析流水线、Qdrant 向量存储、Celery 异步调度和 Casbin 细粒度权限控制机制。',
                metadata: { title: '系统架构设计规范.pdf', page_no: 1, chunk_index: 0 },
            },
            {
                document_id: '30303030-3030-4030-8030-303030303030',
                chunk_id: 'chk-deploy-01',
                title: '部署运维指南.md',
                similarity: 0.77,
                text: '【部署与运维】使用 ?preview=1 参数启动轻量化前端预览服务，通过 preview_mode.js 本地模拟全部后端服务状态。',
                metadata: { title: '部署运维指南.md', page_no: 1, chunk_index: 0 },
            },
        ];

        let matched = allResults;
        if (q) {
            const keywords = q.split(/\s+/);
            const filtered = allResults.filter((item) => (
                keywords.some((k) => item.text.includes(k) || item.title.includes(k))
            ));
            if (filtered.length > 0) matched = filtered;
        }
        return {
            body: {
                results: matched.slice(0, limit),
            },
        };
    }

    // ── 智能问答与流式对话 (/chat) ──────────────────────────────
    if (method === 'GET' && path === '/chat/libraries') {
        return { body: [PREVIEW_LIBRARY] };
    }
    if (method === 'GET' && path === '/chat/conversations') {
        return { body: previewConversations };
    }
    if (method === 'POST' && path === '/chat/conversations') {
        const newConv = {
            id: 'conv-' + Math.random().toString(36).slice(2, 9),
            title: requestBody?.title || '新对话',
            library_id: PREVIEW_LIBRARY.id,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
        };
        previewConversations.unshift(newConv);
        previewMessages[newConv.id] = [];
        return { body: newConv };
    }
    const convMsgMatch = path.match(/^\/chat\/conversations\/([^/]+)\/messages$/);
    if (method === 'GET' && convMsgMatch) {
        const convId = convMsgMatch[1];
        return { body: previewMessages[convId] || [] };
    }
    const convDeleteMatch = path.match(/^\/chat\/conversations\/([^/]+)$/);
    if (method === 'DELETE' && convDeleteMatch) {
        const convId = convDeleteMatch[1];
        previewConversations = previewConversations.filter((c) => c.id !== convId);
        delete previewMessages[convId];
        return { status: 204, body: null };
    }
    if (method === 'GET' && /^\/libraries\/[^/]+\/chat\/graph-context$/.test(path)) {
        return {
            body: {
                entities: PREVIEW_ENTITIES.slice(0, 2),
                relations: PREVIEW_RELATIONS.slice(0, 1),
            },
        };
    }
    if (method === 'POST' && path === '/chat/stream') {
        const userPrompt = String(requestBody?.content || requestBody?.query || '您好');
        const sseEvents = [
            {
                type: 'sources',
                sources: [
                    { title: '产品知识手册.docx', document_id: '24242424-2424-4242-8242-242424242424', chunk_id: 'chk-doc-01', similarity: 0.94 },
                    { title: '检索接口说明.md', document_id: '26262626-2626-4262-8262-262626262626', chunk_id: 'chk-api-01', similarity: 0.89 },
                ],
            },
            { type: 'delta', text: '您好！针对您的问题（“' + userPrompt + '”），为您整理本地知识库的参考要点如下：\n\n' },
            { type: 'delta', text: '1. **系统架构与数据**：当前处于本地前端开发模式，所有文件状态和接口数据均已由 `preview_mode.js` 完成完整模拟。\n' },
            { type: 'delta', text: '2. **本地文件管理**：本地已准备了包括 `.docx`、`.pdf`、`.md`、`.xlsx` 和 `.txt` 等多种格式的样例文件。\n' },
            { type: 'delta', text: '3. **快速修改前端**：由于前端采用无打包的原生 ES Module 模式，修改 `admin-ui/src/` 下的代码后直接刷新浏览器即可生效！' },
            { type: 'done' },
        ];
        return makeSseResponse(sseEvents);
    }

    // ── 知识图谱与治理接口 ─────────────────────────────────────
    if (method === 'GET' && /^\/libraries\/[^/]+\/classifications\/reviews$/.test(path)) {
        return {
            body: {
                items: [],
                available_labels: ['核心架构', '接口规范', '运维部署', '安全制度'],
                taxonomy_version_id: 'tax-01',
                total: 0,
                limit: Number(query.get('limit') || 20),
                offset: Number(query.get('offset') || 0),
            },
        };
    }
    if (method === 'GET' && /^\/libraries\/[^/]+\/schema-lifecycle\/versions$/.test(path)) {
        return {
            body: {
                library_id: PREVIEW_LIBRARY.id,
                library_slug: PREVIEW_LIBRARY.slug,
                versions: [],
            },
        };
    }
    if (method === 'GET' && /^\/libraries\/[^/]+\/graph-governance\/context$/.test(path)) {
        return {
            body: {
                contract_version: 'graph-governance-context-v1',
                library_id: PREVIEW_LIBRARY.id,
                library_slug: PREVIEW_LIBRARY.slug,
                ontology_versions: [],
            },
        };
    }
    if (method === 'GET' && /^\/libraries\/[^/]+\/graph-governance\/actions$/.test(path)) {
        return {
            body: {
                items: [],
                total: 0,
                limit: Number(query.get('limit') || 50),
                offset: Number(query.get('offset') || 0),
            },
        };
    }
    if (method === 'POST' && /^\/organizations\/[^/]+\/graph-catalog\/entities:search$/.test(path)) {
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
    if (method === 'POST' && /^\/organizations\/[^/]+\/graph-catalog\/relations:search$/.test(path)) {
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
    if (fixture instanceof Response) return fixture;
    const status = fixture.status || 200;
    if (status === 204) return new Response(null, { status });
    return new Response(JSON.stringify(fixture.body), {
        status,
        headers: fixture.headers || { 'Content-Type': 'application/json' },
    });
}

export function createPreviewFetch(originalFetch, previewHref) {
    if (!isLocalPreviewUrl(previewHref) && !(typeof window !== 'undefined' && window.__DEV_PREVIEW__)) return originalFetch;
    const previewOrigin = previewHref && String(previewHref).startsWith('http')
        ? new URL(previewHref).origin
        : 'http://127.0.0.1:5599';
    return async (input, init = {}) => {
        const fixture = previewFixture(requestParts(input, init, previewOrigin));
        if (fixture) return fixtureResponse(fixture);
        return originalFetch(input, init);
    };
}

if (typeof window !== 'undefined') {
    const loc = window.location;
    const isFile = loc.protocol === 'file:';
    const isLoopback = LOOPBACK_HOSTS.has(loc.hostname);
    const hasPreviewParam = loc.search.includes('preview=1') || loc.hash.includes('preview=1');
    const hasNoPreview = loc.search.includes('preview=0') || loc.hash.includes('preview=0');

    window.__DEV_PREVIEW__ = window.__DEV_PREVIEW__ === true
        || (isLoopback && !hasNoPreview)
        || isFile
        || hasPreviewParam
        || isLocalPreviewUrl(loc.href);

    if (window.__DEV_PREVIEW__) {
        const previewHref = 'http://127.0.0.1:5599/?preview=1';
        window.fetch = createPreviewFetch(window.fetch.bind(window), previewHref);
    }
}
