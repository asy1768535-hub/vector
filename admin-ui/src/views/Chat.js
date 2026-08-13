import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { marked } from '../../vendor/marked.esm.js';
import DOMPurify from '../../vendor/dompurify.es.mjs';
import { createStreamQueue } from '../stream_queue.js';
import { copyTextToClipboard } from '../copy_text.js';
import { chatWelcome } from '../illustrations.js';
import { extractCitationIndex, highlightSourceWindow, renderAssistantMarkdown } from '../chat_citations.js';
import {
    chatGraphExpansionRequest,
    chatGraphRelationLabel,
    mergeChatGraph,
    prepareChatGraph,
} from '../chat_graph_exploration.js';
import {
    graphExplorationErrorProjection,
    graphTraversalResponseMatches,
    malformedGraphExplorationError,
} from '../graph_exploration_ui.js';
import GraphCanvas from '../components/GraphCanvas.js';

// ── Markdown → safe HTML ──
function renderMarkdown(text, sources = []) {
    return renderAssistantMarkdown(text, sources, { marked, DOMPurify });
}

function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    if (sameDay) return `今天 ${hh}:${mm}`;
    const M = d.getMonth() + 1;
    const D = d.getDate();
    return `${M}/${D} ${hh}:${mm}`;
}

function fmtScore(source) {
    if (source?.score_type === 'rrf') return '融合排序';
    const raw = source?.display_score;
    if (raw === null || raw === undefined || !Number.isFinite(Number(raw))) return '融合排序';
    const score = Math.max(0, Math.min(1, Number(raw)));
    const label = source.score_type === 'rerank' ? '相关度'
        : source.score_type === 'vector' ? '向量相似度' : '';
    return label ? `${label} ${(score * 100).toFixed(1)}%` : '融合排序';
}

function nowISO() { return new Date().toISOString(); }

const LAST_CHAT_LIBRARY_KEY = 'vectorDatabase.chat.lastLibrary';

function savedChatLibrary() {
    try { return window.localStorage.getItem(LAST_CHAT_LIBRARY_KEY) || ''; }
    catch (_) { return ''; }
}

function saveChatLibrary(slug) {
    try {
        if (slug) window.localStorage.setItem(LAST_CHAT_LIBRARY_KEY, slug);
    } catch (_) { /* storage may be disabled */ }
}

export default {
    components: { GraphCanvas },
    setup() {
        const libs = ref([]);
        const currentSlug = ref(null);
        const conversations = ref([]);
        const currentConvId = ref(null);
        const messages = ref([]);
        const loadingEarlierMessages = ref(false);
        const hasEarlierMessages = ref(false);
        const input = ref('');
        const loading = ref(false);
        const chatDisabled = ref(false);
        const streamRef = ref(null);
        const mobileHistoryOpen = ref(false);
        const topK = ref(5);
        const recalledChunkDialog = ref({ open: false, source: null });
        const sourceLocationDialog = ref({ open: false, loading: false, source: null, data: null, error: '' });
        const citationGraphDialog = ref({
            open: false,
            loading: false,
            source: null,
            data: null,
            error: '',
            selected: null,
        });
        const expandingGraphEntityId = ref('');
        const expandedGraphEntityIds = ref(new Set());
        const graphExpansionIssue = ref(null);
        const graphExpansionLimitReached = ref(false);
        const expandedGraphEntityIdList = computed(() => [...expandedGraphEntityIds.value]);
        let sourceLocationRequestSeq = 0;
        let citationGraphRequestSeq = 0;
        let conversationRequestSeq = 0;
        let _abortController = null;

        const _queue = createStreamQueue();
        let _rafId = null;
        let _aiMsgRef = null;

        function _flushDeltas() {
            if (!_aiMsgRef || _queue.isEmpty()) { _rafId = null; return; }
            _aiMsgRef.text += _queue.flush();
            _rafId = null;
            scrollToBottom();
        }

        function _enqueueDelta(t) {
            if (!_aiMsgRef) return;
            _queue.enqueue(t);
            if (!_rafId) _rafId = requestAnimationFrame(_flushDeltas);
        }

        function _flushPending() {
            if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
            _flushDeltas();
        }

        function _cleanupStream() {
            if (_abortController) { _abortController.abort(); _abortController = null; }
            if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
            _queue.cleanup();
            _aiMsgRef = null;
        }

        const convsForLib = computed(() =>
            conversations.value.filter((c) => c.library_slug === currentSlug.value));

        async function loadLibs(forceRefresh = false) {
            try {
                libs.value = await api.listChatLibraries(forceRefresh);
                const preferred = currentSlug.value || savedChatLibrary();
                currentSlug.value = libs.value.some((item) => item.slug === preferred)
                    ? preferred
                    : (libs.value[0]?.slug || null);
                saveChatLibrary(currentSlug.value);
            } catch (e) { ElMessage.error(e.message); }
        }

        async function loadConversations() {
            try {
                conversations.value = await api.listChatConversations();
            } catch (e) { /* no-op */ }
        }

        function closeAndInvalidateSourceDialog() {
            ++sourceLocationRequestSeq;
            sourceLocationDialog.value = { open: false, loading: false, source: null, data: null, error: '' };
        }

        function closeAndInvalidateCitationGraph() {
            ++citationGraphRequestSeq;
            expandingGraphEntityId.value = '';
            expandedGraphEntityIds.value = new Set();
            graphExpansionIssue.value = null;
            graphExpansionLimitReached.value = false;
            citationGraphDialog.value = {
                open: false,
                loading: false,
                source: null,
                data: null,
                error: '',
                selected: null,
            };
        }

        function onLibChange() {
            conversationRequestSeq += 1;
            loadingEarlierMessages.value = false;
            saveChatLibrary(currentSlug.value);
            _cleanupStream();
            closeAndInvalidateSourceDialog();
            closeAndInvalidateCitationGraph();
            currentConvId.value = null;
            messages.value = [];
            hasEarlierMessages.value = false;
        }

        function newChat() {
            conversationRequestSeq += 1;
            loadingEarlierMessages.value = false;
            _cleanupStream();
            closeAndInvalidateSourceDialog();
            closeAndInvalidateCitationGraph();
            currentConvId.value = null;
            messages.value = [];
            hasEarlierMessages.value = false;
        }

        function historyMessage(m) {
            return {
                id: m.id,
                role: m.role === 'assistant' ? 'ai' : 'user',
                text: m.content + (m.status === 'failed' && m.error_message ? '\n\n*[失败]* ' + m.error_message : ''),
                sources: m.sources || [],
                graph_augmented: m.graph_augmented === true,
                graph_evidence: m.graph_evidence || [],
                error: m.status === 'failed',
                time: m.created_at || null,
            };
        }

        async function scrollToBottom() {
            await nextTick();
            const el = streamRef.value;
            if (el) el.scrollTop = el.scrollHeight;
        }

        async function selectConversation(conv) {
            if (conv.id === currentConvId.value) return;
            const seq = ++conversationRequestSeq;
            _cleanupStream();
            closeAndInvalidateSourceDialog();
            closeAndInvalidateCitationGraph();
            currentSlug.value = conv.library_slug;
            saveChatLibrary(currentSlug.value);
            currentConvId.value = conv.id;
            messages.value = [];
            loadingEarlierMessages.value = false;
            hasEarlierMessages.value = false;
            try {
                const hist = await api.getChatConversationMessages(conv.id, { limit: 200 });
                if (seq !== conversationRequestSeq || currentConvId.value !== conv.id) return;
                messages.value = hist.map(historyMessage);
                hasEarlierMessages.value = hist.length === 200;
                scrollToBottom();
            } catch (e) {
                if (seq === conversationRequestSeq) ElMessage.error(e.message);
            }
        }

        async function loadEarlierMessages() {
            const before = messages.value[0]?.id;
            if (!currentConvId.value || !before || loadingEarlierMessages.value) return;
            const seq = conversationRequestSeq;
            const conversationId = currentConvId.value;
            const el = streamRef.value;
            const previousHeight = el?.scrollHeight || 0;
            loadingEarlierMessages.value = true;
            try {
                const hist = await api.getChatConversationMessages(
                    conversationId,
                    { limit: 200, before },
                );
                if (seq !== conversationRequestSeq || currentConvId.value !== conversationId) return;
                messages.value = [...hist.map(historyMessage), ...messages.value];
                hasEarlierMessages.value = hist.length === 200;
                await nextTick();
                if (el) el.scrollTop += el.scrollHeight - previousHeight;
            } catch (e) {
                if (seq === conversationRequestSeq) ElMessage.error(e.message || '加载更早消息失败');
            } finally {
                if (seq === conversationRequestSeq) loadingEarlierMessages.value = false;
            }
        }

        async function archiveConv(conv) {
            try {
                await api.archiveChatConversation(conv.id);
                if (conv.id === currentConvId.value) newChat();
                loadConversations();
            } catch (e) { ElMessage.error(e.message); }
        }

        async function deleteConv(conv) {
            try {
                await ElMessageBox.confirm(`删除会话「${conv.title}」？不可恢复。`, '确认', { type: 'warning' });
                await api.deleteChatConversation(conv.id);
                if (conv.id === currentConvId.value) newChat();
                loadConversations();
            } catch (e) {
                if (e !== 'cancel') ElMessage.error(e.message || String(e));
            }
        }

        async function copyAnswer(text) {
            const value = String(text || '').trim();
            if (!value) { ElMessage.warning('暂无可复制内容'); return; }
            try {
                await copyTextToClipboard(value);
                ElMessage.success('回答已复制');
            } catch (e) { ElMessage.error(e.message || '复制失败，请手动选择内容'); }
        }

        async function copySourceText(text) {
            const value = String(text || '').trim();
            if (!value) { ElMessage.warning('暂无可复制内容'); return; }
            try {
                await copyTextToClipboard(value);
                ElMessage.success('引用内容已复制');
            } catch (e) { ElMessage.error(e.message || '复制失败，请手动选择内容'); }
        }

        function openCitationChunk(source) {
            if (!source) return;
            recalledChunkDialog.value = { open: true, source };
        }

        function messageGraphSource(message) {
            return (message?.graph_evidence || []).find((source) => source?.chunk_id)
                || (message?.sources || []).find((source) => source?.chunk_id)
                || null;
        }

        function citationSources(message) {
            const sources = [...(message?.sources || [])];
            for (const evidence of message?.graph_evidence || []) {
                const index = Number(evidence.citation_index) - 1;
                if (index >= 0) sources[index] = evidence;
            }
            return sources;
        }

        function citationGraphError(error) {
            if (error?.status === 403) return '你没有该知识库的图谱读取权限';
            if (error?.status === 404) return '该引用分片已不可用';
            if (error?.status === 409) return '图谱发布版本已变化，请重新打开';
            if (error?.status === 503 || error?.status === 504) {
                return '已发布知识图谱暂不可用，请稍后重试';
            }
            return '知识图谱加载失败';
        }

        async function openCitationGraph(source) {
            const slug = currentSlug.value;
            const chunkId = source?.chunk_id;
            if (!slug || !chunkId) {
                ElMessage.warning('该引用缺少可用的分片标识');
                return;
            }
            const requestSeq = ++citationGraphRequestSeq;
            expandingGraphEntityId.value = '';
            expandedGraphEntityIds.value = new Set();
            graphExpansionIssue.value = null;
            graphExpansionLimitReached.value = false;
            recalledChunkDialog.value = { open: false, source: null };
            citationGraphDialog.value = {
                open: true,
                loading: true,
                source,
                data: null,
                error: '',
                selected: null,
            };
            try {
                const data = await api.getChatGraphContext(slug, chunkId);
                if (requestSeq !== citationGraphRequestSeq) return;
                citationGraphDialog.value = {
                    open: true,
                    loading: false,
                    source,
                    data: data?.graph ? { ...data, graph: prepareChatGraph(data.graph) } : data,
                    error: '',
                    selected: null,
                };
                expandedGraphEntityIds.value = new Set(
                    (data?.graph?.seed_matches || []).map((item) => item.entity_id),
                );
            } catch (error) {
                if (requestSeq !== citationGraphRequestSeq) return;
                citationGraphDialog.value = {
                    open: true,
                    loading: false,
                    source,
                    data: null,
                    error: citationGraphError(error),
                    selected: null,
                };
            }
        }

        async function expandCitationGraphNode(node) {
            const graph = citationGraphDialog.value.data?.graph;
            if (!citationGraphDialog.value.open || !graph || !node?.id
                || expandingGraphEntityId.value
                || expandedGraphEntityIds.value.has(node.id)
                || graphExpansionLimitReached.value) return;
            const expansion = chatGraphExpansionRequest(graph, node.id);
            if (!expansion) return;
            const requestSeq = citationGraphRequestSeq;
            expandingGraphEntityId.value = node.id;
            graphExpansionIssue.value = null;
            try {
                const response = await api.queryPublishedGraph(currentSlug.value, expansion.request);
                if (requestSeq !== citationGraphRequestSeq || !citationGraphDialog.value.open) return;
                if (!graphTraversalResponseMatches(response, expansion.identity)) {
                    graphExpansionIssue.value = {
                        nodeId: node.id,
                        message: malformedGraphExplorationError().message,
                    };
                    return;
                }
                const merged = mergeChatGraph(citationGraphDialog.value.data.graph, response, node.id);
                expandedGraphEntityIds.value = new Set([...expandedGraphEntityIds.value, node.id]);
                graphExpansionLimitReached.value = merged.limitReached;
                const currentSelection = citationGraphDialog.value.selected;
                const mergedPivot = merged.graph.nodes.find((item) => item.id === node.id) || node;
                citationGraphDialog.value = {
                    ...citationGraphDialog.value,
                    data: { ...citationGraphDialog.value.data, graph: merged.graph },
                    selected: currentSelection?.kind === 'entity'
                        && currentSelection.item?.id === node.id
                        ? { kind: 'entity', item: mergedPivot }
                        : currentSelection,
                };
            } catch (error) {
                if (requestSeq !== citationGraphRequestSeq || !citationGraphDialog.value.open) return;
                graphExpansionIssue.value = {
                    nodeId: node.id,
                    message: graphExplorationErrorProjection(error).message,
                };
            } finally {
                if (requestSeq === citationGraphRequestSeq) expandingGraphEntityId.value = '';
            }
        }

        function selectCitationGraphNode(node) {
            citationGraphDialog.value.selected = { kind: 'entity', item: node };
        }

        function focusCitationGraphNode(node) {
            citationGraphDialog.value.selected = { kind: 'entity', item: node };
            void expandCitationGraphNode(node);
        }

        function selectCitationGraphRelation(relation) {
            citationGraphDialog.value.selected = { kind: 'relation', item: relation };
        }

        function clearCitationGraphSelection() {
            citationGraphDialog.value.selected = null;
            graphExpansionIssue.value = null;
        }

        function citationGraphExpansionStatus(nodeId) {
            if (expandingGraphEntityId.value === nodeId) return '正在加载关联实体';
            if (graphExpansionIssue.value?.nodeId === nodeId) return graphExpansionIssue.value.message;
            if (graphExpansionLimitReached.value) return '已达到当前图谱展示上限';
            if (expandedGraphEntityIds.value.has(nodeId)) return '一跳关系已加载';
            return '';
        }

        function citationGraphSelection() {
            const selected = citationGraphDialog.value.selected;
            const graph = citationGraphDialog.value.data?.graph;
            if (!selected || !graph) return null;
            if (selected.kind === 'entity') {
                const relationCount = graph.relations.filter((relation) => (
                    relation.source_entity_id === selected.item.id
                    || relation.target_entity_id === selected.item.id
                )).length;
                return Object.freeze({
                    kind: selected.item.depth === 0 ? '引用相关实体' : '一跳关联实体',
                    title: selected.item.canonical_name,
                    detail: selected.item.entity_type?.label || selected.item.entity_type?.key || '实体',
                    meta: `${relationCount} 条直接关系`,
                });
            }
            const source = graph.nodes.find((node) => node.id === selected.item.source_entity_id);
            const target = graph.nodes.find((node) => node.id === selected.item.target_entity_id);
            return Object.freeze({
                kind: '已发布关系',
                title: chatGraphRelationLabel(selected.item),
                detail: `${source?.canonical_name || '未知实体'} → ${target?.canonical_name || '未知实体'}`,
                meta: selected.item.relation_type?.direction === 'directed' ? '有向关系' : '无向关系',
            });
        }

        function citationGraphNodes() {
            return [...(citationGraphDialog.value.data?.graph?.nodes || [])].sort((left, right) => (
                left.depth - right.depth
                || String(left.entity_type?.label || '').localeCompare(String(right.entity_type?.label || ''), 'zh-CN')
                || String(left.canonical_name || '').localeCompare(String(right.canonical_name || ''), 'zh-CN')
            ));
        }

        function citationGraphRelations() {
            return [...(citationGraphDialog.value.data?.graph?.relations || [])].sort((left, right) => (
                citationGraphRelationLine(left).localeCompare(citationGraphRelationLine(right), 'zh-CN')
            ));
        }

        function citationGraphNodeName(id) {
            return citationGraphDialog.value.data?.graph?.nodes
                ?.find((node) => node.id === id)?.canonical_name || '未知实体';
        }

        function citationGraphRelationLine(relation) {
            const label = chatGraphRelationLabel(relation);
            return `${citationGraphNodeName(relation?.source_entity_id)} ${label} ${citationGraphNodeName(relation?.target_entity_id)}`;
        }

        function handleCitationClick(message, event) {
            const index = extractCitationIndex(event.target);
            if (index === null) return;
            openCitationChunk(citationSources(message)[index]);
        }

        function handleCitationKeydown(message, event) {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            const index = extractCitationIndex(event.target);
            if (index === null) return;
            event.preventDefault();
            openCitationChunk(citationSources(message)[index]);
        }

        async function openDocDetail(source) {
            if (!source) return;
            const slug = currentSlug.value;
            if (!slug) { ElMessage.warning('请先选择知识库'); return; }
            const docId = source.document_id;
            const chunkId = source.chunk_id;
            if (!docId || !chunkId) {
                ElMessage.warning('该来源缺少文档标识');
                return;
            }
            const requestSeq = ++sourceLocationRequestSeq;
            sourceLocationDialog.value = { open: true, loading: true, source, data: null, error: '' };
            try {
                const data = await api.getDocumentSource(slug, docId, chunkId);
                if (requestSeq !== sourceLocationRequestSeq) return;
                sourceLocationDialog.value = { open: true, loading: false, source, data, error: '' };
            } catch (e) {
                if (requestSeq !== sourceLocationRequestSeq) return;
                sourceLocationDialog.value = {
                    open: true,
                    loading: false,
                    source,
                    data: { legacy: true, fallback_chunk: source.content || '' },
                    error: e.message || '来源定位失败',
                };
            }
        }

        function sourceWindowParts() {
            return highlightSourceWindow(sourceLocationDialog.value.data || {});
        }

        function formatLocation(location) {
            if (!location) return '未记录位置';
            if (location.type === 'page') return `第 ${location.page} 页`;
            if (location.type === 'sheet_row') return `${location.sheet || 'Sheet'} 第 ${location.start_row || '?'}-${location.end_row || '?'} 行`;
            if (location.type === 'csv_row') return `CSV 第 ${location.row} 行`;
            if (location.type === 'line') return `第 ${location.start_line || '?'}-${location.end_line || '?'} 行`;
            if (location.type === 'table') return location.heading ? `表格：${location.heading}` : '表格';
            if (location.type === 'paragraph') return location.heading ? `段落：${location.heading}` : '段落';
            return Object.entries(location).map(([k, v]) => `${k}: ${v}`).join('，');
        }

        async function send() {
            const q = (input.value || '').trim();
            if (!currentSlug.value) { ElMessage.warning('请先选择一个知识库'); return; }
            if (!q) { ElMessage.warning('请输入问题'); return; }
            if (loading.value) return;

            const userTime = nowISO();
            messages.value.push({ role: 'user', text: q, time: userTime });
            input.value = '';
            const aiMsg = { role: 'ai', text: '', sources: [], graph_augmented: false, graph_evidence: [], error: false, cursor: false, statusText: '正在检索资料…', time: null };
            messages.value.push(aiMsg);
            _aiMsgRef = aiMsg;
            loading.value = true;
            scrollToBottom();
            if (_abortController) { _abortController.abort(); }
            _abortController = new AbortController();
            const payload = { library_slug: currentSlug.value, query: q, top_k: topK.value, show_debug: false };
            if (currentConvId.value) payload.conversation_id = currentConvId.value;
            try {
                await api.streamChatMessage(payload, {
                    onSources: (o) => {
                        if (o.conversation_id) currentConvId.value = o.conversation_id;
                        aiMsg.sources = o.sources || [];
                        aiMsg.graph_augmented = o.graph_augmented === true;
                        aiMsg.graph_evidence = o.graph_evidence || [];
                        aiMsg.statusText = '正在生成答案…';
                        aiMsg.cursor = true;
                        scrollToBottom();
                    },
                    onDelta: (t) => {
                        _enqueueDelta(t);
                    },
                    onError: (msg) => {
                        _flushPending();
                        _cleanupStream();
                        aiMsg.error = true;
                        aiMsg.cursor = false;
                        aiMsg.statusText = '';
                        aiMsg.text = (aiMsg.text ? aiMsg.text + '\n\n' : '') + '[生成中断] ' + msg;
                    },
                    onDone: () => {
                        _flushPending();
                        _aiMsgRef = null;
                        aiMsg.cursor = false;
                        aiMsg.statusText = '';
                        aiMsg.time = nowISO();
                    },
                }, _abortController.signal);
                loadConversations();
            } catch (e) {
                if (e.name === 'AbortError') { _abortController = null; return; }
                _abortController = null;
                _cleanupStream();
                aiMsg.error = true;
                aiMsg.cursor = false;
                aiMsg.statusText = '';
                if (e.status === 503) {
                    const detail = e.message || '';
                    if (detail.includes('未启用') || detail.toLowerCase().includes('chat')) {
                        chatDisabled.value = true;
                        aiMsg.text = '智能问答功能未启用，请联系管理员';
                    } else if (detail.toLowerCase().includes('rebuilding')) {
                        aiMsg.text = '知识库正在重建或索引暂不可用，请稍后再试';
                    } else {
                        aiMsg.text = '服务暂不可用：' + (detail || '请稍后再试');
                    }
                } else if (e.status === 409) {
                    aiMsg.text = '该会话已归档或删除，请点「新建会话」重新开始';
                } else if (e.status === 403) {
                    aiMsg.text = '你没有该知识库或该会话的访问权限';
                } else {
                    aiMsg.text = '生成答案失败：' + (e.message || '未知错误');
                }
            } finally {
                loading.value = false;
                scrollToBottom();
            }
        }

        onMounted(() => { Promise.all([loadLibs(), loadConversations()]); });
        onBeforeUnmount(() => {
            _cleanupStream();
            closeAndInvalidateSourceDialog();
            closeAndInvalidateCitationGraph();
        });

        return {
            libs, currentSlug, conversations, convsForLib, currentConvId, messages, input,
            loadingEarlierMessages, hasEarlierMessages,
            loading, chatDisabled, streamRef, mobileHistoryOpen, topK,
            onLibChange, newChat, selectConversation, loadEarlierMessages, archiveConv, deleteConv, send, copyAnswer,
            copySourceText, openCitationChunk, handleCitationClick, handleCitationKeydown,
            openDocDetail, loadLibs, chatWelcome, recalledChunkDialog, sourceLocationDialog,
            citationGraphDialog, openCitationGraph, closeAndInvalidateCitationGraph,
            selectCitationGraphNode, selectCitationGraphRelation, clearCitationGraphSelection,
            citationGraphSelection, citationGraphExpansionStatus, messageGraphSource,
            citationSources,
            expandingGraphEntityId, expandedGraphEntityIdList,
            citationGraphNodes, citationGraphRelations, citationGraphNodeName, citationGraphRelationLine,
            focusCitationGraphNode, chatGraphRelationLabel,
            closeAndInvalidateSourceDialog,
            sourceWindowParts, formatLocation,
            fmtScore, fmtTime, renderMarkdown,
        };
    },
    template: `
    <div class="chat-wrap" :class="{ 'is-history-open': mobileHistoryOpen }">
        <!-- Left: conversation history card -->
        <aside class="chat-history-panel">
            <div class="chat-history-title-row">
                <span class="chat-history-panel-title">会话历史</span>
                <el-button class="chat-new-button" circle
                           aria-label="新建会话" title="新建会话" @click="newChat">
                    <local-icon icon="mdi:plus"></local-icon>
                </el-button>
            </div>
            <div class="chat-history-list">
                <el-empty v-if="!convsForLib.length" description="暂无历史会话" :image-size="40" />
                <div v-for="c in convsForLib" :key="c.id"
                     class="chat-history-item"
                     :class="{ 'is-active': c.id === currentConvId }"
                     @click="selectConversation(c)">
                    <div class="chat-history-item-body">
                        <div class="chat-history-item-title">{{ c.title }}</div>
                        <div class="chat-history-item-time">{{ fmtTime(c.updated_at) }}</div>
                    </div>
                    <div class="chat-history-item-actions">
                        <local-icon icon="mdi:archive-arrow-down-outline" class="chat-history-action" title="归档"
                                    @click.stop="archiveConv(c)"></local-icon>
                        <local-icon icon="mdi:trash-can-outline" class="chat-history-action chat-history-action--danger" title="删除"
                                    @click.stop="deleteConv(c)"></local-icon>
                    </div>
                </div>
            </div>
        </aside>

        <!-- Right: Q&A area card -->
        <section class="chat-main">
            <!-- Toolbar -->
            <div class="chat-toolbar">
                <el-button class="chat-history-toggle" text
                           :aria-label="mobileHistoryOpen ? '关闭会话历史' : '打开会话历史'"
                           @click="mobileHistoryOpen = !mobileHistoryOpen">
                    <local-icon icon="mdi:history"></local-icon>
                    会话历史
                </el-button>
                <div class="chat-toolbar-right">
                    <span class="chat-toolbar-label">知识库</span>
                    <el-select v-model="currentSlug" placeholder="选择知识库" size="default"
                               class="chat-library-select" @change="onLibChange">
                        <el-option v-for="l in libs" :key="l.slug" :label="l.name" :value="l.slug" />
                    </el-select>
                    <el-button class="chat-refresh-btn app-refresh-button" text @click="loadLibs(true)">
                      <span class="app-refresh-icon" aria-hidden="true"></span>刷新
                    </el-button>
                </div>
            </div>

            <el-alert v-if="chatDisabled" type="warning" :closable="false" show-icon
                      title="智能问答功能未启用" style="margin: 0 18px 16px" />

            <!-- Messages -->
            <div ref="streamRef" class="chat-messages">
                <div v-if="hasEarlierMessages" class="chat-load-earlier">
                    <el-button text :loading="loadingEarlierMessages" @click="loadEarlierMessages">加载更早消息</el-button>
                </div>
                <div v-if="!messages.length" class="chat-empty">
                    <img :src="chatWelcome" class="illustration-chat-welcome" alt="" aria-hidden="true" />
                    <div class="chat-empty-title">智能知识问答</div>
                    <div class="chat-empty-desc">基于知识库内容，AI 将检索相关资料并生成答案</div>
                </div>
                <div v-for="(m, i) in messages" :key="m.id || i" class="chat-message-row"
                     :class="m.role === 'user' ? 'chat-message--user' : 'chat-message--ai'">
                    <div v-if="m.role === 'ai'" class="chat-avatar chat-avatar--ai">AI</div>

                    <div class="chat-message-content">
                        <div class="chat-bubble" :class="{
                            'chat-bubble--user': m.role === 'user',
                            'chat-bubble--ai': m.role === 'ai',
                            'chat-bubble--error': m.error,
                        }">
                            <div v-if="m.statusText" class="chat-status">{{ m.statusText }}</div>
                            <div v-if="m.role === 'ai'" class="chat-ai-label">智能助手</div>
                            <div v-if="m.role === 'user'" class="chat-user-text">{{ m.text }}</div>
                            <div v-if="m.role === 'ai'" class="chat-markdown"
                                 @click="handleCitationClick(m, $event)"
                                 @keydown="handleCitationKeydown(m, $event)"
                                 v-html="renderMarkdown(m.text, citationSources(m)) + (m.cursor ? '<span class=\\'chat-cursor\\'>|</span>' : '')"></div>
                            <div v-if="m.role === 'ai' && m.time" class="chat-msg-time">{{ fmtTime(m.time) }}</div>
                            <div v-if="m.role === 'ai' && m.text" class="chat-answer-actions">
                                <el-button v-if="m.time && messageGraphSource(m)" class="chat-answer-graph"
                                           plain size="small" type="primary" title="查看回答引用在知识图谱中的位置"
                                           @click="openCitationGraph(messageGraphSource(m))">
                                    <span class="chat-knowledge-graph-icon" aria-hidden="true"></span>
                                    <span>查看知识图谱</span>
                                </el-button>
                                <el-button class="chat-copy-answer" text aria-label="复制回答" title="复制回答" @click="copyAnswer(m.text)">
                                    <local-icon icon="mdi:content-copy"></local-icon>
                                </el-button>
                            </div>
                        </div>

                        <section v-if="m.role === 'ai' && m.graph_augmented && m.graph_evidence?.length"
                                 class="chat-graph-evidence">
                            <header>
                                <span class="chat-graph-badge"><span class="chat-knowledge-graph-icon" aria-hidden="true"></span>知识图谱增强</span>
                                <span>关系证据（{{ m.graph_evidence.length }}）</span>
                            </header>
                            <button v-for="evidence in m.graph_evidence" :key="evidence.relation_id"
                                    type="button" class="chat-graph-evidence-row"
                                    @click="openDocDetail(evidence)">
                                <span class="chat-graph-citation">[{{ evidence.citation_index }}]</span>
                                <strong>{{ evidence.source_entity_name }}</strong>
                                <span>{{ evidence.relation_label }}</span>
                                <strong>{{ evidence.target_entity_name }}</strong>
                                <small>{{ evidence.title || '查看关系原文' }}</small>
                            </button>
                        </section>

                        <!-- Collapsible sources -->
                        <el-collapse v-if="m.role === 'ai' && m.sources && m.sources.length" class="chat-sources">
                            <el-collapse-item>
                                <template #title>
                                    <span class="chat-sources-label">引用来源（{{ m.sources.length }}）</span>
                                </template>
                                <div v-for="(s, si) in m.sources" :key="si" class="chat-source-item">
                                    <div class="chat-source-left">
                                        <span class="chat-source-num">{{ si + 1 }}.</span>
                                        <span class="chat-source-title" :title="s.title || '(无标题)'">{{ s.title || '(无标题)' }}</span>
                                        <span class="chat-source-score">{{ fmtScore(s) }}</span>
                                        <div class="chat-source-summary">{{ s.content || '' }}</div>
                                    </div>
                                    <div class="chat-source-right">
                                        <el-button class="chat-source-detail" link type="primary" @click="openDocDetail(s)">查看出处</el-button>
                                    </div>
                                </div>
                            </el-collapse-item>
                        </el-collapse>
                    </div>

                    <div v-if="m.role === 'user'" class="chat-user-right">
                        <div class="chat-avatar chat-avatar--user">我</div>
                        <div v-if="m.time" class="chat-msg-time chat-msg-time--user">{{ fmtTime(m.time) }}</div>
                    </div>
                </div>
            </div>

            <el-dialog v-model="recalledChunkDialog.open" title="引用片段" width="620px" class="chat-recalled-dialog">
                <template v-if="recalledChunkDialog.source">
                    <div class="chat-source-dialog-meta">
                        <span>{{ recalledChunkDialog.source.title || '(无标题)' }}</span>
                        <span>{{ fmtScore(recalledChunkDialog.source) }}</span>
                        <span v-if="recalledChunkDialog.source.seq !== undefined">分片 {{ recalledChunkDialog.source.seq }}</span>
                    </div>
                    <pre class="chat-recalled-text">{{ recalledChunkDialog.source.content || '暂无引用内容' }}</pre>
                    <div class="chat-dialog-actions">
                        <el-button type="primary" plain @click="copySourceText(recalledChunkDialog.source.content)">复制片段</el-button>
                        <el-button v-if="recalledChunkDialog.source.chunk_id" type="primary"
                                   @click="openCitationGraph(recalledChunkDialog.source)">
                            <span class="chat-knowledge-graph-icon" aria-hidden="true"></span>
                            知识图谱
                        </el-button>
                    </div>
                </template>
            </el-dialog>

            <el-dialog v-model="citationGraphDialog.open" title="知识点图谱" width="1180px"
                       class="chat-citation-graph-dialog" @closed="closeAndInvalidateCitationGraph">
                <div v-if="citationGraphDialog.loading" class="chat-citation-graph-state">
                    <local-icon class="is-loading" icon="status:processing"></local-icon>
                    <span>正在加载已发布图谱...</span>
                </div>
                <el-alert v-else-if="citationGraphDialog.error" type="warning" :closable="false"
                          :title="citationGraphDialog.error" />
                <el-empty v-else-if="!citationGraphDialog.data?.graph"
                          description="该引用暂未关联已发布图谱" :image-size="64" />
                <template v-else>
                    <div class="chat-citation-graph-meta">
                        <div class="chat-citation-graph-origin">
                            <small>引用来源</small>
                            <strong :title="citationGraphDialog.source?.title || '(无标题)'">
                                {{ citationGraphDialog.source?.title || '(无标题)' }}
                            </strong>
                        </div>
                        <div class="chat-citation-graph-counts">
                            <span><strong>{{ citationGraphDialog.data.graph.counts.nodes }}</strong> 实体</span>
                            <span><strong>{{ citationGraphDialog.data.graph.counts.relations }}</strong> 关系</span>
                        </div>
                        <el-tag v-if="citationGraphDialog.data.exact_seeds_truncated" size="small" type="warning">
                            相关实体已按上限展示
                        </el-tag>
                    </div>
                    <div class="chat-citation-graph-workbench">
                        <section class="chat-citation-graph-stage">
                            <graph-canvas variant="citation"
                                          :graph="citationGraphDialog.data.graph"
                                          :selected-id="citationGraphDialog.selected?.item?.id || ''"
                                          :expanding-id="expandingGraphEntityId"
                                          :expanded-ids="expandedGraphEntityIdList"
                                          @open-entity="selectCitationGraphNode"
                                          @focus-entity="focusCitationGraphNode"
                                          @open-relation="selectCitationGraphRelation"
                                          @clear-selection="clearCitationGraphSelection" />
                        </section>
                        <aside class="chat-citation-graph-inspector">
                            <div v-if="citationGraphSelection()" class="chat-citation-graph-selection">
                                <small>{{ citationGraphSelection().kind }}</small>
                                <strong>{{ citationGraphSelection().title }}</strong>
                                <span>{{ citationGraphSelection().detail }}</span>
                                    <em>{{ citationGraphSelection().meta }}</em>
                                    <span v-if="citationGraphDialog.selected?.kind === 'entity' && citationGraphExpansionStatus(citationGraphDialog.selected.item.id)"
                                          class="chat-citation-graph-expansion-status">
                                        {{ citationGraphExpansionStatus(citationGraphDialog.selected.item.id) }}
                                    </span>
                            </div>
                            <section class="chat-citation-graph-facts">
                                <header><strong>关联事实</strong><span>{{ citationGraphRelations().length }}</span></header>
                                <button v-for="relation in citationGraphRelations()" :key="relation.id" type="button"
                                        :class="{ 'is-active': citationGraphDialog.selected?.item?.id === relation.id }"
                                        :title="citationGraphRelationLine(relation)"
                                        @click="selectCitationGraphRelation(relation)">
                                    <strong>{{ citationGraphNodeName(relation.source_entity_id) }}</strong>
                                    <span>{{ chatGraphRelationLabel(relation) }} →</span>
                                    <strong>{{ citationGraphNodeName(relation.target_entity_id) }}</strong>
                                </button>
                            </section>
                            <section class="chat-citation-graph-entities">
                                <header><strong>图中实体</strong><span>{{ citationGraphNodes().length }}</span></header>
                                <button v-for="node in citationGraphNodes()" :key="node.id" type="button"
                                        :class="{ 'is-active': citationGraphDialog.selected?.item?.id === node.id }"
                                        @click="selectCitationGraphNode(node)">
                                    <i :class="node.depth === 0 ? 'is-seed' : 'is-neighbor'"></i>
                                    <span><strong>{{ node.canonical_name }}</strong><small>{{ node.entity_type?.label || node.entity_type?.key || '实体' }}</small></span>
                                </button>
                            </section>
                        </aside>
                    </div>
                </template>
            </el-dialog>

            <el-dialog v-model="sourceLocationDialog.open" title="查看出处" width="900px" class="chat-source-location-dialog" @closed="closeAndInvalidateSourceDialog">
                <div v-if="sourceLocationDialog.loading" class="chat-source-loading">正在定位来源...</div>
                <template v-else>
                    <div class="chat-source-dialog-meta">
                        <span>{{ sourceLocationDialog.data?.document_title || sourceLocationDialog.source?.title || '(无标题)' }}</span>
                        <span v-if="sourceLocationDialog.data?.file_type">{{ sourceLocationDialog.data.file_type }}</span>
                        <span v-if="sourceLocationDialog.data?.location">{{ formatLocation(sourceLocationDialog.data.location) }}</span>
                        <span v-if="sourceLocationDialog.source">{{ fmtScore(sourceLocationDialog.source) }}</span>
                        <span v-if="sourceLocationDialog.source?.seq !== undefined">分片 {{ sourceLocationDialog.source?.seq }}</span>
                        <span v-else-if="sourceLocationDialog.data?.chunk_seq !== undefined">分片 {{ sourceLocationDialog.data.chunk_seq }}</span>
                    </div>
                    <el-alert v-if="sourceLocationDialog.error" type="warning" :closable="false" :title="sourceLocationDialog.error" />
                    <el-alert v-if="sourceLocationDialog.data?.legacy" type="info" :closable="false" title="该文档需重新导入后才能精确定位" />
                    <pre v-if="sourceLocationDialog.data?.legacy" class="chat-recalled-text">{{ sourceLocationDialog.data?.fallback_chunk || sourceLocationDialog.source?.content || '暂无引用内容' }}</pre>
                    <pre v-else class="chat-source-window"><template v-for="(part, pi) in sourceWindowParts().segments" :key="pi"><mark v-if="part.highlight" class="chat-source-highlight">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></pre>
                </template>
            </el-dialog>

            <!-- Input area: unified editor -->
            <div class="chat-input-bar">
                <div class="chat-input-shell">
                    <el-input v-model="input" type="textarea" :rows="2" resize="none"
                              :placeholder="messages.length ? '继续提问……' : '输入问题……'"
                              :maxlength="2000"
                              @keydown.enter.exact.prevent="send"
                              class="chat-input" />
                    <div class="chat-input-footer">
                        <div class="chat-input-footer-left">
                            <span class="chat-input-hint">{{ (input || '').length }} / 2000</span>
                            <el-tooltip content="结果不足时可适当提高，数值越大检索范围越广。" placement="top">
                                <span class="chat-input-hint">检索数量（Top K）</span>
                            </el-tooltip>
                            <el-select v-model="topK" size="small" class="chat-topk-select">
                                <el-option :value="3" label="3" />
                                <el-option :value="5" label="5" />
                                <el-option :value="10" label="10" />
                                <el-option :value="20" label="20" />
                            </el-select>
                        </div>
                        <el-button type="primary" :loading="loading"
                                   @click="send" class="chat-send-btn">
                            发送
                        </el-button>
                    </div>
                </div>
            </div>
        </section>
    </div>
    `,
};
