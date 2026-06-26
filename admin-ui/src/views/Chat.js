import { computed, nextTick, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';

export default {
    setup() {
        const libs = ref([]);
        const currentSlug = ref(null);
        const conversations = ref([]);
        const currentConvId = ref(null);
        const messages = ref([]);          // {role:'user'|'ai', text, sources?, error?}
        const input = ref('');
        const loading = ref(false);
        const chatDisabled = ref(false);
        const streamRef = ref(null);

        // 当前库的会话（会话跨库，按库过滤展示）
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
            // 切库 = 进入该库的新会话上下文
            currentConvId.value = null;
            messages.value = [];
        }

        function newChat() {
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
            currentSlug.value = conv.library_slug;
            currentConvId.value = conv.id;
            messages.value = [];
            try {
                const hist = await api.getChatConversationMessages(conv.id);
                messages.value = hist.map((m) => ({
                    role: m.role === 'assistant' ? 'ai' : 'user',
                    text: m.content + (m.status === 'failed' && m.error_message ? `\n\n[失败] ${m.error_message}` : ''),
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

        async function send() {
            const q = (input.value || '').trim();
            if (!currentSlug.value) { ElMessage.warning('请先选择一个知识库'); return; }
            if (!q) { ElMessage.warning('请输入问题'); return; }
            if (loading.value) return;

            messages.value.push({ role: 'user', text: q });
            input.value = '';
            const aiMsg = { role: 'ai', text: '', sources: [], loading: true, error: false };
            messages.value.push(aiMsg);
            loading.value = true;
            scrollToBottom();
            const payload = { library_slug: currentSlug.value, query: q, top_k: 5, show_debug: false };
            if (currentConvId.value) payload.conversation_id = currentConvId.value;
            try {
                await api.streamChatMessage(payload, {
                    onSources: (o) => {
                        if (o.conversation_id) currentConvId.value = o.conversation_id;
                        aiMsg.sources = o.sources || [];
                        scrollToBottom();
                    },
                    onDelta: (t) => { aiMsg.loading = false; aiMsg.text += t; scrollToBottom(); },
                    onError: (msg) => {
                        aiMsg.loading = false;
                        aiMsg.error = true;
                        aiMsg.text = (aiMsg.text ? aiMsg.text + '\n\n' : '') + '[生成中断] ' + msg;
                    },
                    onDone: () => { aiMsg.loading = false; },
                });
                loadConversations();          // 刷新侧栏（新会话标题/排序）
            } catch (e) {
                aiMsg.error = true;
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
        return {
            libs, currentSlug, conversations, convsForLib, currentConvId, messages, input,
            loading, chatDisabled, streamRef,
            onLibChange, newChat, selectConversation, archiveConv, deleteConv, send, fmtScore,
        };
    },
    template: `
    <div class="chat-wrap" style="display:flex;gap:16px;height:calc(100vh - 120px)">
        <!-- 左：库选择 + 会话列表 -->
        <el-card style="width:270px;flex:none;display:flex;flex-direction:column" body-style="display:flex;flex-direction:column;flex:1;min-height:0;padding:10px">
            <el-select v-model="currentSlug" placeholder="选择知识库" style="width:100%" @change="onLibChange">
                <el-option v-for="l in libs" :key="l.slug" :label="l.name" :value="l.slug" />
            </el-select>
            <el-button type="primary" plain style="width:100%;margin:10px 0" :disabled="!currentSlug" @click="newChat">
                + 新建会话
            </el-button>
            <div style="flex:1;overflow:auto">
                <el-empty v-if="!convsForLib.length" description="暂无历史会话" :image-size="48" />
                <div v-for="c in convsForLib" :key="c.id"
                     @click="selectConversation(c)"
                     :style="{
                        cursor:'pointer', padding:'8px 10px', borderRadius:'6px', marginBottom:'4px',
                        display:'flex', alignItems:'center', gap:'6px',
                        background: c.id === currentConvId ? 'var(--el-color-primary-light-9)' : 'transparent'
                     }">
                    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px">{{ c.title }}</span>
                    <iconify-icon icon="mdi:archive-arrow-down-outline" style="color:#909399" title="归档"
                                  @click.stop="archiveConv(c)"></iconify-icon>
                    <iconify-icon icon="mdi:trash-can-outline" style="color:var(--el-color-danger)" title="删除"
                                  @click.stop="deleteConv(c)"></iconify-icon>
                </div>
            </div>
        </el-card>

        <!-- 右：聊天区 -->
        <el-card style="flex:1;display:flex;flex-direction:column;min-width:0" body-style="display:flex;flex-direction:column;flex:1;min-height:0;padding:0">
            <el-alert v-if="chatDisabled" type="warning" :closable="false" show-icon
                      title="Chat 功能未启用，请管理员在 .env 配置 CHAT_* 后重启服务" style="margin:0" />
            <div ref="streamRef" style="flex:1;overflow:auto;padding:16px">
                <el-empty v-if="!messages.length" description="选择知识库，输入问题开始问答" />
                <div v-for="(m, i) in messages" :key="i"
                     :style="{ display:'flex', justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start', marginBottom:'14px' }">
                    <div :style="{ maxWidth:'78%' }">
                        <div :style="{
                                padding:'10px 14px', borderRadius:'10px', whiteSpace:'pre-wrap', wordBreak:'break-word',
                                background: m.role === 'user' ? 'var(--el-color-primary)' : 'var(--el-fill-color-light)',
                                color: m.role === 'user' ? '#fff' : 'var(--el-text-color-primary)'
                             }">
                            <span v-if="m.loading">正在检索并生成答案...</span>
                            <span v-else :style="{ color: m.error ? 'var(--el-color-danger)' : '' }">{{ m.text }}</span>
                        </div>
                        <el-collapse v-if="m.role === 'ai' && m.sources && m.sources.length" style="margin-top:8px">
                            <el-collapse-item :title="'引用来源（' + m.sources.length + '）'">
                                <div v-for="(s, si) in m.sources" :key="si"
                                     style="padding:8px 10px;margin-bottom:8px;border:1px solid var(--el-border-color-lighter);border-radius:6px">
                                    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
                                        <span style="color:#909399">[{{ si + 1 }}]</span>
                                        <span style="font-weight:600">{{ s.title || '(无标题)' }}</span>
                                        <el-tag size="small" :type="s.score >= 0.7 ? 'success' : s.score >= 0.5 ? 'warning' : 'info'">
                                            {{ fmtScore(s.score) }}
                                        </el-tag>
                                    </div>
                                    <div style="font-size:13px;color:var(--el-text-color-regular);white-space:pre-wrap;word-break:break-word">{{ s.content }}</div>
                                </div>
                            </el-collapse-item>
                        </el-collapse>
                    </div>
                </div>
            </div>
            <div style="border-top:1px solid var(--el-border-color-lighter);padding:12px;display:flex;gap:10px;align-items:flex-end">
                <el-input v-model="input" type="textarea" :rows="2" resize="none"
                          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
                          @keydown.enter.exact.prevent="send" />
                <el-button type="primary" :loading="loading" :disabled="!currentSlug" @click="send" style="height:54px">发送</el-button>
            </div>
        </el-card>
    </div>
    `,
};
