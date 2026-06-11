import type { RouteMeta } from 'vue-router';
import ElegantVueRouter from '@elegant-router/vue/vite';
import type { RouteKey } from '@elegant-router/types';

export function setupElegantRouter() {
  return ElegantVueRouter({
    layouts: {
      base: 'src/layouts/base-layout/index.vue',
      blank: 'src/layouts/blank-layout/index.vue'
    },
    routePathTransformer(routeName, routePath) {
      const key = routeName as RouteKey;

      if (key === 'login') {
        const modules: UnionKey.LoginModule[] = ['pwd-login', 'code-login', 'register', 'reset-pwd', 'bind-wechat'];

        const moduleReg = modules.join('|');

        return `/login/:module(${moduleReg})?`;
      }

      return routePath;
    },
    onRouteMetaGen(routeName) {
      const key = routeName as RouteKey;

      const constantRoutes: RouteKey[] = ['login', '403', '404', '500'];

      // 向量知识库各业务页菜单元信息（中文 title + 图标 + 顺序 + 角色）
      const metaMap: Record<string, Partial<RouteMeta>> = {
        home: { title: '概览', icon: 'mdi:view-dashboard-outline', order: 1 },
        documents: { title: '文档', icon: 'mdi:file-document-outline', order: 10 },
        search: { title: '数据检索', icon: 'mdi:text-search', order: 11 },
        import: { title: '导入数据', icon: 'mdi:database-import-outline', order: 12 },
        'api-keys': { title: '我的 API Key', icon: 'mdi:key-variant', order: 13 },
        users: { title: '用户管理', icon: 'mdi:account-group-outline', order: 20, roles: ['R_SUPER'] },
        libraries: { title: '库管理', icon: 'mdi:bookshelf', order: 21, roles: ['R_SUPER'] },
        permissions: { title: '权限矩阵', icon: 'mdi:shield-key-outline', order: 22, roles: ['R_SUPER'] },
        jobs: { title: '任务监控', icon: 'mdi:cog-sync-outline', order: 23, roles: ['R_SUPER'] },
        audit: { title: '审计日志', icon: 'mdi:history', order: 24, roles: ['R_SUPER'] }
      };

      const meta: Partial<RouteMeta> = metaMap[key] || {
        title: key,
        i18nKey: `route.${key}` as App.I18n.I18nKey
      };

      if (constantRoutes.includes(key)) {
        meta.constant = true;
      }

      // 内置功能页不进侧边菜单
      const hideInMenuRoutes: RouteKey[] = ['login', '403', '404', '500', 'iframe-page'];
      if (hideInMenuRoutes.includes(key)) {
        meta.hideInMenu = true;
      }

      return meta;
    }
  });
}
