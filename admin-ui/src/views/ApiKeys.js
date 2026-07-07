import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as api from '../api.js';
import { copyTextToClipboard } from '../copy_text.js';
import { dataEmpty, apiKeySecurity } from '../illustrations.js';
import {
    formatKeyTime, keyStatus, STATUS_LABEL, STATUS_TAG,
    computeStats, paginateKeys,
} from '../api_keys_ui.js';

export default {
    setup() {
        const keys = ref([]);
        const loading = ref(false);
        const dialog = reactive({ open: false, name: '', expiresAt: '' });
        const submitting = ref(false);
        const result = reactive({ open: false, plaintext: '' });
        const docDialog = reactive({ open: false });
        const page = ref(1);
        const pageSize = 10;

        // ── Derived ──────────────────────────────────────────
        const stats = computed(() => computeStats(keys.value));
        const pagination = computed(() => paginateKeys(keys.value, page.value, pageSize));
        const visibleKeys = computed(() => pagination.value.items);
        const showPagination = computed(() => pagination.value.total > pageSize);

        // ── Data loading ─────────────────────────────────────
        async function load(forceRefresh = false) {
            loading.value = true;
            try {
                keys.value = await api.listApiKeys(forceRefresh);
                if (pagination.value.page !== page.value) {
                    page.value = pagination.value.page;
                }
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        // ── Create ───────────────────────────────────────────
        function openCreate() {
            dialog.name = '';
            dialog.expiresAt = '';
            dialog.open = true;
        }

        async function submit() {
            const name = (dialog.name || '').trim();
            if (!name) { ElMessage.warning('请输入 Key 名称'); return; }
            const expiresAt = dialog.expiresAt ? new Date(dialog.expiresAt).toISOString() : null;
            if (expiresAt && new Date(expiresAt).getTime() <= Date.now()) {
                ElMessage.warning('过期时间必须晚于当前时间');
                return;
            }
            submitting.value = true;
            try {
                const resp = await api.createApiKey(name, expiresAt);
                dialog.open = false;
                result.plaintext = resp.plaintext_key;
                result.open = true;
                page.value = 1;
                await load();
            } catch (e) { ElMessage.error(e.message); }
            finally { submitting.value = false; }
        }

        function closeResult() {
            result.open = false;
            result.plaintext = '';
        }

        // ── Copy ─────────────────────────────────────────────
        async function copyPlain() {
            const value = (result.plaintext || '').trim();
            if (!value) { ElMessage.warning('暂无可复制内容'); return; }
            try {
                await copyTextToClipboard(value);
                ElMessage.success('已复制到剪贴板');
            } catch (e) { ElMessage.error(e.message || '复制失败，请手动选择内容'); }
        }

        function openApiDoc() {
            docDialog.open = true;
        }

        // ── Revoke ───────────────────────────────────────────
        async function revoke(row) {
            try {
                await ElMessageBox.confirm(
                    `撤销 "${row.name}" (${row.key_prefix}…)？此操作不可恢复。`,
                    '确认撤销', { type: 'warning' }
                );
            } catch (_) { return; }
            try {
                await api.revokeApiKey(row.id);
                ElMessage.success('已撤销');
                await load();
            } catch (e) { ElMessage.error(e.message || String(e)); }
        }

        onMounted(load);

        return {
            keys, loading, dialog, submitting, result, docDialog, page, pageSize,
            stats, pagination, visibleKeys, showPagination,
            load, openCreate, submit, closeResult, copyPlain, openApiDoc, revoke, dataEmpty, apiKeySecurity,
            formatKeyTime, keyStatus, STATUS_LABEL, STATUS_TAG,
        };
    },
    template: `
    <div class="api-keys-workspace">
        <!-- Header -->
        <div class="api-keys-header">
            <div>
                <h2 class="api-keys-title">我的 API Key</h2>
                <p class="api-keys-desc">管理您的 API 密钥，用于通过 API 访问知识库服务。</p>
            </div>
            <div class="api-keys-header-actions">
                <el-button @click="load(true)" :loading="loading">刷新</el-button>
                <el-button type="primary" @click="openCreate">新建 API Key</el-button>
            </div>
        </div>

        <!-- Security warning -->
        <el-alert type="warning" :closable="false" show-icon
                  title="安全提示"
                  description="API Key 拥有您账户的全部权限，请妥善保管。完整密钥仅在创建时显示一次，之后无法再次查看。" />

        <!-- API usage doc -->
        <section class="api-keys-doc-card">
            <div class="api-keys-doc-main">
                <h3>API 接入说明：检索知识库切片</h3>
                <p><code>/query</code> 返回召回切片，不是最终回答；如需最终回答，请调用方将切片交给自己的 LLM。</p>
                <p class="api-keys-doc-note">推荐把 <code>BASE_URL</code> / <code>LIBRARY_ID</code> / <code>API_KEY</code> 放到 <code>.env</code>，不要写死在前端或脚本源码里。</p>
                <ol class="api-keys-doc-steps">
                    <li>创建并保存 API Key</li>
                    <li>在 <code>.env</code> 配置 <code>VECTOR_KB_BASE_URL</code>、<code>VECTOR_KB_LIBRARY_ID</code>、<code>VECTOR_KB_API_KEY</code></li>
                    <li>调用 <code>POST /libraries/{LIBRARY_ID}/query</code> 获取 <code>results</code> 切片</li>
                </ol>
            </div>
            <div class="api-keys-doc-actions">
                <el-button class="api-keys-doc-template-button" @click="openApiDoc">快速接入模板</el-button>
            </div>
        </section>

        <!-- Stats cards -->
        <div class="api-keys-stats">
            <div class="api-keys-stat-card">
                <div class="api-keys-stat-num">{{ stats.total }}</div>
                <div class="api-keys-stat-label">全部</div>
            </div>
            <div class="api-keys-stat-card api-keys-stat-card--active">
                <div class="api-keys-stat-num">{{ stats.active }}</div>
                <div class="api-keys-stat-label">启用</div>
            </div>
            <div class="api-keys-stat-card api-keys-stat-card--revoked">
                <div class="api-keys-stat-num">{{ stats.revoked }}</div>
                <div class="api-keys-stat-label">已撤销</div>
            </div>
            <div class="api-keys-stat-card api-keys-stat-card--expired">
                <div class="api-keys-stat-num">{{ stats.expired }}</div>
                <div class="api-keys-stat-label">已过期</div>
            </div>
        </div>

        <!-- Table -->
        <section class="api-keys-table-card">
            <div class="api-keys-table-shell">
                <el-table :data="visibleKeys" v-loading="loading">
                    <template #empty><div class="illustration-empty-wrapper"><img :src="dataEmpty" class="illustration-data-empty" alt="" aria-hidden="true" /><p>暂无 API Key</p></div></template>
                    <el-table-column label="Key 名称" min-width="140" prop="name" show-overflow-tooltip />
                    <el-table-column label="Key 前缀" min-width="160">
                        <template #default="{row}">
                            <span class="api-keys-prefix">{{ row.key_prefix }}</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="创建时间" width="170">
                        <template #default="{row}">{{ formatKeyTime(row.created_at) }}</template>
                    </el-table-column>
                    <el-table-column label="最后使用" width="170">
                        <template #default="{row}">{{ formatKeyTime(row.last_used_at) }}</template>
                    </el-table-column>
                    <el-table-column label="过期时间" width="170">
                        <template #default="{row}">{{ formatKeyTime(row.expires_at) }}</template>
                    </el-table-column>
                    <el-table-column label="状态" width="90" align="center">
                        <template #default="{row}">
                            <el-tag :type="STATUS_TAG[keyStatus(row)]" size="small">{{ STATUS_LABEL[keyStatus(row)] }}</el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="操作" width="90" align="center">
                        <template #default="{row}">
                            <el-button link type="danger"
                                       :disabled="keyStatus(row) !== 'active'"
                                       @click="revoke(row)">撤销</el-button>
                        </template>
                    </el-table-column>
                </el-table>
            </div>
            <div v-if="showPagination" class="api-keys-pagination">
                <el-pagination v-model:current-page="page" :page-size="pageSize"
                               :total="pagination.total" layout="total, prev, pager, next" />
            </div>
        </section>


        <el-dialog v-model="docDialog.open" title="完整接入模板" width="860px" class="api-keys-doc-dialog">
            <div class="api-keys-doc-dialog-body">
                <section>
                    <h4>1. .env 配置示例</h4>
                    <p>普通用户可直接在这里复制接入模板；推荐把地址、知识库 ID 和 API Key 放在调用脚本同目录的 <code>.env</code>，不要写死在代码里。</p>
                    <pre class="api-keys-doc-code">VECTOR_KB_BASE_URL=http://10.0.10.2:8100
VECTOR_KB_LIBRARY_ID=deploy_acceptance_server
VECTOR_KB_API_KEY=你的完整API_KEY
LOCAL_LLM_BASE_URL=可选
LOCAL_LLM_API_KEY=可选
LOCAL_LLM_MODEL=可选</pre>
                </section>
                <section>
                    <h4>2. Python 完整接入脚本</h4>
                    <pre class="api-keys-doc-code">from pathlib import Path
from typing import Any
import os

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

VECTOR_KB_BASE_URL = os.getenv("VECTOR_KB_BASE_URL", "").rstrip("/")
VECTOR_KB_LIBRARY_ID = os.getenv("VECTOR_KB_LIBRARY_ID", "")
VECTOR_KB_API_KEY = os.getenv("VECTOR_KB_API_KEY", "")

LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "")
LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "")


def require_env(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"请在 .env 中配置 {name}")
    return value


def search_kb(question: str, limit: int = 5) -> list[dict[str, Any]]:
    base_url = require_env("VECTOR_KB_BASE_URL", VECTOR_KB_BASE_URL)
    library_id = require_env("VECTOR_KB_LIBRARY_ID", VECTOR_KB_LIBRARY_ID)
    api_key = require_env("VECTOR_KB_API_KEY", VECTOR_KB_API_KEY)

    resp = requests.post(
        f"{base_url}/libraries/{library_id}/query",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"query": question, "limit": limit},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def build_context(chunks: list[dict[str, Any]]) -> str:
    parts = []
    for index, chunk in enumerate(chunks, start=1):
        parts.append(
            f"[来源 {index}]\n"
            f"标题：{chunk.get('title') or '未知来源'}\n"
            f"相关性：{chunk.get('similarity')}\n"
            f"document_id：{chunk.get('document_id')}\n"
            f"chunk_id：{chunk.get('chunk_id')}\n"
            f"正文：\n{chunk.get('text') or ''}"
        )
    return "\n\n---\n\n".join(parts)


def call_your_llm(prompt: str) -> str:
    # 占位函数：请替换为你自己的大模型调用。
    # 建议从 .env 读取 LOCAL_LLM_BASE_URL / LOCAL_LLM_API_KEY / LOCAL_LLM_MODEL。
    # 不要把大模型 API Key 写死在代码里。
    raise NotImplementedError("请将 call_your_llm(prompt) 替换为自己的模型调用")


def ask(question: str, limit: int = 5, use_llm: bool = False):
    chunks = search_kb(question, limit=limit)

    if not use_llm:
        return chunks

    context = build_context(chunks)
    prompt = f"""请只根据以下知识库切片回答问题。若切片中没有答案，请说明无法从已给资料确认。

问题：{question}

知识库切片：
{context}
"""
    answer = call_your_llm(prompt)
    sources = [
        {
            "title": chunk.get("title"),
            "document_id": chunk.get("document_id"),
            "chunk_id": chunk.get("chunk_id"),
            "similarity": chunk.get("similarity"),
        }
        for chunk in chunks
    ]
    return {"answer": answer, "sources": sources}


if __name__ == "__main__":
    question = "你的问题"

    print("=== use_llm=False：只返回 results 切片 ===")
    chunks = ask(question, limit=5, use_llm=False)
    for item in chunks:
        print("来源文档：", item.get("title"))
        print("相关性：", item.get("similarity"))
        print("切片正文：", item.get("text"))
        print()

    # 替换 call_your_llm(prompt) 后，再打开下面两行：
    # print("=== use_llm=True：返回 answer + sources ===")
    # print(ask(question, limit=5, use_llm=True))</pre>
                </section>
                <section>
                    <h4>3. use_llm 两种模式</h4>
                    <ul class="api-keys-doc-list">
                        <li><code>use_llm=False</code>：只调用 <code>/query</code>，直接返回 <code>results</code> 切片。</li>
                        <li><code>use_llm=True</code>：先检索切片，再调用方自己执行 <code>call_your_llm(prompt)</code>，返回 <code>answer + sources</code>。</li>
                    </ul>
                    <p class="api-keys-doc-note"><code>call_your_llm</code> 是占位函数，需要用户替换为自己的大模型调用。</p>
                </section>
                <section>
                    <h4>4. JavaScript/Node 简版</h4>
                    <pre class="api-keys-doc-code">const VECTOR_KB_BASE_URL = process.env.VECTOR_KB_BASE_URL;
const VECTOR_KB_LIBRARY_ID = process.env.VECTOR_KB_LIBRARY_ID;
const VECTOR_KB_API_KEY = process.env.VECTOR_KB_API_KEY;

async function searchKb(question, limit = 5) {
  const response = await fetch(
    VECTOR_KB_BASE_URL + "/libraries/" + VECTOR_KB_LIBRARY_ID + "/query",
    {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + VECTOR_KB_API_KEY,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ query: question, limit })
    }
  );
  if (!response.ok) throw new Error("知识库请求失败：" + response.status);
  return (await response.json()).results || [];
}</pre>
                </section>
                <section>
                    <h4>5. curl 仅用于临时测试</h4>
                    <pre class="api-keys-doc-code">curl -X POST "$VECTOR_KB_BASE_URL/libraries/$VECTOR_KB_LIBRARY_ID/query" \
  -H "Authorization: Bearer $VECTOR_KB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"你的问题","limit":5}'</pre>
                </section>
                <section>
                    <h4>6. 响应字段说明</h4>
                    <ul class="api-keys-doc-list">
                        <li><code>results[].text</code>：召回切片正文</li>
                        <li><code>results[].similarity</code>：相似度/相关性分数</li>
                        <li><code>results[].document_id</code>：来源文档</li>
                        <li><code>results[].chunk_id</code>：来源切片</li>
                        <li><code>results[].title</code>：来源文档标题</li>
                    </ul>
                </section>
            </div>
        </el-dialog>

        <!-- Create dialog -->
        <el-dialog v-model="dialog.open" title="新建 API Key" width="440px" :close-on-click-modal="false">
            <el-form @submit.prevent="submit">
                <el-form-item label="Key 名称" required>
                    <el-input v-model="dialog.name" placeholder="例如：生产环境、测试环境" maxlength="64" />
                </el-form-item>
                <el-form-item label="过期时间">
                    <el-date-picker v-model="dialog.expiresAt" type="datetime"
                                    placeholder="可选，不填则永不过期"
                                    format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DDTHH:mm"
                                    :disabled-date="(d) => d < Date.now() - 86400000" />
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button @click="dialog.open = false">取消</el-button>
                <el-button type="primary" :loading="submitting" @click="submit">生成</el-button>
            </template>
        </el-dialog>

        <!-- Result card (replaces reveal dialog) -->
        <div v-if="result.open" class="api-keys-result-card">
            <div class="api-keys-result-left">
                <div class="api-keys-result-head">
                    <local-icon icon="mdi:key-variant" class="api-keys-result-icon"></local-icon>
                    <span class="api-keys-result-title">API Key 创建成功</span>
                </div>
                <p class="api-keys-result-warn">以下完整密钥仅显示一次，请立即复制并妥善保存。</p>
                <div class="api-keys-plaintext">{{ result.plaintext }}</div>
                <div class="api-keys-result-actions">
                    <el-button type="primary" @click="copyPlain">复制到剪贴板</el-button>
                    <el-button @click="closeResult">我已复制并保存</el-button>
                </div>
            </div>
            <div class="api-keys-result-right">
                <img :src="apiKeySecurity" class="illustration-api-key-security" alt="" aria-hidden="true" />
                <h4>使用方式</h4>
                <p>在 API 请求中通过 <code>Authorization: Bearer &lt;API Key&gt;</code> 头传递密钥。</p>
                <p>示例：</p>
                <pre class="api-keys-usage-code">curl -H "Authorization: Bearer YOUR_KEY"
     -H "Content-Type: application/json"
     -d '{"query":"你的问题","limit":5}'
     BASE_URL/libraries/LIBRARY_ID/query</pre>
            </div>
        </div>
    </div>
    `,
};
