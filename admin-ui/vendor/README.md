# 前端第三方依赖（本地打包，不访问公网 CDN）

| 文件 | 来源 | 版本 | 许可证 |
|---|---|---|---|
| `vue.esm-browser.prod.js` | https://github.com/vuejs/core | 3.4.27 | MIT |
| `vue-router.esm-browser.prod.js` | https://github.com/vuejs/router | 4.5.1 | MIT |
| `element-plus.full.min.mjs` | https://github.com/element-plus/element-plus | 2.7.6 | MIT |
| `element-plus.index.css` | 同上（Element Plus 主样式） | 2.7.6 | MIT |
| `element-plus.dark-css-vars.css` | 同上（暗色变量） | 2.7.6 | MIT |
| `element-plus-locale-zh-cn.mjs` | 同上（中文语言包） | 2.7.6 | MIT |
| `marked.esm.js` | https://github.com/markedjs/marked | 12.0.2 | MIT |
| `dompurify.es.mjs` | https://github.com/cure53/DOMPurify | 3.1.6 | Apache-2.0 / MPL-2.0 |
| `cytoscape.esm.min.mjs` | https://github.com/cytoscape/cytoscape.js | 3.34.0 | MIT |

`cytoscape.LICENSE` contains the upstream Cytoscape.js license text.

## 更新方式

手动从各项目 GitHub Releases 下载 ESM/浏览器构建产物放入本目录，
更新本文件中的版本号，并确认 `index.html` 的 importmap 引用正确。

## 注意事项

- 所有文件已本地化，页面运行时**零公网请求**。
- 图标已内置到 `src/icons.js`，不依赖 Iconify API。
- 不要将编译产物（如整个 node_modules）放入本目录。
