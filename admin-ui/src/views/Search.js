import { onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';
import { store } from '../store.js';
import { documentTypeIcon, documentDisplayName } from '../documents_ui.js';
import { searchEmpty } from '../illustrations.js';
import { csvEscape, downloadCSV } from '../logs_ui.js';

function formatScore(s) {
    return (Number(s || 0) * 100).toFixed(1) + '%';
}

function scoreType(s) {
    const v = Number(s || 0);
    if (v >= 0.7) return 'success';
    if (v >= 0.5) return 'warning';
    return 'info';
}

function resultDocInfo(row) {
    const m = row.metadata || {};
    return {
        title: row.title || m.title || null,
        external_id: m.external_id || null,
        id: row.document_id || null,
    };
}

export default {
    setup() {
        const router = useRouter();
        const libs = ref([]);
        const slug = ref(null);
        const query = ref('');
        const limit = ref(5);
        const results = ref([]);
        const loading = ref(false);
        const faqs = ref([]);
        const hasSearched = ref(false);
        const elapsed = ref(0);

        async function loadFaqs() {
            faqs.value = [];
            if (!slug.value) return;
            try {
                faqs.value = await api.listLibraryFaqs(slug.value);
            } catch (_) { faqs.value = []; }
        }

        function pickFaq(q) {
            query.value = q;
            handleSearch();
        }

        async function loadLibs() {
            try {
                if (store.user?.is_superuser) {
                    libs.value = (await api.listLibraries()).filter((l) => !l.deleted_at);
                } else {
                    libs.value = (store.permissions || [])
                        .filter((p) => (p.actions || []).includes('read'))
                        .map((p) => ({ slug: p.library_slug, name: p.library_name || p.library_slug }));
                }
                if (!slug.value && libs.value.length) slug.value = libs.value[0].slug;
            } catch (e) { ElMessage.error(e.message); }
        }

        async function handleSearch() {
            if (!slug.value) { ElMessage.warning('请先选择一个库'); return; }
            const q = (query.value || '').trim();
            if (!q) { ElMessage.warning('请输入搜索关键词'); return; }
            loading.value = true;
            hasSearched.value = true;
            elapsed.value = 0;
            const t0 = performance.now();
            try {
                const resp = await api.queryLibrary(slug.value, { query: q, limit: limit.value });
                elapsed.value = ((performance.now() - t0) / 1000);
                results.value = (resp.results || []).map((r) => {
                    const info = resultDocInfo(r);
                    return Object.assign(r, {
                        _docName: documentDisplayName(info),
                        _icon: documentTypeIcon(info),
                    });
                });
                if (!results.value.length) ElMessage.info('未找到相似分片');
            } catch (e) { ElMessage.error(e.message); }
            finally { loading.value = false; }
        }

        function resetSearch() {
            query.value = '';
            limit.value = 5;
            results.value = [];
            hasSearched.value = false;
            elapsed.value = 0;
        }

        function openDocDetail(row) {
            const docId = row.document_id;
            if (!docId) {
                ElMessage.warning('该结果无关联文档');
                return;
            }
            const href = router.resolve({
                path: '/knowledge-assets/documents',
                query: { slug: slug.value, open: docId },
            }).href;
            window.open(href, '_blank', 'noopener');
        }

        function exportCSV() {
            if (!results.value.length) return;
            const headers = ['文档名', '来源信息', '命中片段', '相似度', '重排分数'];
            const data = results.value.map((r) => [
                r._docName,
                (r.metadata?.page != null) ? `第${r.metadata.page}页` : '',
                r.text || '',
                formatScore(r.similarity),
                (r.metadata?.rerank_score != null) ? r.metadata.rerank_score.toFixed(4) : '',
            ]);
            downloadCSV(`search-${slug.value}-${new Date().toISOString().slice(0, 10)}.csv`, headers, data);
        }

        watch(slug, () => { results.value = []; hasSearched.value = false; elapsed.value = 0; loadFaqs(); });
        onMounted(loadLibs);

        return {
            libs, slug, query, limit, results, loading, faqs, hasSearched, elapsed,
            handleSearch, resetSearch, pickFaq, openDocDetail, exportCSV, searchEmpty,
            formatScore, scoreType, documentTypeIcon, documentDisplayName, resultDocInfo,
        };
    },
    template: `
    <div class="search-workspace">
        <!-- Card 1: Search form -->
        <section class="search-card">
            <el-form :inline="true" @submit.prevent="handleSearch">
                <el-form-item label="目标库">
                    <el-select v-model="slug" placeholder="选择知识库" class="search-lib-select">
                        <el-option v-for="l in libs" :key="l.slug"
                                   :label="l.name + ' (' + l.slug + ')'" :value="l.slug" />
                    </el-select>
                </el-form-item>
                <el-form-item label="关键词">
                    <el-input v-model="query" clearable placeholder="输入搜索关键词"
                              class="search-query-input" @keyup.enter="handleSearch" />
                </el-form-item>
                <el-form-item label="最大返回数">
                    <el-input-number v-model="limit" :min="1" :max="20" class="search-limit-input" />
                </el-form-item>
                <el-form-item>
                    <el-button type="primary" :loading="loading" @click="handleSearch">搜索</el-button>
                    <el-button @click="resetSearch">重置</el-button>
                </el-form-item>
            </el-form>
        </section>

        <!-- Card 2: FAQ -->
        <section v-if="faqs.length" class="search-faq-card">
            <span class="search-faq-label">常用问题：</span>
            <el-tag v-for="f in faqs" :key="f.id" effect="plain"
                    class="search-faq-tag" @click="pickFaq(f.question)">{{ f.question }}</el-tag>
        </section>

        <!-- Card 3: Results -->
        <section v-if="results.length || hasSearched" class="search-results-card">
            <div class="search-results-summary">
                <span class="search-results-count">返回 {{ results.length }} 条结果<span v-if="elapsed">，耗时 {{ elapsed.toFixed(1) }} 秒</span></span>
                <el-button :disabled="!results.length" @click="exportCSV">导出结果</el-button>
            </div>
            <div class="search-results-table-shell">
                <el-table :data="results" v-loading="loading">
                    <template #empty><div class="illustration-empty-wrapper"><img :src="searchEmpty" class="illustration-search-empty" alt="" aria-hidden="true" /><p>未找到匹配结果</p></div></template>
                    <el-table-column label="文档名" min-width="180">
                        <template #default="{row}">
                            <div class="search-doc-file">
                                <img v-if="row._icon"
                                     class="search-doc-file-icon"
                                     :src="row._icon"
                                     alt="" aria-hidden="true" />
                                <local-icon v-else class="search-doc-file-icon"
                                            icon="mdi:file-document-outline" />
                                <span class="search-doc-file-name"
                                      :title="row._docName">{{ row._docName }}</span>
                            </div>
                        </template>
                    </el-table-column>
                    <el-table-column label="来源信息" width="110" align="center">
                        <template #default="{row}">
                            <span>{{ (row.metadata && row.metadata.page != null) ? ('第' + row.metadata.page + '页') : '—' }}</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="命中片段" min-width="280">
                        <template #default="{row}">
                            <div class="search-chunk-text">{{ row.text }}</div>
                        </template>
                    </el-table-column>
                    <el-table-column label="相似度" width="100" align="center">
                        <template #default="{row}">
                            <el-tag :type="scoreType(row.similarity)" size="small"
                                    :class="'score--' + (row.similarity >= 0.7 ? 'high' : row.similarity >= 0.5 ? 'mid' : 'low')">
                                {{ formatScore(row.similarity) }}
                            </el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="重排分数" width="100" align="center">
                        <template #default="{row}">
                            <span>{{ (row.metadata && row.metadata.rerank_score != null) ? row.metadata.rerank_score.toFixed(4) : '—' }}</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="操作" width="100" align="center">
                        <template #default="{row}">
                            <el-button link class="search-doc-link" @click="openDocDetail(row)">文档详情</el-button>
                        </template>
                    </el-table-column>
                </el-table>
            </div>
        </section>

        <!-- Empty state -->
        <section v-if="!results.length && !hasSearched && !loading" class="search-empty-card">
            <img :src="searchEmpty" class="illustration-search-empty" alt="" aria-hidden="true" />
            <p class="search-empty-text">选择知识库并输入关键词开始检索</p>
        </section>
    </div>
    `,
};
