import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { marked } from '../../vendor/marked.esm.js';
import DOMPurify from '../../vendor/dompurify.es.mjs';
import { createStreamQueue } from '../stream_queue.js';
import { copyTextToClipboard } from '../copy_text.js';

// ── Markdown → safe HTML ──
function renderMarkdown(text) {
    if (!text) return '';
    const raw = marked.parse(text, { breaks: true, gfm: true });
    return DOMPurify.sanitize(raw, {
        ALLOWED_TAGS: ['h1','h2','h3','h4','h5','h6','p','br','strong','em','del','a',
                        'ul','ol','li','table','thead','tbody','tr','th','td',
                        'blockquote','pre','code','hr','sup','sub','span'],
        ALLOWED_ATTR: ['href','title','target','rel'],
        ALLOW_DATA_ATTR: false,
    });
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

function fmtScore(s) { return (Number(s || 0) * 100).toFixed(1) + '%'; }

function nowISO() { return new Date().toISOString(); }

export default {
    setup() {
        const libs = ref([]);
        const currentSlug = ref(null);
        const conversations = ref([]);
        const currentConvId = ref(null);
        const messages = ref([]);
        const input = ref('');
        const loading = ref(false);
        const chatDisabled = ref(false);
        const streamRef = ref(null);
        const mobileHistoryOpen = ref(false);
        const topK = ref(5);
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

        async function loadLibs() {
            try {
                libs.value = await api.listChatLibraries();
                if (libs.value.length && !currentSlug.value) currentSlug.value = libs.value[0].slug;
            } catch (e) { ElMessage.error(e.message); }
        }

        async function loadConversations() {
            try {
                conversations.value = await api.listChatConversations();
            } catch (e) { /* no-op */ }
        }

        function onLibChange() {
            _cleanupStream();
            currentConvId.value = null;
            messages.value = [];
        }

        function newChat() {
            _cleanupStream();
            currentConvId.value = null;
            messages.value = [];
        }

        async function scrollToBottom() {
            await nextTick();
            const el = streamRef.value;
            if (el) el.scrollTop = el.scrollHeight;
        }

        async function selectConversation(conv) {
            if (conv.id === currentConvId.value) return;
            _cleanupStream();
            currentSlug.value = conv.library_slug;
            currentConvId.value = conv.id;
            messages.value = [];
            try {
                const hist = await api.getChatConversationMessages(conv.id);
                messages.value = hist.map((m) => ({
                    role: m.role === 'assistant' ? 'ai' : 'user',
                    text: m.content + (m.status === 'failed' && m.error_message ? '\n\n*[失败]* ' + m.error_message : ''),
                    sources: m.sources || [],
                    error: m.status === 'failed',
                    time: m.created_at || null,
                }));
                scrollToBottom();
            } catch (e) { ElMessage.error(e.message); }
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

        function openDocDetail(source) {
            if (!source) return;
            const slug = currentSlug.value;
            if (!slug) { ElMessage.warning('请先选择知识库'); return; }
            const docId = source.document_id || source.chunk_id;
            if (docId) {
                const url = `/console/#/documents?slug=${encodeURIComponent(slug)}&open=${encodeURIComponent(docId)}`;
                window.open(url, '_blank');
            } else {
                ElMessage.warning('该来源缺少文档标识');
            }
        }

        async function send() {
            const q = (input.value || '').trim();
            if (!currentSlug.value) { ElMessage.warning('请先选择一个知识库'); return; }
            if (!q) { ElMessage.warning('请输入问题'); return; }
            if (loading.value) return;

            const userTime = nowISO();
            messages.value.push({ role: 'user', text: q, time: userTime });
            input.value = '';
            const aiMsg = { role: 'ai', text: '', sources: [], loading: true, error: false, cursor: false, statusText: '正在检索资料…', time: null };
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
                        aiMsg.statusText = '正在生成答案…';
                        aiMsg.cursor = true;
                        scrollToBottom();
                    },
                    onDelta: (t) => {
                        aiMsg.loading = false;
                        _enqueueDelta(t);
                    },
                    onError: (msg) => {
                        _flushPending();
                        _cleanupStream();
                        aiMsg.loading = false;
                        aiMsg.error = true;
                        aiMsg.cursor = false;
                        aiMsg.statusText = '';
                        aiMsg.text = (aiMsg.text ? aiMsg.text + '\n\n' : '') + '[生成中断] ' + msg;
                    },
                    onDone: () => {
                        _flushPending();
                        _aiMsgRef = null;
                        aiMsg.loading = false;
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
                        aiMsg.text = 'Chat 功能未启用，请管理员配置 CHAT_*';
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
                aiMsg.loading = false;
                loading.value = false;
                scrollToBottom();
            }
        }

        onMounted(async () => { await loadLibs(); await loadConversations(); });
        onBeforeUnmount(() => { _cleanupStream(); });

        return {
            libs, currentSlug, conversations, convsForLib, currentConvId, messages, input,
            loading, chatDisabled, streamRef, mobileHistoryOpen, topK,
            onLibChange, newChat, selectConversation, archiveConv, deleteConv, send, copyAnswer,
            openDocDetail, loadLibs,
            fmtScore, fmtTime, renderMarkdown,
        };
    },
    template: `
    <div class="chat-wrap" :class="{ 'is-history-open': mobileHistoryOpen }">
        <!-- Left: conversation history card -->
        <aside class="chat-history-panel">
            <div class="chat-history-title-row">
                <span class="chat-history-panel-title">会话历史</span>
                <el-button class="chat-new-button" circle :disabled="!currentSlug"
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
                    <el-button class="chat-refresh-btn" text title="刷新" @click="loadLibs">
                        <local-icon icon="mdi:database-import-outline"></local-icon>
                    </el-button>
                </div>
            </div>

            <el-alert v-if="chatDisabled" type="warning" :closable="false" show-icon
                      title="Chat 功能未启用" style="margin: 0 18px 16px" />

            <!-- Messages -->
            <div ref="streamRef" class="chat-messages">
                <div v-if="!messages.length" class="chat-empty">
                    <local-icon icon="carbon:chart-relationship" style="font-size:48px;color:var(--app-border);margin-bottom:16px"></local-icon>
                    <div class="chat-empty-title">智能知识问答</div>
                    <div class="chat-empty-desc">基于知识库内容，AI 将检索相关资料并生成答案</div>
                </div>
                <div v-for="(m, i) in messages" :key="i" class="chat-message-row"
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
                                 v-html="renderMarkdown(m.text) + (m.cursor ? '<span class=\\'chat-cursor\\'>|</span>' : '')"></div>
                            <div v-if="m.role === 'ai' && m.time" class="chat-msg-time">{{ fmtTime(m.time) }}</div>
                            <div v-if="m.role === 'ai' && m.text" class="chat-answer-actions">
                                <el-button class="chat-copy-answer" text aria-label="复制回答" title="复制回答" @click="copyAnswer(m.text)">
                                    <local-icon icon="mdi:content-copy"></local-icon>
                                    复制
                                </el-button>
                            </div>
                        </div>

                        <!-- Compact sources -->
                        <div v-if="m.role === 'ai' && m.sources && m.sources.length" class="chat-sources">
                            <div class="chat-sources-label">引用来源（{{ m.sources.length }}）</div>
                            <div v-for="(s, si) in m.sources" :key="si" class="chat-source-item">
                                <div class="chat-source-left">
                                    <span class="chat-source-num">{{ si + 1 }}.</span>
                                    <span class="chat-source-title" :title="s.title || '(无标题)'">{{ s.title || '(无标题)' }}</span>
                                    <span class="chat-source-score">{{ fmtScore(s.score) }}</span>
                                    <div class="chat-source-summary">{{ s.content || '' }}</div>
                                </div>
                                <div class="chat-source-right">
                                    <el-button class="chat-source-detail" link type="primary" @click="openDocDetail(s)">文档详情</el-button>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div v-if="m.role === 'user'" class="chat-user-right">
                        <div class="chat-avatar chat-avatar--user">我</div>
                        <div v-if="m.time" class="chat-msg-time chat-msg-time--user">{{ fmtTime(m.time) }}</div>
                    </div>
                </div>
            </div>

            <!-- Input area: unified editor -->
            <div class="chat-input-bar">
                <div class="chat-input-shell">
                    <el-input v-model="input" type="textarea" :rows="3" resize="none"
                              placeholder="继续提问，或输入问题..."
                              :maxlength="2000"
                              @keydown.enter.exact.prevent="send"
                              class="chat-input" />
                    <div class="chat-input-footer">
                        <div class="chat-input-footer-left">
                            <span class="chat-input-hint">{{ (input || '').length }} / 2000</span>
                            <span class="chat-input-hint">检索数量</span>
                            <el-select v-model="topK" size="small" class="chat-topk-select">
                                <el-option :value="3" label="3" />
                                <el-option :value="5" label="5" />
                                <el-option :value="10" label="10" />
                                <el-option :value="20" label="20" />
                            </el-select>
                        </div>
                        <el-button type="primary" :loading="loading" :disabled="!currentSlug"
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
