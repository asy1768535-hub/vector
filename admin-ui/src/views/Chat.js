import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { marked } from '../../vendor/marked.esm.js';
import DOMPurify from '../../vendor/dompurify.es.mjs';
import { createStreamQueue } from '../stream_queue.js';
import { copyTextToClipboard } from '../copy_text.js';

// ── Markdown → 安全 HTML（净化 script/事件属性/javascript: URL 等 XSS） ──
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

export default {
    setup() {
        const libs = ref([]);
        const currentSlug = ref(null);
        const conversations = ref([]);
        const currentConvId = ref(null);
        const messages = ref([]);          // {role:'user'|'ai', text, sources?, error?, cursor?, statusText?}
        const input = ref('');
        const loading = ref(false);
        const chatDisabled = ref(false);
        const streamRef = ref(null);
        const mobileHistoryOpen = ref(false);
        let _abortController = null;   // 切换会话/新建/卸载时取消旧请求

        // ── 流式队列：RAF 逐帧刷新，避免一次性大段渲染 ──
        const _queue = createStreamQueue();
        let _rafId = null;
        let _aiMsgRef = null;             // 当前正在生成的 aiMsg（直引 messages 中的对象）

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
            // done 时立即排空队列，确保最终内容完整（不能丢字）
            if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
            _flushDeltas();
        }

        function _cleanupStream() {
            // error / 切换 / 删除时清理，禁止内容串到其他消息
            if (_abortController) { _abortController.abort(); _abortController = null; }
            if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
            _queue.cleanup();
            _aiMsgRef = null;
        }

        // ── 当前库的会话（会话跨库，按库过滤展示） ──
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
            } catch (e) { /* 不阻断问答 */ }
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
            if (!value) {
                ElMessage.warning('暂无可复制内容');
                return;
            }
            try {
                await copyTextToClipboard(value);
                ElMessage.success('回答已复制');
            } catch (e) {
                ElMessage.error(e.message || '复制失败，请手动选择内容');
            }
        }

        async function send() {
            const q = (input.value || '').trim();
            if (!currentSlug.value) { ElMessage.warning('请先选择一个知识库'); return; }
            if (!q) { ElMessage.warning('请输入问题'); return; }
            if (loading.value) return;

            messages.value.push({ role: 'user', text: q });
            input.value = '';
            const aiMsg = { role: 'ai', text: '', sources: [], loading: true, error: false, cursor: false, statusText: '正在检索资料…' };
            messages.value.push(aiMsg);
            _aiMsgRef = aiMsg;
            loading.value = true;
            scrollToBottom();
            // 取消上一个未完成的请求，避免旧流写入当前会话
            if (_abortController) { _abortController.abort(); }
            _abortController = new AbortController();
            const payload = { library_slug: currentSlug.value, query: q, top_k: 5, show_debug: false };
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
                        // 先刷出已收到但未渲染的 delta，避免丢最后一段文字
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
                    },
                }, _abortController.signal);
                loadConversations();
            } catch (e) {
                if (e.name === 'AbortError') { _abortController = null; return; } // 用户主动切换，不显示错误
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

        function fmtScore(s) { return (Number(s || 0) * 100).toFixed(1) + '%'; }

        onMounted(async () => { await loadLibs(); await loadConversations(); });
        // 组件卸载时清理流式定时器，防止内存泄漏
        onBeforeUnmount(() => { _cleanupStream(); });

        return {
            libs, currentSlug, conversations, convsForLib, currentConvId, messages, input,
            loading, chatDisabled, streamRef, mobileHistoryOpen,
            onLibChange, newChat, selectConversation, archiveConv, deleteConv, send, copyAnswer, fmtScore,
            renderMarkdown,
        };
    },
    template: `
    <div class="chat-wrap" :class="{ 'is-history-open': mobileHistoryOpen }">
        <!-- 左侧：会话历史面板 -->
        <div class="chat-history-panel">
            <div class="chat-history-title-row">
                <div>
                    <div class="chat-history-panel-title">会话历史</div>
                    <div class="chat-history-panel-subtitle">最近的知识问答记录</div>
                </div>
                <el-button class="chat-new-button" circle :disabled="!currentSlug"
                           aria-label="新建会话" title="新建会话" @click="newChat">
                    <local-icon icon="mdi:plus"></local-icon>
                </el-button>
            </div>
            <div class="chat-history-list">
                <div class="chat-history-section-title" v-if="convsForLib.length">最近会话</div>
                <el-empty v-if="!convsForLib.length" description="暂无历史会话" :image-size="40" />
                <div v-for="c in convsForLib" :key="c.id"
                     class="chat-history-item"
                     :class="{ 'is-active': c.id === currentConvId }"
                     @click="selectConversation(c)">
                    <div class="chat-history-item-title">{{ c.title }}</div>
                    <div class="chat-history-item-actions">
                        <local-icon icon="mdi:archive-arrow-down-outline" class="chat-history-action" title="归档"
                                    @click.stop="archiveConv(c)"></local-icon>
                        <local-icon icon="mdi:trash-can-outline" class="chat-history-action chat-history-action--danger" title="删除"
                                    @click.stop="deleteConv(c)"></local-icon>
                    </div>
                </div>
            </div>
        </div>

        <!-- 右侧：问答主区 -->
        <div class="chat-main">
            <div class="chat-toolbar">
                <el-button class="chat-history-toggle" text
                           :aria-label="mobileHistoryOpen ? '关闭会话历史' : '打开会话历史'"
                           @click="mobileHistoryOpen = !mobileHistoryOpen">
                    <local-icon icon="mdi:history"></local-icon>
                    会话历史
                </el-button>
                <span class="chat-toolbar-label">知识库</span>
                <el-select v-model="currentSlug" placeholder="选择知识库" size="default"
                           class="chat-library-select" @change="onLibChange">
                    <el-option v-for="l in libs" :key="l.slug" :label="l.name" :value="l.slug" />
                </el-select>
            </div>

            <el-alert v-if="chatDisabled" type="warning" :closable="false" show-icon
                      title="Chat 功能未启用" style="margin: 0 18px 16px" />

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
                            <div v-if="m.role === 'user'" class="chat-user-text">{{ m.text }}</div>
                            <div v-if="m.role === 'ai'" class="chat-ai-label">智能助手</div>
                            <div v-if="m.role === 'ai'" class="chat-markdown"
                                 v-html="renderMarkdown(m.text) + (m.cursor ? '<span class=\\'chat-cursor\\'>|</span>' : '')"></div>
                            <div v-if="m.role === 'ai' && m.text" class="chat-answer-actions">
                                <el-button class="chat-copy-answer" text aria-label="复制回答"
                                           title="复制回答" @click="copyAnswer(m.text)">
                                    <local-icon icon="mdi:content-copy"></local-icon>
                                    复制
                                </el-button>
                            </div>
                        </div>
                        <el-collapse v-if="m.role === 'ai' && m.sources && m.sources.length" class="chat-sources">
                            <el-collapse-item :title="'引用来源（' + m.sources.length + '）'">
                                <div v-for="(s, si) in m.sources" :key="si" class="chat-source-item">
                                    <div class="chat-source-header">
                                        <span class="chat-source-num">[{{ si + 1 }}]</span>
                                        <span class="chat-source-title">{{ s.title || '(无标题)' }}</span>
                                        <el-tag size="small" :type="s.score >= 0.7 ? 'success' : s.score >= 0.5 ? 'warning' : 'info'">
                                            {{ fmtScore(s.score) }}
                                        </el-tag>
                                    </div>
                                    <div class="chat-source-content">{{ s.content }}</div>
                                </div>
                            </el-collapse-item>
                        </el-collapse>
                    </div>
                    <div v-if="m.role === 'user'" class="chat-avatar chat-avatar--user">我</div>
                </div>
            </div>

            <div class="chat-input-bar">
                <div class="chat-input-shell">
                    <el-input v-model="input" type="textarea" :rows="2" resize="none"
                              placeholder="继续提问，或输入问题..."
                              @keydown.enter.exact.prevent="send"
                              class="chat-input" />
                    <el-button type="primary" :loading="loading" :disabled="!currentSlug"
                               @click="send" class="chat-send-btn">
                        发送
                    </el-button>
                </div>
            </div>
        </div>
    </div>
    `,
};
