import { onMounted, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import * as api from '../api.js';
import { store } from '../store.js';

export default {
    setup() {
        const libs = ref([]);
        const slug = ref(null);
        const query = ref('');
        const limit = ref(5);
        const results = ref([]);
        const loading = ref(false);
        const faqs = ref([]);            // 当前库的常用问题（active）

        // 选库后加载该库 active FAQ；失败/无权不影响检索，静默清空
        async function loadFaqs() {
            faqs.value = [];
            if (!slug.value) return;
            try {
                faqs.value = await api.listLibraryFaqs(slug.value);
            } catch (e) {
                faqs.value = [];
            }
        }
        watch(slug, loadFaqs);

        // 点击常用问题 → 填入查询框并立即检索
        function pickFaq(q) {
            query.value = q;
            handleSearch();
        }

        async function loadLibs() {
            try {
                if (store.user?.is_superuser) {
                    libs.value = (await api.listLibraries()).filter((l) => !l.deleted_at);
                } else {
                    libs.value = store.permissions
                        .filter((p) => p.actions.includes('read'))
                        .map((p) => ({ slug: p.library_slug, name: p.library_name || p.library_slug }));
                }
                if (!slug.value && libs.value.length) slug.value = libs.value[0].slug;
            } catch (e) {
                ElMessage.error(e.message);
            }
        }

        async function handleSearch() {
            if (!slug.value) {
                ElMessage.warning('请先选择一个库');
                return;
            }
            if (!query.value.trim()) {
                ElMessage.warning('请输入搜索关键词');
                return;
            }
            loading.value = true;
            try {
                const resp = await api.queryLibrary(slug.value, {
                    query: query.value.trim(),
                    limit: limit.value
                });
                results.value = resp.results || [];
                if (results.value.length === 0) {
                    ElMessage.info('未找到相似分片');
                }
            } catch (e) {
                ElMessage.error(e.message);
            } finally {
                loading.value = false;
            }
        }

        function formatScore(score) {
            return (score * 100).toFixed(1) + '%';
        }

        onMounted(loadLibs);

        return {
            libs,
            slug,
            query,
            limit,
            results,
            loading,
            faqs,
            pickFaq,
            handleSearch,
            formatScore
        };
    },
    template: `
    <div>
        <div class="page-header">
            <h2>数据检索</h2>
        </div>

        <el-card style="margin-bottom: 20px;">
            <el-form :inline="true" @submit.prevent="handleSearch">
                <el-form-item label="目标库">
                    <el-select v-model="slug" placeholder="选择库" style="width:220px">
                        <el-option v-for="l in libs" :key="l.slug" :label="l.name + ' (' + l.slug + ')'" :value="l.slug" />
                    </el-select>
                </el-form-item>
                <el-form-item label="关键词">
                    <el-input v-model="query" placeholder="输入关键词查询向量库" style="width:300px" clearable @keyup.enter="handleSearch" />
                </el-form-item>
                <el-form-item label="最大返回数">
                    <el-input-number v-model="limit" :min="1" :max="20" style="width:130px" />
                </el-form-item>
                <el-form-item>
                    <el-button type="primary" @click="handleSearch" :loading="loading">搜索</el-button>
                </el-form-item>
            </el-form>
            <div v-if="faqs.length" style="margin-top:4px">
                <span style="color:#909399;font-size:13px;margin-right:8px">常用问题：</span>
                <el-tag v-for="f in faqs" :key="f.id" effect="plain"
                        style="cursor:pointer;margin:0 8px 8px 0" @click="pickFaq(f.question)">
                    {{ f.question }}
                </el-tag>
            </div>
        </el-card>

        <el-table :data="results" border v-loading="loading" style="width: 100%">
            <el-table-column label="相似度" width="120" align="center">
                <template #default="{row}">
                    <el-tag :type="row.similarity >= 0.7 ? 'success' : row.similarity >= 0.5 ? 'warning' : 'info'">
                        {{ formatScore(row.similarity) }}
                    </el-tag>
                </template>
            </el-table-column>
            <el-table-column prop="text" label="分片正文" min-width="300">
                <template #default="{row}">
                    <div style="white-space: pre-wrap; font-size: 13px;">{{ row.text }}</div>
                </template>
            </el-table-column>
        </el-table>
    </div>
    `
};
