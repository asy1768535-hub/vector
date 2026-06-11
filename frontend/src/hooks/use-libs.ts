import { ref } from 'vue';
import { useAuthStore } from '@/store/modules/auth';
import { fetchMyPermissions } from '@/service/api/system';
import { fetchLibraries } from '@/service/api/biz';

/** 当前用户可用库下拉：超管取全部库；普通用户按权限（read/insert）推导 */
export function useLibraryOptions() {
  const options = ref<{ label: string; value: string }[]>([]);
  const perms = ref<any[]>([]);
  const isSuper = ref(false);

  async function loadFor(action: 'read' | 'insert') {
    const authStore = useAuthStore();
    isSuper.value = Boolean(authStore.userInfo.isSuperuser);
    if (isSuper.value) {
      const { data }: any = await fetchLibraries({ limit: 500 });
      options.value = (data || []).filter((l: any) => !l.deleted_at).map((l: any) => ({ label: `${l.name} (${l.slug})`, value: l.slug }));
    } else {
      const { data }: any = await fetchMyPermissions();
      perms.value = data || [];
      options.value = perms.value.filter((p: any) => p.actions.includes(action)).map((p: any) => ({ label: p.library_slug, value: p.library_slug }));
    }
    return options.value;
  }

  function can(slug: string, action: string) {
    if (isSuper.value) return true;
    const row = perms.value.find((p: any) => p.library_slug === slug);
    return Boolean(row && row.actions.includes(action));
  }

  return { options, perms, isSuper, loadFor, can };
}
