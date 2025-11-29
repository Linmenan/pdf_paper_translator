# 🚀 本地化 AI 学术文献翻译系统 (Local Paper Translator)

## 0. 快速开始 (Quick Start) [TODO]

1.  **环境准备**: 确保安装 Python 3.8+ 及 Node.js (可选，目前前端通过 CDN 引入)。
2.  **依赖安装**: `pip install -r requirements.txt` (需包含 PyMuPDF, openai, flask/http.server 等)。
3.  **启动服务**: 运行 `python server.py` (或对应入口脚本)。
4.  **访问**: 浏览器自动打开 `http://localhost:8000`。
5.  **数据准备**: 将需翻译的 PDF 文件放入 `pdf_input` (或其他指定目录)。

## 1\. 系统概述

这是一个集成了 **Web 可视化标注、PDF 智能拆分、LLM 双模翻译** 以及 **人机回环纠错 (HITL)** 的全流程本地化工具。

系统采用 **Vue.js 3** 前端 + **Python** 后端架构，彻底重构了交互流程。它不仅解决了传统翻译工具公式丢失、图表错位的痛点，还引入了类似 CAT (计算机辅助翻译) 的 **任务列表视图**，允许用户在翻译前预览拆分结果，在翻译后进行所见即所得的微调。

## 2\. 核心特性 (Key Features)

### A. 新一代 Web 交互标注 (Fabric.js)

- **Web 端可视化编辑器**: 替代旧版 Tkinter，提供更流畅的标注体验。支持框选、缩放、平移。
- **智能属性继承**: 当进入新页面且无正文范围时，自动继承上一页的 `ContentArea` 位置，减少重复操作。
- **UUID 精确追踪**: 引入 UUID 替代单纯的数字 ID 作为对象唯一标识，彻底解决修改 ID 导致的引用丢失和选中失效问题。
- **即时响应**: 属性修改 (ID/Role) 实时生效，无需回车确认；支持 Esc 键全局取消选中。

### B. 结构化任务流 (Task-Based Workflow)

- **任务列表视图 (Task Dashboard)**: 在提取 (Step 1) 和 翻译 (Step 2) 之间新增可视化缓冲区。用户可以直接查看 PDF 被拆分成的 JSON 任务列表，核对段落、图表和标题的提取准确性。
- **四段式智能切分**: 将全文拆解为 **Meta (元数据)**、**Body (正文)**、**Assets (图表资源)**、**References (参考文献)**。参考文献将被自动隔离，不消耗翻译 Token。
- **Tag 注入与保护**: 自动识别 `Fig. 1`、`[1]` 等引用，转换为 `[[LINK:...]]` 标签，确保 LLM 输出格式稳定。

### C. 双模型协作与纠错

- **初译模式 (Fast Pass)**: 使用轻量模型 (如 `qwen2.5:7b`) 快速处理 JSON 任务队列。
- **纠错模式 (Deep Correction)**: 用户在 Web 端提交反馈后，系统自动切换至强逻辑模型 (如 `qwen2.5:14b`)，将 **"原文 + 旧译文 + 用户指引"** 同时输入，进行精准修复。

### D. 全流程体验优化

- **全屏状态反馈**: 内置全屏 Loading 遮罩，实时显示 "正在提取"、"正在翻译" 或 "正在生成报告" 等后台状态。
- **所见即所得报告**: 翻译结果直接在 Web 端通过 iframe 预览，支持点击段落唤起纠错面板。

## 3\. 工程架构

```
local_paper_translate/
├── static/                   # [Frontend] Web 静态资源
│   ├── css/
│   │   └── style.css         # 全局样式 (含 Loading, Task Card)
│   └── js/
│       ├── app.js            # Vue 主逻辑 (状态管理, API 调用)
│       ├── editor.js         # Canvas 编辑器 (Fabric.js 封装)
│       ├── api.js            # 后端 API 接口层
│       └── config.js         # 工具配置
├── index.html                # [Frontend] 单页应用入口
├── workflow_utils.py         # [Backend] 核心算法 (提取/清洗/LLM/生成)
├── server.py (或主 Notebook)  # [Backend] HTTP Server & API 路由
├── extracted_output/         # [Data] 中间产物：包含按区域截图的 png 和 layout_config.json
├── llm_output/               # [Data] {Paper_title}_llm_cache.json：存储 LLM 翻译任务队列、原文、译文及用户纠错记录
└── vis_output/               # [Data] 最终 HTML 报告
```

## 4\. 详细工作流 (Workflow)

系统将翻译过程标准化为三个核心步骤，用户可通过顶部导航栏切换：

### Step 1: 布局标注 (Layout Annotation)

- **界面**: 左侧工具栏 + 右侧 Canvas 画布。
- **操作**:
  1.  使用 `1-9` 快捷键切换工具 (图、表、公式、标题等)。
  2.  框选页面元素，使用 `Del` 删除错误框。
  3.  点击 **"⚡ 开始提取"**：后端保存布局 -\> 调用 PyMuPDF 提取内容 -\> 生成中间态 JSON -\> 自动跳转 Step 2。

### Step 2: 任务列表 (Task Dashboard)

- **界面**: 左右对照的卡片列表 (Source Text vs Placeholder)。
- **功能**:
  - 展示后端提取到的 `translationTasks` (JSON 数据)。
  - 显示统计信息 (总段落数、字符数)。
  - 用户确认提取无误后，点击 **"🚀 开始翻译"**：后端启动 LLM 并行翻译 -\> 生成 HTML -\> 自动跳转 Step 3。

### Step 3: 结果预览 (Result Preview)

- **界面**: 内嵌 Iframe 显示最终生成的 HTML 报告。
- **交互**:
  - **浏览模式**: 查看双语对照排版。
  - **纠错模式**: 点击译文段落 -\> 输入修改意见 -\> 保存 -\> 点击 "刷新报告" 触发重译。

## 5\. 数据流转说明

1.  **PDF -\> Layout JSON**: 用户在 Web 端使用 [标注工具](./docs/editor.md) 标注，生成坐标数据。
2.  **Layout JSON -\> Extract JSON**: 后端 `extract_text_and_save_assets_smart` 函数根据坐标提取文本和截图，生成 `_llm_cache.json` 的初始版本 (状态为 `pending`)。
3.  **Extract JSON -\> Translated JSON**: 后端 `run_smart_analysis` 读取任务列表，调用 LLM 填充 `trans` 字段，更新状态为 `success`。
4.  **Translated JSON -\> HTML**: 后端 `generate_html_report` 组装最终页面。

## 6\. 常见问题 (FAQ)

- **Q: 出现如当一个页面上有公式 1，2，3 三个框时，我将第一个 id 改为 2，改第二个为 3 时，发现第一个变为了 3，并且又都无法选中了？**
  - **A:** [FIXME] 已修改，实测未修复。现在系统使用 UUID 作为内部唯一索引，修改显示 ID 不会影响对象的物理引用。
- **Q: 为什么翻页后正文框没有了？**
  - **A:** 系统已加入智能继承逻辑。如果下一页没有正文框，会自动复制上一页的 `ContentArea` 位置。
- **Q: 提取或翻译时间过长怎么办？**
  - **A:** 界面会显示全屏 Loading 遮罩阻断操作，请耐心等待后台处理完毕，不要刷新页面。
    **Q: 再次回到总览状态**
