# PDF 图片页 OCR 实施计划

> **状态：✅ 已实施（2026-06-24）。**下文“## 四、实施任务”各项均已完成并通过六类真实 PDF 文件验收与 OCR 文档更新/删除闭环（验收记录本地留档）。测试：`pytest -q` → **174 passed, 12 skipped**；`ruff check app tests` 通过。本文档保留作设计与实施记录。

> **给执行智能体：**按任务顺序实施，先写失败测试，再写最小代码；不要顺带重构摄入架构，不要提交真实发票、合同或其他业务文件。

**目标：**在现有文字版 PDF 摄入基础上，支持扫描 PDF 和“文字页 + 图片页”的混合 PDF，并复用已有库级 `ocr_enabled` 与本地 RapidOCR。

**架构：**`pypdf` 继续读取 PDF 文字层；新增独立 `pdf_extract` 服务逐页判断。页面文字达到阈值时直接使用文字层，否则仅在 OCR 开启时用 `pypdfium2` 将该页渲染为 PNG，再交给现有 `ocr.ocr_image()`。输出按页码合并并带 `【第 N 页】` 标记。

**技术栈：**pypdf、pypdfium2、RapidOCR/ONNX Runtime、现有 FastAPI 上传入口与 pytest。

---

## 一、范围与非目标

### 本轮实现

- 文字 PDF 保持现有行为，不触发 OCR。
- 纯扫描 PDF 在库级 `ocr_enabled=true` 时逐页 OCR。
- 混合 PDF 按页选择文字层或 OCR，保持原页序。
- OCR 默认关闭；不开启时，纯图片 PDF 返回清晰的 400。
- OCR 依赖缺失、PDF 损坏、OCR 页数超限时返回明确错误。
- 页面顺序和页码进入标准化正文，便于检索结果追踪来源。

### 本轮不实现

- 不恢复图片表格的行列结构；OCR 只输出文本行。
- 不处理手写体、印章语义、版面关系和票据字段结构化。
- 不把 PDF 解析从 API 搬到异步 Worker。
- 不接任何云 OCR，不新增 API Key。
- 不修改文档 revision、Outbox、检索和权限逻辑。

---

## 二、拟修改文件

- 新建：`app/services/pdf_extract.py`，负责逐页文字提取、低文字页渲染和 OCR 合并。
- 修改：`app/api/documents.py`，PDF 分支调用新服务并映射错误。
- 修改：`app/config.py`，增加 PDF OCR 安全参数。
- 修改：`pyproject.toml`，OCR extra 增加 `pypdfium2` 与 Pillow。
- 修改：`.env.example`，增加 PDF OCR 参数示例。
- 修改：`admin-ui/src/views/Libraries.js`，把 OCR 说明从“DOCX 图片”改成“DOCX 图片和 PDF 扫描页”。
- 新建：`tests/test_pdf_extract.py`。
- 修改：`tests/test_query_import_api.py`，补上传端点契约测试。
- 修改：`README.md`、`docs/05-configuration.md`、`docs/09-document-ingest.md`、`docs/16-testing.md`。

---

## 三、固定设计

### 1. 服务接口

`app/services/pdf_extract.py` 对外只暴露：

```python
class PdfExtractError(ValueError):
    pass


class PdfOcrUnavailableError(PdfExtractError):
    pass


def extract_pdf_text(
    data: bytes,
    *,
    ocr_enabled: bool,
    ocr: Callable[[bytes], str] | None,
    min_text_chars: int,
    render_dpi: int,
    max_ocr_pages: int,
) -> str:
    ...
```

内部函数：

```python
def _meaningful_char_count(text: str) -> int:
    return len("".join(text.split()))


def _render_page_png(data: bytes, page_index: int, dpi: int) -> bytes:
    ...
```

`_render_page_png()` 懒加载 `pypdfium2`，只渲染需要 OCR 的单页，不提前渲染整份 PDF。

### 2. 逐页选择规则

对每一页执行：

```text
page.extract_text()
  ├─ 非空白字符数 >= PDF_OCR_MIN_TEXT_CHARS：使用文字层
  └─ 少于阈值：
       ├─ OCR 未开启：跳过该页
       ├─ OCR 开启但引擎不可用：抛 PdfOcrUnavailableError
       └─ OCR 开启：渲染该页 -> ocr_image -> 使用识别文本
```

结果格式：

```text
【第 1 页】
第一页文字

【第 2 页】
第二页 OCR 文字
```

若所有页面最终都没有文字：

- OCR 关闭：`PDF 无可提取文本；如为扫描件，请在知识库开启图片 OCR`。
- OCR 开启：`PDF OCR 后仍无可识别文本`。

单个空白页允许跳过；不能因为空白页让整份 PDF 失败。

### 3. 资源限制

新增配置：

```python
pdf_ocr_min_text_chars: int = 20
pdf_ocr_render_dpi: int = 200
pdf_ocr_max_pages: int = 50
```

约束：

- `min_text_chars >= 0`
- `render_dpi` 建议 150~300，默认 200。
- `max_pages` 统计实际进入 OCR 的页面，不限制纯文字页数量。
- 第 `max_pages + 1` 个 OCR 页面出现时停止并返回 400，不继续消耗 CPU。

### 4. 依赖选择

`pyproject.toml`：

```toml
ocr = [
    "rapidocr_onnxruntime>=1.2",
    "pypdfium2",
    "Pillow>=10",
]
```

不引入 PyMuPDF，避免 AGPL/商业许可问题；不使用 `pdf2image`，避免额外安装 Poppler。

---

## 四、实施任务

> ✅ 任务 1–6 已全部实施完成（2026-06-24）。以下复选框保留为实施清单存档。

### 任务 1：依赖与配置

- [x] 在 `pyproject.toml` 的 `ocr` extra 添加 `pypdfium2`、`Pillow>=10`。
- [x] 在 `pyproject.toml` 中说明 PDF OCR 由 `pip install -e ".[ocr]"` 安装。
- [x] 在 `app/config.py` 添加三个 `pdf_ocr_*` 配置。
- [x] 在 `.env.example` 添加：

```env
PDF_OCR_MIN_TEXT_CHARS=20
PDF_OCR_RENDER_DPI=200
PDF_OCR_MAX_PAGES=50
```

- [x] 运行配置相关测试，确认未安装 OCR extra 时应用仍可启动。

### 任务 2：先写 PDF 服务失败测试

在 `tests/test_pdf_extract.py` 增加：

- [x] `test_text_pages_do_not_call_ocr`
- [x] `test_scanned_page_uses_ocr_when_enabled`
- [x] `test_mixed_pdf_keeps_text_and_ocr_in_page_order`
- [x] `test_scanned_pdf_without_ocr_reports_enable_hint`
- [x] `test_ocr_enabled_but_engine_missing_reports_dependency_error`
- [x] `test_blank_page_does_not_fail_other_pages`
- [x] `test_all_pages_empty_after_ocr_is_error`
- [x] `test_max_ocr_pages_stops_before_rendering_extra_page`
- [x] `test_corrupt_pdf_is_clear_error`

测试不得依赖真实 OCR 模型：使用 monkeypatch 替换 `pypdf.PdfReader`、`_render_page_png` 和 OCR callback，断言调用页码、顺序和错误消息。

运行：

```powershell
pytest tests/test_pdf_extract.py -q
```

预期：新服务尚未创建，测试失败。

### 任务 3：实现最小 PDF 提取服务

- [x] 新建 `app/services/pdf_extract.py`。
- [x] 使用 `pypdf.PdfReader(io.BytesIO(data))` 读取页数和文字层。
- [x] 只对低文字页调用 `_render_page_png()`。
- [x] 每次只持有一页 PNG 字节，OCR 完成立即释放引用。
- [x] 用 `【第 N 页】` 合并有效页面。
- [x] PDF 加密、损坏和页读取异常统一转换成 `PdfExtractError`，错误中不包含正文或密钥。
- [x] 运行 `pytest tests/test_pdf_extract.py -q`，预期全部通过。

### 任务 4：接入上传 API

- [x] 在 `tests/test_query_import_api.py` 先增加端点测试：
  - OCR 关闭的文字 PDF 正常入库。
  - OCR 关闭的扫描 PDF 返回 400 和开启 OCR 提示。
  - OCR 开启时把库级开关与配置参数传给 `extract_pdf_text()`。
  - 服务抛 `PdfOcrUnavailableError` 时返回 400，提示安装 `.[ocr]`。
- [x] 将 `app/api/documents.py` 的 `.pdf` 分支替换为新服务调用。
- [x] `eff_ocr = lib.ocr_enabled if lib.ocr_enabled is not None else settings.ocr_enabled`，与 DOCX 保持一致。
- [x] 不修改 JSON/CSV/DOCX/XLSX 分支。
- [x] 运行：

```powershell
pytest tests/test_pdf_extract.py tests/test_query_import_api.py -q
```

### 任务 5：更新后台提示与文档

- [x] `admin-ui/src/views/Libraries.js` 创建和编辑弹窗均改为：

```text
开启后会识别 DOCX 内嵌图片及 PDF 扫描页；处理更慢，敏感文件请使用本地 OCR。
```

- [x] README 文件格式表将 PDF 改为“文字层直接提取；开启 OCR 后支持扫描页和混合 PDF”。
- [x] `docs/05-configuration.md` 增加三个 PDF OCR 配置。
- [x] `docs/09-document-ingest.md` 写清逐页回退逻辑与限制。
- [x] `docs/16-testing.md` 增加 PDF OCR 测试命令。
- [x] 不宣称支持票据字段结构化或图片表格行列还原。

### 任务 6：完整验证

- [x] 默认环境不装 OCR extra：

```powershell
pytest -q
ruff check app/services/pdf_extract.py app/api/documents.py tests/test_pdf_extract.py
git diff --check
```

- [x] OCR 环境：

```powershell
pip install -e ".[ocr]"
python -c "from app.services import ocr; print(ocr.is_available())"
```

预期输出 `True`。

- [x] 使用三份不含真实业务数据的样本做手工验收：
  1. 两页文字 PDF，OCR 关闭也能入库并检索。
  2. 两页扫描 PDF，OCR 开启后能检索第二页唯一测试句。
  3. 第一页文字、第二页图片的混合 PDF，两页唯一测试句都能召回。
- [x] OCR 关闭上传扫描 PDF，确认返回明确 400。
- [x] 检查任务完成后没有临时文件残留。

---

## 五、完成标准

- 文字 PDF 行为不退化。
- 扫描 PDF 与混合 PDF 在库级 OCR 开启后可摄入。
- 仅低文字页触发 OCR，顺序和页码正确。
- OCR 默认关闭，未安装可选依赖时基础服务仍能运行。
- 超过 OCR 页数限制时快速失败，不继续渲染。
- 不引入 PyMuPDF、云 OCR、业务样本或真实发票。
- 全量 pytest、Ruff 和 `git diff --check` 通过。

