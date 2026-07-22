import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';

import * as api from '../api.js';
import {
    compatibilityConflicts,
    compatibilityItems,
    compatibilityLibraryReadiness,
    formatRetrievalNumber,
    formatRetrievalPercent,
    librariesForOrganization,
    organizationAdminMemberships,
    retrievalScopeKey,
    retrievalSourceLocation,
    rewriteSourceLabel,
    shortRetrievalId,
    validateRetrievalTest,
} from '../retrieval_test_ui.js';
import { searchEmpty, serviceError } from '../illustrations.js';
import { store } from '../store.js';

function fixedRequestError(error) {
    if (error?.status === 403) return '你没有运行该组织检索诊断的权限';
    if (error?.status === 404) return '检索诊断暂未启用';
    if (error?.status === 409) return '当前知识库范围不兼容';
    if (error?.status === 422) return '检索范围或参数不符合要求';
    if (error?.status === 502) return '检索分支执行失败，请稍后重试';
    return '检索诊断请求失败，请稍后重试';
}

function sameOrderedValues(left, right) {
    return Array.isArray(left) && Array.isArray(right)
        && left.length === right.length
        && left.every((value, index) => String(value) === String(right[index]));
}

export default {
    setup() {
        const organizations = computed(() => organizationAdminMemberships(store.organizations));
        const organizationId = ref('');
        const librarySlugs = ref([]);
        const query = ref('');
        const topK = ref(10);
        const candidateK = ref(30);
        const scoreThreshold = ref(0);
        const compatibility = reactive({
            status: 'idle',
            data: null,
            conflicts: [],
            errorMessage: '',
            scopeKey: '',
        });
        const retrieval = reactive({
            loading: false,
            data: null,
            errorMessage: '',
            conflicts: [],
        });

        let compatibilityRequestSeq = 0;
        let retrievalRequestSeq = 0;
        let compatibilityTimer = null;

        const selectedOrganization = computed(() => (
            organizations.value.find((item) => (
                String(item.organization_id) === organizationId.value
            )) || null
        ));
        const availableLibraries = computed(() => (
            librariesForOrganization(store.permissions, organizationId.value)
        ));
        const currentScopeKey = computed(() => (
            retrievalScopeKey(organizationId.value, librarySlugs.value)
        ));
        const compatibilityCurrent = computed(() => (
            compatibility.scopeKey === currentScopeKey.value
        ));
        const canRun = computed(() => (
            compatibility.status === 'compatible'
            && compatibilityCurrent.value
            && !retrieval.loading
            && !validateRetrievalTest(formValues())
        ));
        const selectedLibraryNames = computed(() => {
            const names = new Map(availableLibraries.value.map((item) => [item.slug, item.name]));
            return librarySlugs.value.map((slug) => names.get(slug) || slug);
        });

        function formValues() {
            return {
                organizationId: organizationId.value,
                librarySlugs: [...librarySlugs.value],
                query: query.value,
                topK: topK.value,
                candidateK: candidateK.value,
                scoreThreshold: scoreThreshold.value,
            };
        }

        function executionKey() {
            return JSON.stringify({
                scope: currentScopeKey.value,
                query: String(query.value || '').trim(),
                topK: Number(topK.value),
                candidateK: Number(candidateK.value),
                scoreThreshold: Number(scoreThreshold.value),
            });
        }

        function clearRetrieval() {
            retrievalRequestSeq += 1;
            retrieval.loading = false;
            retrieval.data = null;
            retrieval.errorMessage = '';
            retrieval.conflicts = [];
        }

        function clearCompatibility() {
            compatibilityRequestSeq += 1;
            compatibility.status = 'idle';
            compatibility.data = null;
            compatibility.conflicts = [];
            compatibility.errorMessage = '';
            compatibility.scopeKey = '';
        }

        function cancelCompatibilityTimer() {
            if (compatibilityTimer) clearTimeout(compatibilityTimer);
            compatibilityTimer = null;
        }

        function scheduleCompatibility() {
            cancelCompatibilityTimer();
            clearCompatibility();
            clearRetrieval();
            if (!currentScopeKey.value || !librarySlugs.value.length) return;
            compatibility.status = 'checking';
            compatibilityTimer = setTimeout(() => {
                compatibilityTimer = null;
                checkCompatibility();
            }, 200);
        }

        async function checkCompatibility() {
            cancelCompatibilityTimer();
            const requestedOrganizationId = organizationId.value;
            const requestedSlugs = [...librarySlugs.value];
            const requestedScopeKey = retrievalScopeKey(
                requestedOrganizationId,
                requestedSlugs,
            );
            if (!requestedScopeKey || !requestedSlugs.length) {
                clearCompatibility();
                return false;
            }
            const token = ++compatibilityRequestSeq;
            compatibility.status = 'checking';
            compatibility.data = null;
            compatibility.conflicts = [];
            compatibility.errorMessage = '';
            compatibility.scopeKey = requestedScopeKey;
            try {
                const response = await api.checkLibraryCompatibility({
                    library_slugs: requestedSlugs,
                    channels: ['text'],
                });
                if (token !== compatibilityRequestSeq
                    || requestedScopeKey !== currentScopeKey.value) return false;
                const responseSlugs = (response?.libraries || [])
                    .map((item) => item?.library_slug);
                if (String(response?.organization_id || '') !== requestedOrganizationId
                    || !sameOrderedValues(responseSlugs, requestedSlugs)) {
                    compatibility.status = 'error';
                    compatibility.errorMessage = '兼容性检查返回的范围不一致';
                    return false;
                }
                compatibility.data = response;
                compatibility.conflicts = compatibilityItems(response?.incompatibilities);
                compatibility.status = response?.compatible === true
                    ? 'compatible'
                    : 'incompatible';
                return compatibility.status === 'compatible';
            } catch (error) {
                if (token !== compatibilityRequestSeq
                    || requestedScopeKey !== currentScopeKey.value) return false;
                compatibility.status = 'error';
                compatibility.errorMessage = fixedRequestError(error);
                return false;
            }
        }

        function onOrganizationChange() {
            const choices = availableLibraries.value;
            librarySlugs.value = choices.length ? [choices[0].slug] : [];
            scheduleCompatibility();
        }

        function onScopeChange() {
            if (librarySlugs.value.length > 20) librarySlugs.value = librarySlugs.value.slice(0, 20);
            scheduleCompatibility();
        }

        async function runRetrievalTest() {
            const validation = validateRetrievalTest(formValues());
            if (validation) {
                ElMessage.warning(validation);
                return;
            }
            if (!compatibilityCurrent.value || compatibility.status !== 'compatible') {
                const ready = await checkCompatibility();
                if (!ready) {
                    ElMessage.warning('请先处理知识库兼容性问题');
                    return;
                }
            }
            const requestedExecutionKey = executionKey();
            const requestedOrganizationId = organizationId.value;
            const requestedSlugs = [...librarySlugs.value];
            const request = {
                library_slugs: requestedSlugs,
                query: String(query.value || '').trim(),
                top_k: Number(topK.value),
                candidate_k: Number(candidateK.value),
                score_threshold: Number(scoreThreshold.value),
            };
            const token = ++retrievalRequestSeq;
            retrieval.loading = true;
            retrieval.data = null;
            retrieval.errorMessage = '';
            retrieval.conflicts = [];
            try {
                const response = await api.runOrganizationRetrievalTest(
                    requestedOrganizationId,
                    request,
                );
                if (token !== retrievalRequestSeq
                    || requestedExecutionKey !== executionKey()) return;
                if (String(response?.organization_id || '') !== requestedOrganizationId
                    || !sameOrderedValues(response?.library_slugs, requestedSlugs)
                    || String(response?.query || '') !== request.query) {
                    retrieval.errorMessage = '检索结果返回的执行范围不一致';
                    return;
                }
                retrieval.data = response;
            } catch (error) {
                if (token !== retrievalRequestSeq
                    || requestedExecutionKey !== executionKey()) return;
                retrieval.conflicts = compatibilityConflicts(error);
                retrieval.errorMessage = fixedRequestError(error);
                if (retrieval.conflicts.length) {
                    compatibility.status = 'incompatible';
                    compatibility.conflicts = retrieval.conflicts;
                    compatibility.data = null;
                    compatibility.scopeKey = currentScopeKey.value;
                }
            } finally {
                if (token === retrievalRequestSeq) retrieval.loading = false;
            }
        }

        function resetForm() {
            query.value = '';
            topK.value = 10;
            candidateK.value = 30;
            scoreThreshold.value = 0;
            clearRetrieval();
        }

        function hitRowKey(row) {
            return [row?.library_slug, row?.rank, row?.source?.chunk_id || ''].join(':');
        }

        watch(
            () => [query.value, topK.value, candidateK.value, scoreThreshold.value],
            clearRetrieval,
        );

        onMounted(() => {
            if (organizations.value.length) {
                organizationId.value = String(organizations.value[0].organization_id);
                onOrganizationChange();
            }
        });

        onBeforeUnmount(() => {
            cancelCompatibilityTimer();
            clearCompatibility();
            clearRetrieval();
        });

        return {
            organizations, organizationId, selectedOrganization,
            availableLibraries, librarySlugs, selectedLibraryNames,
            query, topK, candidateK, scoreThreshold,
            compatibility, compatibilityCurrent, retrieval, canRun,
            onOrganizationChange, onScopeChange, checkCompatibility,
            runRetrievalTest, resetForm, hitRowKey,
            formatRetrievalNumber, formatRetrievalPercent,
            compatibilityLibraryReadiness,
            retrievalSourceLocation, rewriteSourceLabel, shortRetrievalId,
            searchEmpty, serviceError,
        };
    },
    template: `
    <div class="retrieval-test-workspace">
      <header class="retrieval-test-header">
        <div>
          <h2>检索诊断</h2>
          <p>{{ selectedOrganization ? selectedOrganization.name : '未选择组织' }}</p>
        </div>
        <el-tag type="info" effect="plain">文本检索</el-tag>
      </header>

      <el-alert v-if="!organizations.length" class="retrieval-test-access-empty"
                title="当前账号没有可管理的组织" type="info" :closable="false" show-icon />

      <template v-else>
        <section class="retrieval-test-controls">
          <div class="retrieval-test-scope">
            <label>
              <span>组织</span>
              <el-select v-model="organizationId" @change="onOrganizationChange">
                <el-option v-for="item in organizations" :key="item.organization_id"
                           :label="item.name" :value="String(item.organization_id)" />
              </el-select>
            </label>
            <label>
              <span>知识库范围</span>
              <el-select v-model="librarySlugs" multiple filterable collapse-tags
                         :max-collapse-tags="3" :disabled="!availableLibraries.length"
                         @change="onScopeChange">
                <el-option v-for="item in availableLibraries" :key="item.slug"
                           :label="item.name" :value="item.slug" />
              </el-select>
            </label>
          </div>

          <label class="retrieval-test-query">
            <span>检索内容</span>
            <el-input v-model="query" type="textarea" :rows="3" maxlength="4000"
                      show-word-limit placeholder="输入需要验证的检索内容" />
          </label>

          <div class="retrieval-test-parameters">
            <label><span>返回数</span><el-input-number v-model="topK" :min="1" :max="50" /></label>
            <label><span>候选数</span><el-input-number v-model="candidateK" :min="1" :max="100" /></label>
            <label><span>分数阈值</span><el-input-number v-model="scoreThreshold" :min="0" :max="1" :step="0.05" :precision="2" /></label>
            <div class="retrieval-test-actions">
              <el-button @click="resetForm">重置参数</el-button>
              <el-button type="primary" :loading="retrieval.loading" :disabled="!canRun"
                         @click="runRetrievalTest">
                <local-icon icon="mdi:text-search"></local-icon>运行检索
              </el-button>
            </div>
          </div>
        </section>

        <section class="retrieval-test-compatibility" :class="'is-' + compatibility.status">
          <div class="retrieval-test-compatibility-head">
            <div>
              <strong>范围兼容性</strong>
              <span v-if="compatibility.status === 'idle'">请选择知识库</span>
              <span v-else-if="compatibility.status === 'checking'">正在检查</span>
              <span v-else-if="compatibility.status === 'compatible'">可以联合检索</span>
              <span v-else-if="compatibility.status === 'incompatible'">当前范围不能联合检索</span>
              <span v-else>{{ compatibility.errorMessage }}</span>
            </div>
            <el-button v-if="compatibility.status === 'error'" text
                       @click="checkCompatibility">
              <local-icon icon="status:retry"></local-icon>重试
            </el-button>
          </div>
          <div v-if="selectedLibraryNames.length" class="retrieval-test-selected-scope">
            <span v-for="(name, index) in selectedLibraryNames" :key="librarySlugs[index]">
              {{ index + 1 }}. {{ name }}
            </span>
          </div>
          <div v-if="compatibility.data?.libraries?.length"
               class="retrieval-test-library-readiness">
            <div v-for="item in compatibility.data.libraries" :key="item.library_slug">
              <strong>{{ item.library_name }}</strong>
              <span>{{ item.library_slug }}</span>
              <el-tag v-for="state in compatibilityLibraryReadiness(item)" :key="state.key"
                      size="small" :type="state.ready ? 'success' : 'warning'" effect="plain">
                {{ state.label }}{{ state.ready ? '就绪' : '未就绪' }}
              </el-tag>
            </div>
          </div>
          <ul v-if="compatibility.conflicts.length" class="retrieval-test-conflicts">
            <li v-for="item in compatibility.conflicts" :key="item.librarySlug">
              <strong>{{ item.librarySlug }}</strong>
              <span>{{ item.reasons.join('、') }}</span>
            </li>
          </ul>
        </section>

        <section v-if="retrieval.errorMessage" class="retrieval-test-result-state">
          <img :src="serviceError" alt="" aria-hidden="true" />
          <h3>{{ retrieval.errorMessage }}</h3>
          <ul v-if="retrieval.conflicts.length" class="retrieval-test-conflicts">
            <li v-for="item in retrieval.conflicts" :key="item.librarySlug">
              <strong>{{ item.librarySlug }}</strong>
              <span>{{ item.reasons.join('、') }}</span>
            </li>
          </ul>
        </section>

        <template v-else-if="retrieval.data">
          <section class="retrieval-test-summary">
            <div><strong>{{ retrieval.data.hits.length }}</strong><span>命中片段</span></div>
            <div><strong>{{ retrieval.data.library_slugs.length }}</strong><span>知识库</span></div>
            <div><strong>{{ retrieval.data.total_elapsed_ms }} ms</strong><span>总耗时</span></div>
            <div><strong>{{ retrieval.data.fusion_contract_version }}</strong><span>融合规则</span></div>
          </section>

          <section class="retrieval-test-profile-band">
            <div class="retrieval-test-section-heading"><h3>执行范围</h3></div>
            <div class="retrieval-test-profile-list">
              <div v-for="profile in retrieval.data.profiles" :key="profile.library_slug">
                <strong>{{ profile.library_name }}</strong>
                <span>{{ profile.library_slug }}</span>
                <code :title="profile.embedding_profile_sha256">E {{ shortRetrievalId(profile.embedding_profile_sha256) }}</code>
                <code :title="profile.retrieval_profile_sha256">R {{ shortRetrievalId(profile.retrieval_profile_sha256) }}</code>
              </div>
            </div>
          </section>

          <section class="retrieval-test-timing-band">
            <div class="retrieval-test-section-heading"><h3>分库耗时</h3></div>
            <div class="retrieval-test-timing-list">
              <div v-for="item in retrieval.data.timings" :key="item.library_slug">
                <strong>{{ item.library_slug }}</strong>
                <span>{{ item.candidate_count }} 个候选</span>
                <span>{{ item.elapsed_ms }} ms</span>
              </div>
            </div>
          </section>

          <section class="retrieval-test-results-band">
            <div class="retrieval-test-section-heading">
              <h3>命中结果</h3>
              <span>阈值 {{ formatRetrievalPercent(retrieval.data.score_threshold) }}</span>
            </div>
            <div class="retrieval-test-results-shell">
              <el-table :data="retrieval.data.hits" :row-key="hitRowKey">
                <template #empty>
                  <div class="illustration-empty-wrapper">
                    <img :src="searchEmpty" class="illustration-search-empty" alt="" aria-hidden="true" />
                    <p>当前范围没有命中片段</p>
                  </div>
                </template>
                <el-table-column type="expand" width="46">
                  <template #default="{row}">
                    <dl class="retrieval-test-hit-detail">
                      <div><dt>Document</dt><dd :title="row.source.document_id">{{ shortRetrievalId(row.source.document_id) }}</dd></div>
                      <div><dt>Revision</dt><dd :title="row.source.document_revision_id">{{ shortRetrievalId(row.source.document_revision_id) }}</dd></div>
                      <div><dt>Chunk</dt><dd :title="row.source.chunk_id">{{ shortRetrievalId(row.source.chunk_id) }}</dd></div>
                      <div><dt>片段序号</dt><dd>{{ row.source.seq ?? '—' }}</dd></div>
                      <div><dt>向量分数</dt><dd>{{ formatRetrievalNumber(row.source.vector_score) }}</dd></div>
                      <div><dt>重排分数</dt><dd>{{ formatRetrievalNumber(row.source.rerank_score) }}</dd></div>
                      <div><dt>本地 RRF</dt><dd>{{ formatRetrievalNumber(row.source.local_rrf_score) }}</dd></div>
                      <div><dt>稠密排名</dt><dd>{{ row.source.dense_rank ?? '—' }}</dd></div>
                      <div><dt>关键词排名</dt><dd>{{ row.source.keyword_rank ?? '—' }}</dd></div>
                      <div class="retrieval-test-rewrite-row"><dt>查询来源</dt><dd><el-tag v-for="kind in row.source.rewrite_sources" :key="kind" size="small" type="info">{{ rewriteSourceLabel(kind) }}</el-tag><span v-if="!row.source.rewrite_sources.length">—</span></dd></div>
                    </dl>
                  </template>
                </el-table-column>
                <el-table-column label="排名" width="78" align="center">
                  <template #default="{row}"><strong>#{{ row.rank }}</strong></template>
                </el-table-column>
                <el-table-column label="知识库" min-width="150">
                  <template #default="{row}"><div class="retrieval-test-library-cell"><strong>{{ row.library_name }}</strong><span>{{ row.library_slug }} · 本地 #{{ row.local_rank }}</span></div></template>
                </el-table-column>
                <el-table-column label="命中片段" min-width="340">
                  <template #default="{row}"><div class="retrieval-test-excerpt"><strong :title="row.title">{{ row.title }}</strong><p>{{ row.content_excerpt }}</p><el-tag v-if="row.content_truncated" size="small" type="info">片段已截断</el-tag></div></template>
                </el-table-column>
                <el-table-column label="来源" min-width="190">
                  <template #default="{row}"><div class="retrieval-test-source-cell"><span :title="retrievalSourceLocation(row.source)">{{ retrievalSourceLocation(row.source) }}</span><code :title="row.source.document_revision_id">{{ shortRetrievalId(row.source.document_revision_id) }}</code></div></template>
                </el-table-column>
                <el-table-column label="分数" width="150" align="right">
                  <template #default="{row}"><div class="retrieval-test-score-cell"><span>融合 {{ formatRetrievalNumber(row.fusion_score) }}</span><span>本地 {{ formatRetrievalNumber(row.local_score) }}</span></div></template>
                </el-table-column>
              </el-table>
            </div>
          </section>
        </template>

        <section v-else-if="!retrieval.loading" class="retrieval-test-result-state is-initial">
          <img :src="searchEmpty" alt="" aria-hidden="true" />
          <p>完成范围兼容性检查后运行检索</p>
        </section>
      </template>
    </div>
    `,
};
