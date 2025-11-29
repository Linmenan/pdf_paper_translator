import { TOOL_CONFIG, API_BASE } from "./config.js";
import { ApiService } from "./api.js";
import { CanvasEditor } from "./editor.js";

const { createApp, nextTick } = Vue;

// [新增] 1. 在文件顶部或组件 data 外部定义中英文映射
const TYPE_CN_MAP = {
  Figure: "图",
  Table: "表",
  Equation: "公式",
  Algorithm: "算法",
  Title: "标题",
  Author: "作者",
  Mask: "遮罩",
  Header: "章节头",
  ContentArea: "正文范围",
};

createApp({
  data() {
    return {
      papers: [],
      currentPaper: null,
      step: 1,
      pageIdx: 0,
      totalPages: 1,
      currentTool: null,
      tools: TOOL_CONFIG,
      layoutData: {},
      historyStack: [],
      hasUnsavedChanges: false,
      editor: null,
      reportUrl: "",
      selectedItem: null,
      pendingId: 1,
      pendingRole: "Body",

      // Loading 状态
      isBusy: false,
      busyMsg: "Processing...",

      // === 翻译任务数据 (Step 2) ===
      translationTasks: [],
      promptTemplates: {},
      currentRefMap: "", // [新增] 用于存储当前论文的引用映射表
      isTooltipLocked: false, // [新增] 锁定状态
      eventSource: null, // [新增] SSE 连接对象
      isTranslating: false,
    };
  },
  computed: {
    // 标注列表分组逻辑
    groupedItems() {
      // 1. 获取当前页数据
      if (!this.layoutData[String(this.pageIdx)]) return [];
      let raw = this.layoutData[String(this.pageIdx)];

      // 2. 如果处于特定工具模式，只看该类元素
      if (this.currentTool) {
        raw = raw.filter((x) => x.type === this.currentTool);
      }

      // 3. 按 "类型_ID" 进行聚合 (将 Body 和 Caption 合并显示)
      const map = {};
      raw.forEach((item) => {
        // 对于 ContentArea/Mask/Title/Author/Header，通常 ID 意义不大或为 0，
        // 我们特殊处理 key 以便它们也能分组
        const key = `${item.type}_${item.id}`;

        if (!map[key]) {
          map[key] = {
            id: item.id,
            type: item.type,
            uuid: item.uuid, // 携带 uuid 用于选中
            children: [],
          };
        }
        map[key].children.push(item);
      });

      // 4. 生成列表显示对象
      return Object.values(map)
        .map((group) => {
          const cnType = TYPE_CN_MAP[group.type] || group.type;

          // 逻辑：构建清晰的显示文本
          // 以前可能是: Figure #1
          // 现在改为: 图 (ID: 1) 或 遮罩 (ID: 1)
          let label = "";
          let subInfo = "";

          // 特殊类型通常不需要强调 ID
          const isSingleton = [
            "ContentArea",
            "Mask",
            "Title",
            "Author",
            "Header",
          ].includes(group.type);

          if (isSingleton) {
            // 如果是遮罩/正文，可能更关心它是本页的第几个
            label = `${cnType}`;
            // 如果 ID > 0 或者是 Header，显示 ID 辅助区分
            if (group.id > 0 || group.type === "Header") {
              label += ` (ID: ${group.id})`;
            }
          } else {
            // 图表公式，强制显示 ID，明确这是“文中编号”
            label = `${cnType} (编号: ${group.id})`;
          }

          // 统计子区域 (例如: 含截图+标题)
          const roles = group.children
            .map((c) => (c.role === "Body" ? "截图" : "文字"))
            .join("+");
          subInfo = roles ? `[${roles}]` : "";

          return {
            ...group,
            displayLabel: label, // <--- 核心改动：UI 应该绑定这个字段
            subInfo: subInfo, // <--- 辅助信息
          };
        })
        .sort((a, b) => {
          // 排序：先按类型聚类，再按 ID 排序
          if (a.type !== b.type) return a.type.localeCompare(b.type);
          return a.id - b.id;
        });
    },
    // [新增 1] 统计已完成任务数
    completedTaskCount() {
      // 安全检查：如果不是数组，返回 0
      if (!Array.isArray(this.translationTasks)) return 0;
      return this.translationTasks.filter((t) => t.status === "success").length;
    },

    // [新增 2] 计算按钮显示的文字
    translationBtnLabel() {
      // 安全检查：如果不是数组，返回默认值
      if (!Array.isArray(this.translationTasks)) return "🚀 开始翻译";
      const hasProgress = this.translationTasks.some(
        (t) => t.status === "success"
      );
      return hasProgress ? "▶️ 继续翻译" : "🚀 开始翻译";
    },
    // === Step 2 任务统计 ===
    taskStats() {
      // 安全检查
      const tasks = Array.isArray(this.translationTasks)
        ? this.translationTasks
        : [];
      return {
        total: tasks.length,
        chars: tasks.reduce(
          (acc, cur) => acc + (cur.text ? cur.text.length : 0),
          0
        ),
      };
    },
  },
  async mounted() {
    await this.loadPapers();
    await this.loadPrompts(); // [新增] 启动时拉取 Prompt
    window.addEventListener("keydown", this.handleKey);
    window.addEventListener("beforeunload", (e) => {
      if (this.hasUnsavedChanges) e.returnValue = "Unsaved";
    });
  },
  methods: {
    async loadPapers() {
      try {
        this.papers = await ApiService.getPapers();
      } catch (e) {
        alert("Server Error: " + e.message);
      }
    },
    // [新增] 获取后端 Prompt 配置
    async loadPrompts() {
      try {
        const res = await fetch(`${API_BASE}/api/config/prompts`);
        if (res.ok) {
          this.promptTemplates = await res.json();
          console.log("✅ Prompts loaded from server");
        }
      } catch (e) {
        console.error("Failed to load prompts:", e);
        // 可以在这里写个兜底的 fallback，或者直接留空
      }
    },
    async selectPaper(p) {
      if (this.hasUnsavedChanges && !confirm("Discard changes?")) return;

      // 1. 清理旧状态 (必须保留)
      this.closeSSE();
      this.isTranslating = false;

      this.currentPaper = p;
      this.pageIdx = 0;
      this.layoutData = {};
      this.reportUrl = "";
      this.step = 1; // 默认 Step 1
      this.hasUnsavedChanges = false;
      this.historyStack = [];
      this.selectedItem = null;
      this.translationTasks = []; // 先置空

      // 2. 加载布局数据 (Step 1 数据)
      try {
        this.layoutData = await ApiService.getLayout(p.filename);
      } catch (e) {
        console.warn("Layout load failed", e);
      }

      // 3. [核心修复] 预加载任务数据 (Step 2 数据)
      // 只要文件处理过（状态不是未开始），就尝试加载任务列表
      // 这样无论进入 Step 2 还是 Step 3，切换 Tab 时数据都在
      if (p.status !== "未开始") {
        try {
          const res = await ApiService.getExtractData(p.filename);
          if (Array.isArray(res)) {
            this.translationTasks = res;
            this.currentRefMap = "";
          } else {
            this.translationTasks = res.tasks || [];
            this.currentRefMap = res.ref_map || "";
          }
        } catch (e) {
          console.warn("尝试预加载任务数据失败 (可能文件被删):", e);
        }
      }

      // 4. 根据状态决定初始显示的页面 (Step Router)
      const s = p.status;

      if (s === "已完成" || s === "翻译完成") {
        // 如果已完成，优先看报告 (Step 3)
        // 但因为上面已经加载了 Tasks，所以你手动切回 Step 2 也能看到数据了
        this.step = 3;
        if (s === "已完成") this.generateReport();
      } else if (s.includes("已提取") || s.includes("翻译中")) {
        // 如果是中间状态，进入任务列表 (Step 2)
        if (this.translationTasks.length > 0) {
          this.step = 2;
        } else {
          // 如果状态显示已提取，但读不到数据，回退到 Step 1
          this.step = 1;
        }
      } else {
        // 未开始 -> Step 1
        this.step = 1;
      }

      await nextTick();
      if (this.step === 1) this.initEditor();
    },
    // [新增] 销毁时清理
    beforeUnmount() {
      this.closeSSE();
    },
    goBack() {
      if (this.hasUnsavedChanges && !confirm("Discard changes?")) return;
      if (this.editor) this.editor.dispose();
      this.editor = null;
      this.currentPaper = null;
      this.hasUnsavedChanges = false;
      this.loadPapers();
    },
    initEditor() {
      if (this.editor) this.editor.dispose();
      this.editor = new CanvasEditor("c", this.tools);
      this.editor.init();

      this.setTool(null);

      this.editor.onObjectAdded = (newItem) => this.handleObjectAdded(newItem);
      this.editor.onObjectRemoved = (data) => this.handleObjectRemoved(data);
      this.editor.onObjectModified = (data) => this.handleObjectModified(data);

      this.editor.onSelectionUpdated = (data) => {
        const pageList = this.layoutData[String(this.pageIdx)];
        let item = null;
        if (data.uuid) item = pageList.find((x) => x.uuid === data.uuid);
        else
          item = pageList.find(
            (x) =>
              x.type === data.type &&
              x.id === data.id &&
              x.rect[0] === data.rect[0]
          );
        this.selectedItem = item;
        if (item) {
          if (this.currentTool !== item.type) {
            this.setTool(item.type);
          }
          this.pendingId = item.id;
          this.updateEditorState();
        }
      };
      this.editor.onSelectionCleared = () => {
        this.selectedItem = null;
      };
      this.loadPageImage();
    },

    async loadPageImage() {
      if (!this.currentPaper || !this.editor) return;
      this.selectedItem = null;
      try {
        const data = await ApiService.getPageImage(
          this.currentPaper.filename,
          this.pageIdx
        );
        this.totalPages = data.total_pages;

        this.editor.clear();
        await this.editor.setBackground(data.image, data.width, data.height);

        if (!this.layoutData[String(this.pageIdx)]) {
          this.layoutData[String(this.pageIdx)] = [];
        }
        const items = this.layoutData[String(this.pageIdx)];
        // [建议新增] 防御性代码：为历史数据补全 UUID
        items.forEach((item) => {
          if (!item.uuid) item.uuid = crypto.randomUUID();
        });
        // ContentArea 继承逻辑
        const hasContentArea = items.some((x) => x.type === "ContentArea");
        if (!hasContentArea && this.pageIdx > 0) {
          const prevItems = this.layoutData[String(this.pageIdx - 1)];
          if (prevItems) {
            const prevCA = prevItems.find((x) => x.type === "ContentArea");
            if (prevCA) {
              const newCA = JSON.parse(JSON.stringify(prevCA));
              newCA.uuid = crypto.randomUUID();
              items.push(newCA);
              this.hasUnsavedChanges = true;
            }
          }
        }

        this.editor.renderLayoutItems(items);
        this.editor.updateMode(this.currentTool);
      } catch (e) {
        console.error(e);
      }
    },

    setTool(type) {
      this.currentTool = type;
      if (type) {
        this.pendingId = this.getNextId(type);
        this.pendingRole = "Body";
      }
      if (this.editor) {
        this.editor.updateMode(type);
        this.updateEditorState();
      }
    },

    // === 核心修复 2：Allow Escape in Input ===
    handleKey(e) {
      if (!this.currentPaper) return;
      const activeTag = document.activeElement.tagName;
      // 判断当前是否在输入框内
      const isInput = activeTag === "INPUT" || activeTag === "TEXTAREA";

      // --- 优先级 1: 全局系统级快捷键 (无视焦点在哪里) ---

      // Ctrl + S: 保存
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        this.saveLayout(true);
        return; // 阻止后续逻辑
      }

      // Ctrl + Z: 撤销
      // (特殊逻辑: 如果在输入框内，让浏览器处理文本撤销；如果不在，处理 Canvas 撤销)
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        if (!isInput) {
          e.preventDefault();
          this.undo();
        }
        return;
      }

      // --- 优先级 2: 输入框安全守卫 ---
      // 如果正在输入文字，且按下的不是 Escape，则屏蔽 Canvas 操作快捷键 (如 1, 2, Del, Q 等)
      if (isInput && e.key !== "Escape") {
        return;
      }

      // --- 优先级 3: Canvas 交互快捷键 ---

      // ESC: 取消选中 / 输入框失焦
      if (e.key === "Escape") {
        if (this.editor && this.editor.canvas.getActiveObject()) {
          this.editor.discardActiveObject();
        } else {
          this.setTool(null);
        }
        // 关键：如果在输入框里按 ESC，强制失焦，方便用户立即使用快捷键切换工具
        if (isInput) {
          document.activeElement.blur();
        }
        return;
      }

      // Delete: 删除
      if (e.key === "Delete" && this.editor) this.editor.deleteSelected();

      // 1-9: 切换工具
      if (e.key >= "1" && e.key <= "9") {
        const idx = parseInt(e.key) - 1;
        if (this.tools[idx]) this.setTool(this.tools[idx].type);
      }

      // Q: 切换 Role
      if (e.key.toLowerCase() === "q") this.togglePendingRole();

      // 翻页
      if ([" ", "ArrowRight", "ArrowDown"].includes(e.key)) {
        e.preventDefault();
        this.nextPage();
      }
      if (["ArrowLeft", "ArrowUp"].includes(e.key)) {
        e.preventDefault();
        this.prevPage();
      }
    },
    pushHistory() {
      const items = this.layoutData[String(this.pageIdx)] || [];
      this.historyStack.push({
        pageIdx: this.pageIdx,
        items: JSON.parse(JSON.stringify(items)),
      });
      if (this.historyStack.length > 30) this.historyStack.shift();
    },
    undo() {
      if (this.historyStack.length === 0) return;
      const snapshot = this.historyStack.pop();
      if (this.pageIdx !== snapshot.pageIdx) {
        this.pageIdx = snapshot.pageIdx;
        this.loadPageImage().then(() =>
          this.restoreSnapshot(snapshot.pageIdx, snapshot.items)
        );
      } else {
        this.restoreSnapshot(snapshot.pageIdx, snapshot.items);
      }
    },
    restoreSnapshot(pageIdx, items) {
      this.layoutData[String(pageIdx)] = items;
      this.editor.renderLayoutItems(items);
      this.selectedItem = null;
      this.hasUnsavedChanges = true;
      if (this.currentTool) this.pendingId = this.getNextId(this.currentTool);
      this.updateEditorState();
    },
    togglePendingRole() {
      if (["Figure", "Table", "Algorithm"].includes(this.currentTool)) {
        this.pendingRole = this.pendingRole === "Body" ? "Caption" : "Body";
        this.updateEditorState();
      }
    },
    handleObjectAdded(newItem) {
      this.pushHistory();
      if (!newItem.uuid) newItem.uuid = crypto.randomUUID();
      if (newItem.type === "ContentArea") {
        this.layoutData[String(this.pageIdx)] = (
          this.layoutData[String(this.pageIdx)] || []
        ).filter((x) => x.type !== "ContentArea");
      }
      if (!this.layoutData[String(this.pageIdx)])
        this.layoutData[String(this.pageIdx)] = [];
      this.layoutData[String(this.pageIdx)].push(newItem);
      this.hasUnsavedChanges = true;
      if (
        newItem.role === "Body" &&
        ["Figure", "Table"].includes(newItem.type)
      ) {
        this.pendingRole = "Caption";
      }
      this.updateEditorState();
    },
    handleObjectRemoved(data) {
      this.pushHistory();
      const list = this.layoutData[String(this.pageIdx)];
      let idx = -1;
      if (data.uuid) idx = list.findIndex((x) => x.uuid === data.uuid);
      else
        idx = list.findIndex(
          (x) =>
            x.type === data.type &&
            x.id === data.id &&
            x.rect[0] === data.rect[0]
        );
      if (idx > -1) {
        list.splice(idx, 1);
        this.hasUnsavedChanges = true;
        this.selectedItem = null;
      }
    },
    handleObjectModified(mod) {
      this.pushHistory();
      const list = this.layoutData[String(this.pageIdx)];
      let t = null;
      if (mod.uuid) t = list.find((x) => x.uuid === mod.uuid);
      else
        t = list.find(
          (x) =>
            x.type === mod.type && x.id === mod.id && x.rect[0] === mod.rect[0]
        );
      if (t) {
        t.rect = mod.rect;
        this.hasUnsavedChanges = true;
      }
    },
    getNextId(type) {
      if (!type) return 1;
      const items = this.layoutData[String(this.pageIdx)] || [];
      const existingIds = items.filter((x) => x.type === type).map((x) => x.id);
      let nextId = 1;
      while (existingIds.includes(nextId)) nextId++;
      return nextId;
    },
    createNewId() {
      if (!this.currentTool) return;
      this.pendingId = this.getNextId(this.currentTool);
      this.pendingRole = "Body";
      this.updateEditorState();
    },
    setPendingRole(role) {
      this.pendingRole = role;
      this.updateEditorState();
    },
    updateEditorState() {
      if (this.editor)
        this.editor.setPendingState(this.pendingId, this.pendingRole);
    },

    // === 核心修复 3：ID 更新使用 UUID 定位 ===
    updateSelectedId(e) {
      if (!this.selectedItem) return;

      // 使用 e.target.value 获取输入值，不强制转 parseInt 以允许用户输入空值或临时字符
      // 但这里为了业务逻辑，最好还是转成数字。如果用户输入空，给个默认或者不更新
      const val = e.target.value;
      if (val === "") return; // 暂不处理空

      const newId = parseInt(val);
      if (isNaN(newId)) return;

      this.pushHistory();
      this.selectedItem.id = newId;

      if (this.editor)
        this.editor.updateObjectByUuid(this.selectedItem.uuid, newId);

      this.hasUnsavedChanges = true;
    },

    updateSelectedRole(newRole) {
      if (!this.selectedItem) return;
      this.pushHistory();
      this.selectedItem.role = newRole;
      this.editor.renderLayoutItems(this.layoutData[String(this.pageIdx)]);
      this.hasUnsavedChanges = true;
    },
    selectFromList(item) {
      if (this.editor) {
        // [修复] 之前传入 item.type, item.id，导致重复 ID 时无法选中正确的框
        // 现在传入 item.uuid (app.js 初始化时已保证 uuid 存在)
        this.editor.selectObjectByUuid(item.uuid);
      }
    },
    async saveLayout(showMsg) {
      if (!this.currentPaper) return;
      await ApiService.saveLayout(this.currentPaper.filename, this.layoutData);
      this.hasUnsavedChanges = false;
      if (showMsg) alert("Saved");
    },
    async prevPage() {
      if (this.pageIdx > 0) {
        this.pageIdx--;
        this.loadPageImage();
      }
    },
    async nextPage() {
      if (this.pageIdx < this.totalPages - 1) {
        this.pageIdx++;
        this.loadPageImage();
      }
    },

    // === 提取流程 ===
    async triggerExtract() {
      if (!confirm("确认开始提取全文内容？此过程可能需要几十秒。")) return;
      this.isBusy = true;
      this.busyMsg = "🔍 正在智能提取文档内容 (PDF -> JSON)...";

      try {
        await this.saveLayout();
        await ApiService.triggerExtract(this.currentPaper.filename);
        this.busyMsg = "📥 正在加载任务列表...";

        // 核心修复：处理后端返回的新格式（可能是数组，也可能是对象）
        const res = await ApiService.getExtractData(this.currentPaper.filename);

        if (Array.isArray(res)) {
          // 旧格式兼容
          this.translationTasks = res;
        } else {
          // 新格式：提取 tasks 字段
          this.translationTasks = res.tasks || [];
          this.currentRefMap = res.ref_map || "";
        }

        this.step = 2;
      } catch (e) {
        console.error(e);
        alert("提取失败: " + e.message);
      } finally {
        this.isBusy = false;
      }
    },
    // [新增] 显示 Prompt 预览
    showPromptPreview(e, task) {
      if (this.isTooltipLocked) return; // [新增] 如果锁定了，不要跟随鼠标移动
      const tooltip = document.getElementById("prompt-tooltip");
      const contentBox = tooltip.querySelector(".pt-content");

      if (!tooltip) return;

      let sys = this.promptTemplates[task.type] || "【System】Loading...";

      // [核心修改] 将占位符替换为真实的 Ref Map 数据
      // 如果 ref_map 内容太长，可以考虑截断，或者完整显示（根据你的需求）
      const mapDisplay = this.currentRefMap
        ? this.currentRefMap
        : "(本段落无特定资源引用)";
      sys = sys.replace("{ref_map_str}", mapDisplay);

      let fullText = "";
      if (task.user_hint && task.old_trans) {
        fullText = `=== 🔥 纠错模式 (Correction Mode) ===\n\n${sys}\n\n【User Input】\n原文:\n${task.src}\n\n旧译文:\n${task.old_trans}\n\n用户指引:\n${task.user_hint}`;
      } else {
        fullText = `${sys}\n\n【User Input】\n${task.src}`;
      }

      contentBox.innerText = fullText;

      // 2. 定位 (跟随鼠标但稍微偏移)
      tooltip.style.display = "block";

      // 防止溢出屏幕右侧/底部
      const x = e.clientX + 20;
      const y = e.clientY + 20;
      const viewW = window.innerWidth;
      const viewH = window.innerHeight;

      // 简单碰撞检测
      if (x + 600 > viewW) tooltip.style.left = viewW - 610 + "px";
      else tooltip.style.left = x + "px";

      if (y + 400 > viewH) tooltip.style.top = viewH - 410 + "px";
      else tooltip.style.top = y + "px";
    },
    // [修改] 切换锁定状态
    toggleTooltipLock(e) {
      this.isTooltipLocked = !this.isTooltipLocked;

      const tooltip = document.getElementById("prompt-tooltip");
      if (tooltip) {
        if (this.isTooltipLocked) {
          // 锁定：允许鼠标交互，改变边框颜色提示
          tooltip.style.pointerEvents = "auto";
          tooltip.style.borderColor = "#3498db"; // 变蓝提示已锁定
          tooltip.style.boxShadow = "0 0 15px rgba(52, 152, 219, 0.5)";
        } else {
          // 解锁：恢复穿透，恢复样式
          tooltip.style.pointerEvents = "none";
          tooltip.style.borderColor = "#444";
          tooltip.style.boxShadow = "0 8px 24px rgba(0,0,0,0.3)";
          this.hidePromptPreview(); // 立即隐藏
        }
      }
    },
    // [新增] 隐藏
    hidePromptPreview() {
      if (this.isTooltipLocked) return; // [新增] 如果锁定了，不要隐藏
      const tooltip = document.getElementById("prompt-tooltip");
      if (tooltip) tooltip.style.display = "none";
    },
    // === 翻译流程 ===
    async triggerTranslate() {
      // Logic A: 如果正在翻译 -> 点击即停止
      if (this.isTranslating) {
        if (!confirm("确定要终止后台翻译任务吗？")) return;

        try {
          // [修改] 调用后端 API 真正停止
          await ApiService.stopTranslation(this.currentPaper.filename);

          this.closeSSE(); // 断开前端监听
          this.isTranslating = false; // 更新 UI 状态

          alert("已发送停止信号，后台将在当前段落翻译完成后停止。");
        } catch (e) {
          alert("停止失败: " + e.message);
        }
        return;
      }

      // Logic B: 如果未翻译 -> 点击即开始
      if (
        this.translationTasks.length > 0 &&
        this.translationTasks.every((t) => t.status === "success")
      ) {
        this.step = 3;
        this.generateReport();
        return;
      }

      this.isTranslating = true;
      this.busyMsg = "🚀 翻译任务已启动...";

      try {
        await ApiService.triggerTranslate(this.currentPaper.filename);
        this.startSSE();
      } catch (e) {
        console.error(e);
        alert("启动失败: " + e.message);
        this.isTranslating = false;
      }
    },
    // [新增] 开启 SSE 连接
    startSSE() {
      this.closeSSE(); // 防止重复
      const url = `${API_BASE}/api/stream/translation/${this.currentPaper.filename}`;

      this.eventSource = new EventSource(url);

      // 监听数据推送
      this.eventSource.onmessage = (event) => {
        const tasks = JSON.parse(event.data);
        this.translationTasks = tasks; // 实时更新界面
      };

      // 监听结束信号 (我们在 server.py 里定义的 event: close)
      this.eventSource.addEventListener("close", (e) => {
        this.closeSSE();
        this.isTranslating = false;

        // 延迟跳转，提升体验
        setTimeout(async () => {
          if (confirm("翻译已完成！是否查看报告？")) {
            await this.generateReport();
            this.step = 3;
          }
        }, 500);
      });

      this.eventSource.onerror = (err) => {
        console.warn("SSE 连接断开或出错", err);
        // SSE 默认会自动重连，如果不需要自动重连可以手动 close
        // this.closeSSE();
      };
    },

    // [新增] 关闭连接
    closeSSE() {
      if (this.eventSource) {
        this.eventSource.close();
        this.eventSource = null;
      }
    },

    async generateReport() {
      const res = await ApiService.generateReport(this.currentPaper.filename);
      if (res.status === "success") {
        this.reportUrl = API_BASE + res.url + "?t=" + Date.now();
      }
    },
  },
  watch: {
    step(n) {
      if (n === 1 && this.currentPaper) {
        nextTick(() => {
          if (!this.editor) this.initEditor();
          else this.editor.resizeCanvasToContainer();
        });
      }
    },
  },
}).mount("#app");
