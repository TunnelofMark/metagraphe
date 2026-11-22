# metagraphe（转写）

> *μεταγραφή (metagraphē)* — 古希腊语“转写、摹录、抄写转译”。

一套面向**中文扫描版学术著作、人文社科书籍与地方志**的高精度 OCR 与出版级多格式交付流水线，将只读扫描 PDF 转换为四类高保真产物：

- **`<书名>_双层.pdf`**：原扫描图字节零修改（SHA256 逐页一致）+ 精细清除旧 OCR 残层 + `render_mode=3` 不可见检索文字层 + 多级书签；
- **`<书名>.epub`**：出版级 EPUB 3.3（通过 `EPUBCheck 0 fatals / 0 errors / 0 warnings`），兼容 **微信读书、Apple Books、多看、Kobo**；
- **`book.md` + `images/`**：原生扫描分辨率无损重裁插图/表格截图 + 跨页自动合段 + 跨页续表合并；
- **`book.json`**：保留原 PDF 1-based 页码与 display-space `bbox` 坐标的结构化语料。

---

## 核心特性

### 1. 四阶段可断点续跑流水线
1. **`recognize`（GPU 串行）**：`make_pages_json.py` → `run_mineru_full.sh`（MinerU 4.x，`--tier advanced --ocr-mode ocr`，忽略原 PDF 劣质旧文字层）。
2. **`layout`（GPU 串行）**：`run_layout_full.sh` → `run_layout.py`（PP-DocLayoutV3 独立版面检测，补强表格/插图框）。
3. **`post`（CPU 多线程）**：`post_full.sh` 依次执行 `adapt_mineru.py` → `hybrid_images.py` → `assemble.py`（+ `recrop.py` + `pandoc`）→ `epubcheck` → `build_pdf.py`（+ `textlayer.py`）。
4. **`qa`（CPU）**：`qa_book.py` 校验页数、原图 SHA256、旧文字层残留、体积比、书签、字符分布、`epubcheck`、空白页与重叠层；`review_suspects.py` 自动分诊疑难页并生成可视化 `review_report.html` 复核看板。

### 2. 出版级 EPUB 3 排版引擎（`scripts/assemble.py`）
- **三层自适应标题与目录**：地方志沿用 编/章/节 规则（`--split-level=2`）；普通学术/人文书籍自动对齐原 PDF 书签大纲并按大章切分文件（`--split-level=1`），默认移除冗余纸书扫描目录页（`--keep-toc-pages` 可保留），且与正版 EPUB 一致不将 `nav.xhtml` 塞入 `<spine>` 阅读流、清理废弃 `<guide>`。
- **跨平台弹窗脚注（`--footnote-mode {dual,weread,epub3}`）**：
  - 自动匹配每页底部 `footnote` 块（`①–⑳`、`*`、`[1]`/`〔1〕` 及 OCR 上标变体 `\*`/`^*`/`^6`）回正文对应位置，合并跨页续注，并消除页底脚注对跨页正文的打断；
  - 默认 `dual` 双标准合一：外层保留标准 EPUB 3 `<a epub:type="noteref" class="duokan-footnote">` + 篇末 `<aside epub:type="footnote">`，内层嵌入微信读书 `<img class="qqreader-footnote" alt="..." src="../media/note.png"/>`（自动生成 96×96 暖橙色圆形“注”徽标），实现 **Apple Books 与微信读书同文件双端原地弹窗**。
- **扉页 / 版权页 / 封底与版面几何还原**：
  - 封面双规范注入（EPUB 3 `properties="cover-image"` + 微信读书 `<meta name="cover">`），自动提取作者 `<dc:creator>` 与版权页 `<dc:publisher>`、`<dc:date>`、`urn:isbn:...`；
  - 识别并将封底挪至全书末尾、扉页整页图片显示、版权页独立分页且 CIP 与居中版权牌记分区排版；
  - 基于单栏页 PDF `bbox` 几何还原居中/右对齐段落，并自动识别整块缩进学术引文为 `<blockquote>`（楷体 + 缩进 + 左侧细灰线）。

### 3. 双层 PDF 精细化清理（`scripts/build_pdf.py`、`scripts/textlayer.py`）
- 清除超星 `/BHJXID /OCRText` 流后，基于 PDF 图形状态栈（`q`/`Q`/`Tr`）与全页不透明扫描图遮挡检测，仅剥离 `3 Tr` 不可见文字及被全页扫描图完全盖住的老式 `0 Tr` 旧 OCR 层（含 Form XObject），保护真正可见矢量文字与非文本 `/ExtGState`。
- 行内公式 `equation_inline` 自动内联拼接并清洗 LaTeX 残留；跨页续表透传 `continues_prev` 自动合并；多图页裁剪自动回退至 300 dpi 渲染裁剪。

---

## 环境部署（Windows + WSL2 Ubuntu 24.04）

依赖环境位于 WSL2 下的 `/opt/ocr/{mineru,paddle,post}`，完整安装脚本与已验证版本快照见：
- 安装脚本：[scripts/install_wsl.sh](scripts/install_wsl.sh)
- 版本快照：[scripts/env_versions.lock.json](scripts/env_versions.lock.json)

```bash
# 在 WSL2 Ubuntu 24.04 中初始化独立虚拟环境（MinerU / Paddle / Post）
bash scripts/install_wsl.sh
```

---

## 常用命令

```bash
# 1. 源 PDF 预检（只读）
/opt/ocr/post/bin/python scripts/inspect_pdf.py <source.pdf> 1-20

# 2. 单书 / 批量全流程处理（自动断点续跑）
bash scripts/run_all.sh --only <书名stem>

# 3. 单独重新打包 EPUB（例如调整脚注模式或样式后，无需重跑 OCR）
/opt/ocr/post/bin/python scripts/assemble.py <out_dir>/work/doc_hybrid.json <source.pdf> <out_dir> \
  --title "<书名>" --no-recrop --footnote-mode dual
java -jar /opt/ocr/epubcheck/epubcheck.jar <out_dir>/book.epub

# 4. 疑难页自动分诊与可视化复核看板（只读）
/opt/ocr/post/bin/python scripts/review_suspects.py <out_dir> --out /tmp/review_out
/opt/ocr/post/bin/python scripts/review_suspects.py --batch <batch_dir> --out /tmp/batch_triage.json

# 5. 离线回归测试（在自动清理的临时目录运行，不改动成品）
/opt/ocr/post/bin/python scripts/test_pipeline_fixes.py
```

---

## 目录说明

- `scripts/run_all.py` / `run_all.sh`：批量调度、断点续跑与磁盘余量保护
- `scripts/adapt_mineru.py` / `adapt_paddle.py` / `hybrid_images.py`：OCR 输出标准化与版面融合
- `scripts/assemble.py` / `recrop.py`：出版级 EPUB 3 与 Markdown 装配、无损插图重裁
- `scripts/build_pdf.py` / `textlayer.py`：双层 PDF 旧层剥离与不可见文字层写入
- `scripts/qa_book.py` / `review_suspects.py` / `verify_pdf.py`：质量验收与疑难页复核看板
- `scripts/test_pipeline_fixes.py`：核心修复项离线回归测试
