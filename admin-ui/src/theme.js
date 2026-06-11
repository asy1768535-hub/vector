// 暗色模式：切换 <html class="dark">，Element Plus 的 dark css-vars 自动接管。
import { ref } from 'vue';

const KEY = 'vk_dark';
export const isDark = ref(localStorage.getItem(KEY) === '1');

function apply() {
    document.documentElement.classList.toggle('dark', isDark.value);
}
apply();  // 启动即应用，避免闪烁

export function toggleDark() {
    isDark.value = !isDark.value;
    localStorage.setItem(KEY, isDark.value ? '1' : '0');
    apply();
}
