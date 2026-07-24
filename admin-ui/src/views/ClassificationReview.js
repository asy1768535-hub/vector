import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { useRoute, useRouter } from 'vue-router';

import * as api from '../api.js';
import {
    classificationReviewErrorKind,
    classificationReviewErrorMessage,
    currentPrimaryDecision,
    currentSecondaryDecisions,
    formatReviewConfidence,
    initialReviewSelection,
    reviewCanAccept,
    reviewPageMatches,
    reviewProposalLabel,
    reviewReasonLabel,
    reviewRoleLabel,
    reviewStatusLabel,
    reviewStatusTag,
    validateReviewSelection,
} from '../classification_review_ui.js';
import { formatTime } from '../common_ui.js';
import { manageableLibraries } from '../menu_access.js';
import { store } from '../store.js';

const PAGE_SIZE = 20;

export default {
    setup() {
        const route = useRoute();
        const router = useRouter();
        const libraries = computed(() => manageableLibraries(
            store.permissions,
            store.organizations,
        ));
        const selectedSlug = ref('');
        const selectedRunId = ref('');
        const primaryLabelId = ref('');
        const secondaryLabelIds = ref([]);
        const queue = reactive({
            loading: false,
            items: [],
            labels: [],
            taxonomyVersionId: '',
            total: 0,
            limit: PAGE_SIZE,
            offset: 0,
            errorKind: '',
            errorMessage: '',
            mutationError: '',
            mutatingAction: '',
        });

        let loadRequestSeq = 0;
        let mutationRequestSeq = 0;

        const selectedLibrary = computed(() => (
            libraries.value.find((item) => item.slug === selectedSlug.value) || null
        ));
        const selectedRun = computed(() => (
            queue.items.find((item) => String(item?.id || '') === selectedRunId.value) || null
        ));
        const pageProjection = computed(() => ({
            taxonomy_version_id: queue.taxonomyVersionId,
            available_labels: queue.labels,
        }));
        const canAccept = computed(() => reviewCanAccept(
            selectedRun.value,
            pageProjection.value,
        ));
        const currentPrimary = computed(() => currentPrimaryDecision(selectedRun.value));
        const currentSecondary = computed(() => currentSecondaryDecisions(selectedRun.value));
        const pageNumber = computed(() => Math.floor(queue.offset / queue.limit) + 1);
        const pageCount = computed(() => Math.max(1, Math.ceil(queue.total / queue.limit)));
        const hasPrevious = computed(() => queue.offset > 0 && !queue.loading);
        const hasNext = computed(() => (
            queue.offset + queue.items.length < queue.total && !queue.loading
        ));

        function clearSelection() {
            selectedRunId.value = '';
            primaryLabelId.value = '';
            secondaryLabelIds.value = [];
            queue.mutationError = '';
        }

        function initializeSelection(run) {
            const selection = initialReviewSelection(run, pageProjection.value);
            primaryLabelId.value = selection.primaryLabelId;
            secondaryLabelIds.value = selection.secondaryLabelIds;
        }

        function selectRun(run) {
            if (queue.mutatingAction || !run?.id) return;
            selectedRunId.value = String(run.id);
            queue.mutationError = '';
            initializeSelection(run);
        }

        function resetScopeState() {
            loadRequestSeq += 1;
            mutationRequestSeq += 1;
            queue.loading = false;
            queue.items = [];
            queue.labels = [];
            queue.taxonomyVersionId = '';
            queue.total = 0;
            queue.offset = 0;
            queue.errorKind = '';
            queue.errorMessage = '';
            queue.mutatingAction = '';
            clearSelection();
        }

        async function loadReviews({ preserveSelection = false } = {}) {
            const slug = selectedSlug.value;
            if (!slug || !libraries.value.some((item) => item.slug === slug)) return;
            const identity = { slug, limit: queue.limit, offset: queue.offset };
            const previousId = preserveSelection ? selectedRunId.value : '';
            const token = ++loadRequestSeq;
            queue.loading = true;
            queue.errorKind = '';
            queue.errorMessage = '';
            queue.mutationError = '';
            try {
                const response = await api.listClassificationReviews(slug, {
                    limit: identity.limit,
                    offset: identity.offset,
                });
                if (token !== loadRequestSeq || selectedSlug.value !== identity.slug
                    || queue.offset !== identity.offset || queue.limit !== identity.limit) return;
                if (!reviewPageMatches(response, identity)) {
                    queue.errorKind = 'malformed';
                    queue.errorMessage = classificationReviewErrorMessage('malformed');
                    queue.items = [];
                    clearSelection();
                    return;
                }
                queue.items = response.items;
                queue.labels = response.available_labels;
                queue.taxonomyVersionId = String(response.taxonomy_version_id || '');
                queue.total = response.total;
                const retained = previousId
                    ? queue.items.find((item) => String(item.id) === previousId)
                    : null;
                const next = retained || queue.items[0] || null;
                if (next) {
                    selectedRunId.value = String(next.id);
                    initializeSelection(next);
                } else {
                    clearSelection();
                }
            } catch (error) {
                if (token !== loadRequestSeq || selectedSlug.value !== identity.slug) return;
                const kind = classificationReviewErrorKind(error);
                queue.errorKind = kind;
                queue.errorMessage = classificationReviewErrorMessage(kind);
                queue.items = [];
                queue.labels = [];
                queue.taxonomyVersionId = '';
                queue.total = 0;
                clearSelection();
            } finally {
                if (token === loadRequestSeq) queue.loading = false;
            }
        }

        async function chooseLibrary(slug) {
            const next = String(slug || '');
            if (!libraries.value.some((item) => item.slug === next)) return;
            resetScopeState();
            selectedSlug.value = next;
            await router.replace({
                path: '/knowledge-governance/classification',
                query: { library: next },
            });
            await loadReviews();
        }

        async function changePage(direction) {
            if (queue.loading || queue.mutatingAction) return;
            const nextOffset = direction === 'next'
                ? queue.offset + queue.limit
                : Math.max(0, queue.offset - queue.limit);
            if (nextOffset === queue.offset) return;
            queue.offset = nextOffset;
            clearSelection();
            await loadReviews();
        }

        async function confirmManualReplacement(action) {
            if (selectedRun.value?.status !== 'blocked_manual' || action === 'reject') return true;
            try {
                await ElMessageBox.confirm(
                    '当前文档已有人工分类。继续后，本次审核结果会替换当前人工结果。',
                    '确认替换人工分类',
                    {
                        confirmButtonText: '继续审核',
                        cancelButtonText: '取消',
                        type: 'warning',
                    },
                );
                return true;
            } catch {
                return false;
            }
        }

        async function submitReview(action) {
            const run = selectedRun.value;
            const slug = selectedSlug.value;
            if (!run || queue.mutatingAction || !slug) return;
            if (action === 'accept' && !canAccept.value) {
                ElMessage.warning('提案包含未知或已停用分类，请调整后再提交');
                return;
            }
            if (action === 'change') {
                const validation = validateReviewSelection(
                    primaryLabelId.value,
                    secondaryLabelIds.value,
                    queue.labels,
                );
                if (validation) {
                    ElMessage.warning(validation);
                    return;
                }
            }
            if (!await confirmManualReplacement(action)) return;

            const identity = {
                slug,
                runId: String(run.id),
                runStatus: run.status,
                effectiveDecisionSetId: run.effective_decision_set_id || null,
            };
            const body = {
                expected_run_status: identity.runStatus,
                expected_effective_decision_set_id: identity.effectiveDecisionSetId,
                action,
                primary_label_id: action === 'change' ? primaryLabelId.value : null,
                secondary_label_ids: action === 'change' ? [...secondaryLabelIds.value] : [],
            };
            const token = ++mutationRequestSeq;
            queue.mutatingAction = action;
            queue.mutationError = '';
            try {
                await api.reviewClassificationRun(identity.slug, identity.runId, body);
                if (token !== mutationRequestSeq || selectedSlug.value !== identity.slug
                    || selectedRunId.value !== identity.runId) return;
                ElMessage.success(action === 'reject' ? '分类建议已驳回' : '分类审核已应用');
                clearSelection();
                await loadReviews();
                if (!queue.items.length && queue.offset > 0 && queue.total > 0) {
                    queue.offset = Math.max(0, queue.offset - queue.limit);
                    await loadReviews();
                }
            } catch (error) {
                if (token !== mutationRequestSeq || selectedSlug.value !== identity.slug) return;
                const kind = classificationReviewErrorKind(error);
                queue.mutationError = classificationReviewErrorMessage(kind);
                if (kind === 'conflict') {
                    ElMessage.warning(queue.mutationError);
                    queue.offset = 0;
                    clearSelection();
                    await loadReviews();
                }
            } finally {
                if (token === mutationRequestSeq) queue.mutatingAction = '';
            }
        }

        async function openCatalogDocument() {
            if (!selectedRun.value?.document_id || !selectedSlug.value) return;
            await router.push({
                path: '/knowledge-assets/catalog',
                query: {
                    library: selectedSlug.value,
                    document: String(selectedRun.value.document_id),
                },
            });
        }

        async function reconcileLibraries() {
            const rows = libraries.value;
            if (!rows.length) {
                selectedSlug.value = '';
                resetScopeState();
                return;
            }
            const requested = String(route.query.library || '');
            const next = rows.some((item) => item.slug === selectedSlug.value)
                ? selectedSlug.value
                : (rows.some((item) => item.slug === requested) ? requested : rows[0].slug);
            if (next !== selectedSlug.value) {
                await chooseLibrary(next);
            }
        }

        watch(
            () => libraries.value.map((item) => `${item.organizationId}:${item.slug}`).join('|'),
            reconcileLibraries,
        );
        watch(
            () => String(route.query.library || ''),
            (requested) => {
                if (requested && requested !== selectedSlug.value
                    && libraries.value.some((item) => item.slug === requested)) {
                    chooseLibrary(requested);
                }
            },
        );
        onMounted(reconcileLibraries);
        onBeforeUnmount(() => {
            loadRequestSeq += 1;
            mutationRequestSeq += 1;
        });

        return {
            libraries, selectedSlug, selectedLibrary, chooseLibrary,
            queue, selectedRunId, selectedRun, selectRun,
            primaryLabelId, secondaryLabelIds,
            canAccept, currentPrimary, currentSecondary,
            pageNumber, pageCount, hasPrevious, hasNext,
            loadReviews, changePage, submitReview, openCatalogDocument,
            reviewStatusLabel, reviewStatusTag, reviewRoleLabel,
            reviewReasonLabel, reviewProposalLabel, formatReviewConfidence, formatTime,
        };
    },
    template: `
    <div class="classification-review-workspace">
      <header class="classification-review-header">
        <div>
          <h2>分类审核</h2>
          <p>{{ selectedLibrary ? selectedLibrary.name : '未选择知识库' }}</p>
        </div>
        <div class="classification-review-header-actions">
          <span v-if="selectedSlug">待处理 <b>{{ queue.total }}</b></span>
          <el-select :model-value="selectedSlug" class="classification-review-library"
                     :disabled="queue.loading || Boolean(queue.mutatingAction)"
                     @change="chooseLibrary">
            <el-option v-for="item in libraries" :key="item.slug"
                       :label="item.name" :value="item.slug" />
          </el-select>
        </div>
      </header>

      <el-alert v-if="!libraries.length" title="当前账号没有可管理的知识库"
                type="info" :closable="false" show-icon />

      <template v-else>
        <el-alert v-if="queue.errorMessage" class="classification-review-alert"
                  :title="queue.errorMessage" :type="queue.errorKind === 'forbidden' ? 'warning' : 'error'"
                  :closable="false" show-icon>
          <template #default>
            <el-button text type="primary" :disabled="queue.loading || Boolean(queue.mutatingAction)"
                       @click="loadReviews()">重新加载</el-button>
          </template>
        </el-alert>

        <div class="classification-review-grid">
          <section class="classification-review-queue" aria-label="待审核分类队列">
            <div class="classification-review-section-heading">
              <div><h3>待审核队列</h3><span>按进入队列时间排列</span></div>
              <el-tag type="warning" effect="plain">{{ queue.total }}</el-tag>
            </div>

            <div v-if="queue.loading" class="classification-review-state">正在加载审核队列...</div>
            <div v-else-if="!queue.items.length && !queue.errorMessage" class="classification-review-state">
              <local-icon icon="status:success"></local-icon>
              <strong>当前没有待审核分类</strong>
            </div>
            <div v-else class="classification-review-list">
              <button v-for="run in queue.items" :key="run.id" type="button"
                      class="classification-review-item"
                      :class="{ 'is-selected': String(run.id) === selectedRunId }"
                      :disabled="Boolean(queue.mutatingAction)" @click="selectRun(run)">
                <span class="classification-review-item-top">
                  <strong :title="run.document_title || '未命名文档'">{{ run.document_title || '未命名文档' }}</strong>
                  <el-tag :type="reviewStatusTag(run.status)" size="small">{{ reviewStatusLabel(run.status) }}</el-tag>
                </span>
                <span class="classification-review-item-proposal">
                  {{ run.proposals[0] ? reviewProposalLabel(run.proposals[0]) : '没有可显示的分类建议' }}
                  <small v-if="run.proposals.length > 1">+{{ run.proposals.length - 1 }}</small>
                </span>
                <span class="classification-review-item-meta">
                  <span>第 {{ run.generation_no }} 代</span>
                  <span v-if="run.retry_generation">重试 {{ run.retry_generation }}</span>
                  <span>{{ formatTime(run.created_at) }}</span>
                </span>
              </button>
            </div>

            <div class="classification-review-pagination">
              <el-button :disabled="!hasPrevious" title="上一页" @click="changePage('previous')">
                <local-icon icon="mdi:chevron-left"></local-icon>上一页
              </el-button>
              <span>第 {{ pageNumber }} / {{ pageCount }} 页</span>
              <el-button :disabled="!hasNext" title="下一页" @click="changePage('next')">
                下一页<local-icon icon="mdi:chevron-right"></local-icon>
              </el-button>
            </div>
          </section>

          <section class="classification-review-detail" aria-label="分类审核详情">
            <div v-if="!selectedRun" class="classification-review-detail-empty">
              <local-icon icon="mdi:shield-key-outline"></local-icon>
              <strong>选择一条记录开始审核</strong>
            </div>

            <template v-else>
              <div class="classification-review-detail-heading">
                <div>
                  <div class="classification-review-title-line">
                    <h3>{{ selectedRun.document_title || '未命名文档' }}</h3>
                    <el-tag :type="reviewStatusTag(selectedRun.status)">{{ reviewStatusLabel(selectedRun.status) }}</el-tag>
                  </div>
                  <p>Revision {{ selectedRun.document_revision_id }} · 第 {{ selectedRun.generation_no }} 代</p>
                </div>
                <el-button text type="primary" title="打开文档详情" @click="openCatalogDocument">
                  <local-icon icon="mdi:file-document-outline"></local-icon>文档详情
                </el-button>
              </div>

              <el-alert v-if="selectedRun.status === 'blocked_manual'"
                        class="classification-review-manual-warning"
                        title="当前文档已有人工分类，接受或调整会替换当前结果"
                        type="warning" :closable="false" show-icon />
              <el-alert v-if="queue.mutationError" class="classification-review-alert"
                        :title="queue.mutationError" type="error" :closable="false" show-icon />

              <div class="classification-review-detail-section">
                <div class="classification-review-section-heading">
                  <div><h3>模型建议</h3><span>{{ selectedRun.proposals.length }} 项</span></div>
                  <el-button type="primary" plain :disabled="!canAccept || Boolean(queue.mutatingAction)"
                             :loading="queue.mutatingAction === 'accept'" @click="submitReview('accept')">
                    <local-icon icon="status:success"></local-icon>接受建议
                  </el-button>
                </div>
                <div v-if="selectedRun.proposals.length" class="classification-review-proposals">
                  <div v-for="proposal in selectedRun.proposals" :key="proposal.id"
                       class="classification-review-proposal">
                    <span class="classification-review-proposal-role">{{ reviewRoleLabel(proposal.role) }}</span>
                    <div>
                      <strong>{{ reviewProposalLabel(proposal) }}</strong>
                      <small v-if="!proposal.label_id">未知分类建议</small>
                    </div>
                    <b>{{ formatReviewConfidence(proposal.confidence_micros) }}</b>
                    <div class="classification-review-reasons">
                      <span v-for="reason in proposal.reason_codes" :key="reason">{{ reviewReasonLabel(reason) }}</span>
                    </div>
                  </div>
                </div>
                <div v-else class="classification-review-inline-empty">没有可接受的分类建议</div>
              </div>

              <div class="classification-review-detail-section">
                <div class="classification-review-section-heading"><div><h3>当前有效分类</h3></div></div>
                <div v-if="currentPrimary" class="classification-review-current">
                  <strong>{{ currentPrimary.label }}</strong>
                  <span v-for="item in currentSecondary" :key="item.id">{{ item.label }}</span>
                  <small>{{ selectedRun.effective_source === 'manual' ? '人工结果' : '模型结果' }}</small>
                </div>
                <div v-else class="classification-review-inline-empty">
                  {{ selectedRun.status === 'blocked_manual' ? '当前人工分类已移除，但仍保护本版本' : '当前没有有效分类' }}
                </div>
              </div>

              <div class="classification-review-detail-section classification-review-adjustment">
                <div class="classification-review-section-heading"><div><h3>调整后应用</h3><span>只可选择当前知识库启用的分类</span></div></div>
                <div v-if="queue.labels.length" class="classification-review-form">
                  <label>
                    <span>主分类</span>
                    <el-select v-model="primaryLabelId" filterable :disabled="Boolean(queue.mutatingAction)">
                      <el-option v-for="label in queue.labels" :key="label.id"
                                 :label="label.label" :value="String(label.id)" />
                    </el-select>
                  </label>
                  <label>
                    <span>辅助分类</span>
                    <el-select v-model="secondaryLabelIds" multiple filterable collapse-tags
                               :max-collapse-tags="3" :disabled="Boolean(queue.mutatingAction)">
                      <el-option v-for="label in queue.labels" :key="label.id"
                                 :label="label.label" :value="String(label.id)"
                                 :disabled="String(label.id) === primaryLabelId" />
                    </el-select>
                  </label>
                  <div class="classification-review-actions">
                    <el-button type="primary" :loading="queue.mutatingAction === 'change'"
                               :disabled="Boolean(queue.mutatingAction)" @click="submitReview('change')">
                      应用调整
                    </el-button>
                    <el-button type="danger" plain :loading="queue.mutatingAction === 'reject'"
                               :disabled="Boolean(queue.mutatingAction)" @click="submitReview('reject')">
                      驳回建议
                    </el-button>
                  </div>
                </div>
                <el-alert v-else title="当前知识库没有可用于调整的启用分类"
                          type="warning" :closable="false" show-icon />
              </div>
            </template>
          </section>
        </div>
      </template>
    </div>
    `,
};
