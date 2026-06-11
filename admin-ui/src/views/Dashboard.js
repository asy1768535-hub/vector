import { onMounted, ref } from 'vue';
import * as api from '../api.js';
import { store } from '../store.js';

export default {
    setup() {
        const health = ref(null);
        const error = ref(null);

        onMounted(async () => {
            try {
                health.value = await api.health();
            } catch (e) {
                error.value = e.message;
            }
        });

        return { health, error, store };
    },
    template: `
    <div>
        <el-row :gutter="20">
            <el-col :span="8">
                <el-card>
                    <template #header>当前用户</template>
                    <p><b>邮箱:</b> {{ store.user?.email }}</p>
                    <p><b>角色:</b>
                        <el-tag :type="store.user?.is_superuser ? 'danger' : 'info'" size="small">
                            {{ store.user?.is_superuser ? '超级管理员' : '普通用户' }}
                        </el-tag>
                    </p>
                    <p><b>已授权库:</b>
                        <template v-if="store.user?.is_superuser">全部（超管直通）</template>
                        <template v-else-if="store.permissions.length === 0">无</template>
                        <template v-else>
                            <el-tag v-for="p in store.permissions" :key="p.library_slug" style="margin-right:4px">
                                {{ p.library_slug }} ({{ p.actions.join('/') }})
                            </el-tag>
                        </template>
                    </p>
                </el-card>
            </el-col>

            <el-col :span="16">
                <el-card>
                    <template #header>服务状态</template>
                    <p v-if="error"><el-tag type="danger">{{ error }}</el-tag></p>
                    <template v-else-if="health">
                        <el-descriptions :column="2" border>
                            <el-descriptions-item label="API">
                                <el-tag :type="health.status === 'ok' ? 'success' : 'warning'">{{ health.status }}</el-tag>
                            </el-descriptions-item>
                            <el-descriptions-item label="DB">
                                <el-tag :type="health.db ? 'success' : 'danger'">{{ health.db ? 'OK' : 'DOWN' }}</el-tag>
                            </el-descriptions-item>
                            <el-descriptions-item label="Qdrant">
                                <el-tag :type="health.qdrant ? 'success' : 'danger'">{{ health.qdrant ? 'OK' : 'DOWN' }}</el-tag>
                            </el-descriptions-item>
                            <el-descriptions-item label="Embedding 模型">
                                {{ health.embedding_model }} ({{ health.embedding_dim }}维)
                            </el-descriptions-item>
                        </el-descriptions>
                    </template>
                </el-card>
            </el-col>
        </el-row>

        <el-card style="margin-top:20px">
            <template #header>使用提示</template>
            <ol style="line-height:1.8">
                <li>左侧菜单按角色过滤；管理员看到全部，普通用户只能看到「文档」「API Key」。</li>
                <li>需要外部调用（Dify 等）时，在「我的 API Key」生成一把 Bearer Key。</li>
                <li>管理员可在「权限矩阵」给用户授权 read / insert / delete 三种动作。</li>
                <li>Dify 检索端点：<code>POST /retrieval</code>，body 见 <code>README.md</code>。</li>
            </ol>
        </el-card>
    </div>
    `,
};
