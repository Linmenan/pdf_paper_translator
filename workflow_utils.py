import os
import re
import time
import shutil
import platform
import fitz  # PyMuPDF
import json      # 新增
import hashlib   # 新增
import tkinter as tk
from tkinter import messagebox, font, ttk
from PIL import Image, ImageTk, ImageOps
from openai import OpenAI # 使用 OpenAI 兼容库

# --- 配置常量 ---
MAX_CHUNK_CHARS = 1000
TIMEOUT_MS = 600000
MAX_RETRIES = 3

# ==============================================================================
# 1. 定义多场景专用 Prompt
# ==============================================================================

# --- 场景 A: 元数据专用 (强格式约束) ---
SYSTEM_PROMPT_META = """
你是一个元数据解析器。请将输入的论文【标题】和【作者信息】翻译为中文。

**核心规则 (Strict Rules):**
1. **输入格式**: 
   - `[[META_TITLE: ...]]` -> 论文标题
   - `[[META_AUTHOR: ...]]` -> 作者/机构信息
2. **输出格式 (必须严格遵守 XML)**:
   - <meta_title>中文标题</meta_title>
   - <meta_author>作者不需翻译/机构翻译</meta_author>
3. **禁止**: 绝对不要输出原文，不要输出任何解释性文字，不要输出 markdown 代码块。
4. **人名处理**: 作者不需翻译；机构名请翻译。
"""

# --- 场景 B: 正文专用 (学术风格 + 引用处理) ---
SYSTEM_PROMPT_BODY = """
你是一个专业的学术论文翻译引擎。请将输入的学术段落翻译为中文。
**输入资源映射表 (Ref Map):**
{ref_map_str}

**核心规则:**
1. **风格**: 保持学术论文的严谨、客观、逻辑性。
2. **结构**: 
   - 当原文行首显式包含 `[[HEADER: ...]]` 标记，代表独立标题行时，使用 `<header>...</header>` 标签。
   - **禁止**将正文中的列表项（如 "1)", "3)" 等）随意升级为 `<header>`。
   - 正文段落 -> <p>译文</p> (也可以不加 p 标签，直接输出文本)。
3. **引用链接 (Link)**: 
   - 仅针对图表引用 (如 "Fig. 1", "Table 2", "Eq. 3", "Algorithm. 4") 使用 `[[LINK: ID|原文]]` 格式。
   - **严格禁止**对参考文献引用 (如 "[1]", "[22]", "[1-5]") 添加链接。参考文献引用必须原样保留，如 `[22]`。
4. **禁止**: 绝对不要输出 <src> 原文标签。只输出译文。
5. **保留**: 驼峰格式专有名词、缩写保留原文。
"""

# --- 场景 C: 资源说明专用 (图表/算法描述) ---
SYSTEM_PROMPT_ASSET = """
你是一个图表说明翻译助手。请翻译以下图表、算法或公式的标题与说明。

**输入资源映射表 (Ref Map):**
{ref_map_str}

**核心规则:**
1. **输入格式**: `[[ASSET_CAPTION: ID | Text...]]`
2. **输出格式**: <asset id="ID">中文译文</asset>
3. **处理**: 
   - 保持简洁，准确描述图表含义。
   - 遇到占位符 `[[ASSET_PLACEHOLDER:...]]`，请直接忽略或输出空标签。
"""

def sanitize_filename(filename: str) -> str:
    if not filename: return "untitled"
    name = os.path.splitext(os.path.basename(filename))[0]
    return re.sub(r'[\\/*?:"<>|]', '', name).replace('\n','').strip()

def is_box_in_rect(box, rect, threshold=0.5):
    bx0, by0, bx1, by1 = box
    rx0, ry0, rx1, ry1 = rect
    ix0 = max(bx0, rx0); iy0 = max(by0, ry0)
    ix1 = min(bx1, rx1); iy1 = min(by1, ry1)
    if ix1 > ix0 and iy1 > iy0:
        inter = (ix1 - ix0) * (iy1 - iy0)
        b_area = (bx1 - bx0) * (by1 - by0)
        if b_area > 0 and inter / b_area > threshold: return True
    return False

def get_optimal_font(root):
    system = platform.system()
    available = set(font.families(root))
    if system == "Windows": candidates = ["Microsoft YaHei UI", "SimHei"]
    elif system == "Darwin": candidates = ["PingFang SC", "Heiti SC"]
    else: candidates = ["Noto Sans CJK SC", "WenQuanYi Micro Hei"]
    for f in candidates:
        if f in available: return f
    return "Helvetica"

# --- 交互式编辑器 ---
class LayoutEditor:
    def __init__(self, doc, initial_data):
        self.doc = doc
        self.data = initial_data 
        self.page_count = len(doc)
        self.current_page = 0
        
        self.root = tk.Tk()
        self.ui_font = get_optimal_font(self.root)
        self.root.title(f"PDF 结构化校对 (新增: 5-标题 6-作者 7-遮罩)")
        
        if platform.system() == "Windows":
            self.root.state('zoomed')
        else:
            w = self.root.winfo_screenwidth()
            h = self.root.winfo_screenheight()
            self.root.geometry(f"{w}x{h}+0+0")

        self.current_tool_type = tk.StringVar(value="Figure") 
        self.current_id = tk.IntVar(value=1)
        self.current_role = tk.StringVar(value="Body") 
        
        # 颜色定义
        self.colors = {
            "Figure": "#e74c3c",    # 红
            "Table": "#3498db",     # 蓝
            "Equation": "#27ae60",  # 绿
            "Algorithm": "#9b59b6", # 紫
            "Title": "#d35400",     # 深橙 (标题)
            "Author": "#1abc9c",    # 青色 (作者)
            "Mask": "#7f8c8d",      # 灰色 (遮罩)
            "ContentArea": "#f1c40f" # 黄
        }

        self.main_paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.main_paned.pack(fill=tk.BOTH, expand=True)

        self.canvas_frame = tk.Frame(self.main_paned, bg="#555")
        self.sidebar_frame = tk.Frame(self.main_paned, bg="#f0f0f0", width=340)
        
        self.main_paned.add(self.canvas_frame, stretch="always")
        self.main_paned.add(self.sidebar_frame, stretch="never")

        self.setup_sidebar()

        self.v_scroll = tk.Scrollbar(self.canvas_frame, orient=tk.VERTICAL)
        self.h_scroll = tk.Scrollbar(self.canvas_frame, orient=tk.HORIZONTAL)
        self.canvas = tk.Canvas(self.canvas_frame, bg="#555",
                                yscrollcommand=self.v_scroll.set, xscrollcommand=self.h_scroll.set)
        self.v_scroll.config(command=self.canvas.yview)
        self.h_scroll.config(command=self.canvas.xview)
        
        self.v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.root.bind("<Key>", self.on_key_press)

        self.scale = 1.5 
        self.rect_id_map = {}
        self.start_x = None; self.start_y = None; self.current_rect_id = None
        
        self.update_id_suggestion()
        self.load_page()
        self.root.mainloop()

    def setup_sidebar(self):
        f_title = (self.ui_font, 14, "bold")
        f_norm = (self.ui_font, 12)
        f_bold = (self.ui_font, 12, "bold")
        
        p = 10
        tk.Label(self.sidebar_frame, text="工具箱 (Toolbox)", font=f_title, bg="#f0f0f0").pack(pady=(15, 10))
        
        # 1. Type
        type_frame = tk.LabelFrame(self.sidebar_frame, text="1. 元素类型", font=f_bold, bg="#f0f0f0")
        type_frame.pack(fill=tk.X, padx=p, pady=5)
        
        # 分组显示
        types_group1 = [
            ("图 (Figure) - [1]", "Figure"), 
            ("表 (Table) - [2]", "Table"), 
            ("式 (Equation) - [3]", "Equation"), 
            ("算 (Algorithm) - [4]", "Algorithm")
        ]
        types_group2 = [
            ("标题 (Title) - [5]", "Title"),
            ("作者 (Author) - [6]", "Author"),
            ("遮罩 (Mask) - [7]", "Mask")
        ]
        
        for text, val in types_group1:
            tk.Radiobutton(type_frame, text=text, variable=self.current_tool_type, value=val, 
                           command=self.update_id_suggestion, font=f_norm, bg="#f0f0f0", anchor="w").pack(fill=tk.X, padx=5)
        
        ttk.Separator(type_frame, orient='horizontal').pack(fill='x', padx=5, pady=5)
        
        for text, val in types_group2:
            tk.Radiobutton(type_frame, text=text, variable=self.current_tool_type, value=val, 
                           command=self.update_id_suggestion, font=f_norm, bg="#f0f0f0", anchor="w").pack(fill=tk.X, padx=5)
        
        ttk.Separator(type_frame, orient='horizontal').pack(fill='x', padx=5, pady=5)
        tk.Radiobutton(type_frame, text="正文范围 - [0]", variable=self.current_tool_type, value="ContentArea", 
                        command=self.update_id_suggestion, font=f_norm, bg="#f0f0f0", anchor="w").pack(fill=tk.X, padx=5)

        # 2. Props
        prop_frame = tk.LabelFrame(self.sidebar_frame, text="2. 属性设定", font=f_bold, bg="#f0f0f0")
        prop_frame.pack(fill=tk.X, padx=p, pady=5)
        
        row1 = tk.Frame(prop_frame, bg="#f0f0f0")
        row1.pack(fill=tk.X, padx=5, pady=5)
        tk.Label(row1, text="编号 (ID):", font=f_norm, bg="#f0f0f0").pack(side=tk.LEFT)
        tk.Button(row1, text="-", command=lambda: self.adj_id(-1), font=f_bold, width=3).pack(side=tk.LEFT, padx=5)
        self.id_entry = tk.Entry(row1, textvariable=self.current_id, width=5, font=f_norm, justify='center')
        self.id_entry.pack(side=tk.LEFT)
        tk.Button(row1, text="+", command=lambda: self.adj_id(1), font=f_bold, width=3).pack(side=tk.LEFT, padx=5)
        
        tk.Label(prop_frame, text="角色 (Role):", font=f_norm, bg="#f0f0f0").pack(anchor="w", padx=5, pady=(5,0))
        tk.Radiobutton(prop_frame, text="内容截图 (Body)", variable=self.current_role, value="Body", font=f_norm, bg="#f0f0f0").pack(anchor="w", padx=15)
        tk.Radiobutton(prop_frame, text="标题文本 (Caption)", variable=self.current_role, value="Caption", font=f_norm, bg="#f0f0f0").pack(anchor="w", padx=15)

        # 3. List
        list_frame = tk.LabelFrame(self.sidebar_frame, text="当前页列表 (Del删除)", font=f_bold, bg="#f0f0f0")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=p, pady=5)
        self.item_listbox = tk.Listbox(list_frame, bg="white", height=10, font=(self.ui_font, 11))
        self.item_listbox.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.item_listbox.bind("<Delete>", self.delete_selected_list_item)

        # 4. Nav
        nav_frame = tk.Frame(self.sidebar_frame, bg="#f0f0f0")
        nav_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=p, pady=20)
        tk.Button(nav_frame, text="< 上一页", command=self.prev_page, font=f_norm).pack(side=tk.LEFT)
        self.btn_next = tk.Button(nav_frame, text="下一页 >", command=self.next_page, font=f_bold, bg="#2ecc71", fg="white")
        self.btn_next.pack(side=tk.RIGHT)

    def adj_id(self, delta):
        val = self.current_id.get() + delta
        if val < 1: val = 1
        self.current_id.set(val)

    def set_tool(self, tool_type):
        self.current_tool_type.set(tool_type)
        self.update_id_suggestion()

    def update_id_suggestion(self):
        ctype = self.current_tool_type.get()
        if ctype in ["ContentArea", "Mask", "Title", "Author"]: return
        
        max_id = 0
        for p_idx in self.data:
            for item in self.data[p_idx]:
                if item['type'] == ctype:
                    max_id = max(max_id, item.get('id', 0))
        self.current_id.set(max_id + 1)
        self.current_role.set("Body")

    def load_page(self):
        self.canvas.delete("all")
        self.rect_id_map = {}
        self.item_listbox.delete(0, tk.END)
        
        page = self.doc[self.current_page]
        pix = page.get_pixmap(matrix=fitz.Matrix(self.scale, self.scale))
        self.tk_img = ImageTk.PhotoImage(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
        
        self.canvas.config(scrollregion=(0, 0, pix.width, pix.height))
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_img)
        
        self.btn_next.config(text="生成结果 (Finish)" if self.current_page == self.page_count - 1 else "下一页 >")
        
        if self.current_page in self.data:
            for idx, item in enumerate(self.data[self.current_page]):
                self.draw_box(item, idx)
                desc = f"[{item['type']}]"
                if item['type'] not in ["ContentArea", "Mask", "Title", "Author"]:
                    desc += f" {item['id']} - {item['role']}"
                self.item_listbox.insert(tk.END, desc)

    def draw_box(self, item, idx):
        r = item['rect']
        x0, y0, x1, y1 = r.x0*self.scale, r.y0*self.scale, r.x1*self.scale, r.y1*self.scale
        color = self.colors.get(item['type'], "black")
        
        dash = (4, 4) if item.get('role') == 'Caption' else None
        width = 3 if item['type'] == 'ContentArea' else 2
        stipple = 'gray50' if item['type'] == 'Mask' else '' # 遮罩加阴影
        
        rect_id = self.canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=width, dash=dash, stipple=stipple, tags="box")
        
        label_txt = item['type']
        if item['type'] not in ['ContentArea', 'Mask', 'Title', 'Author']:
            label_txt += f" {item['id']} ({item['role'][0]})"
        
        bg_id = self.canvas.create_rectangle(x0, y0-20, x0+len(label_txt)*9, y0, fill=color, outline=color, tags="box")
        txt_id = self.canvas.create_text(x0+2, y0-10, text=label_txt, anchor=tk.W, fill="white", font=("Arial", 10, "bold"), tags="box")
        
        self.rect_id_map[rect_id] = idx
        self.rect_id_map[bg_id] = idx
        self.rect_id_map[txt_id] = idx

    def on_mouse_down(self, event):
        self.start_x = self.canvas.canvasx(event.x)
        self.start_y = self.canvas.canvasy(event.y)
        color = self.colors.get(self.current_tool_type.get(), "black")
        self.current_rect_id = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline=color, width=2, dash=(2,2))

    def on_mouse_drag(self, event):
        self.canvas.coords(self.current_rect_id, self.start_x, self.start_y, self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))

    def on_mouse_up(self, event):
        x0, x1 = sorted([self.start_x, self.canvas.canvasx(event.x)])
        y0, y1 = sorted([self.start_y, self.canvas.canvasy(event.y)])
        self.canvas.delete(self.current_rect_id)
        
        if x1 - x0 < 10 or y1 - y0 < 10: return
        
        pdf_rect = fitz.Rect(x0/self.scale, y0/self.scale, x1/self.scale, y1/self.scale)
        
        new_item = {
            'rect': pdf_rect,
            'type': self.current_tool_type.get(),
            'id': self.current_id.get(),
            'role': self.current_role.get()
        }
        
        if self.current_page not in self.data: self.data[self.current_page] = []
        if new_item['type'] == 'ContentArea':
             self.data[self.current_page] = [x for x in self.data[self.current_page] if x['type'] != 'ContentArea']

        self.data[self.current_page].append(new_item)
        
        # 自动切换 role 逻辑
        no_caption_types = ['ContentArea', 'Equation', 'Mask', 'Title', 'Author']
        if new_item['type'] not in no_caption_types and new_item['role'] == 'Body':
            self.current_role.set("Caption")
        
        self.load_page()

    def on_right_click(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        items = self.canvas.find_overlapping(x-2, y-2, x+2, y+2)
        for item_id in items:
            if item_id in self.rect_id_map:
                idx = self.rect_id_map[item_id]
                del self.data[self.current_page][idx]
                self.load_page()
                break

    def delete_selected_list_item(self, event):
        sel = self.item_listbox.curselection()
        if sel:
            idx = sel[0]
            del self.data[self.current_page][idx]
            self.load_page()

    def on_key_press(self, event):
        k = event.keysym
        if k == '1': self.set_tool("Figure")
        elif k == '2': self.set_tool("Table")
        elif k == '3': self.set_tool("Equation")
        elif k == '4': self.set_tool("Algorithm") 
        elif k == '5': self.set_tool("Title")   # 新增
        elif k == '6': self.set_tool("Author")  # 新增
        elif k == '7': self.set_tool("Mask")    # 新增
        elif k == '0': self.set_tool("ContentArea")
        elif k in ['space', 'Return', 'Right']: self.next_page()
        elif k in ['BackSpace', 'Left']: self.prev_page()

    def next_page(self):
        # 1. 获取当前页的正文区域 (ContentArea)
        curr_content = next((x for x in self.data.get(self.current_page, []) if x['type'] == 'ContentArea'), None)
        next_idx = self.current_page + 1
        
        if next_idx < self.page_count:
            # 确保下一页的数据列表已初始化
            if next_idx not in self.data: 
                self.data[next_idx] = []
            
            # --- 【核心修复】智能继承逻辑 ---
            # 检查下一页是否已经有了 ContentArea (例如从历史记录加载的)
            next_has_content = any(x['type'] == 'ContentArea' for x in self.data[next_idx])
            
            # 只有当下一页【没有】正文区域时，才尝试继承当前页的
            if not next_has_content and curr_content:
                # 额外的智能检查：只有当页面尺寸一致时才继承，防止横页/竖页切换导致框跑飞
                if self.doc[self.current_page].rect == self.doc[next_idx].rect:
                     # 复制一份当前页的框过去
                     self.data[next_idx].insert(0, curr_content.copy())
            
            # 翻页
            self.current_page += 1
            self.load_page()
        else:
            if messagebox.askyesno("完成", "确认完成所有校对？"):
                self.root.destroy()

    def prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.load_page()

def split_long_buffer_safely(text, max_len):
    """
    将超长文本拆分为多个片段，确保拆分点在句子结束处。
    修复：使用负向后瞻 (Negative Look-behind) 保护常见缩写 (Fig., Eq., al. 等) 不被切断。
    """
    if len(text) <= max_len:
        return [text]
    
    # --- 核心修改：保护缩写词的正则 ---
    # 含义：
    # 1. (?<!...) : 负向后瞻，如果句号前面是这些词，则不匹配
    # 2. \b       : 单词边界，防止匹配到非单词结尾
    # 3. (?<=[.?!;]) : 正向后瞻，必须以标点结尾
    # 4. \s+      : 中间有空格
    # 5. (?=[A-Z0-9]) : 后面接大写字母或数字
    
    protect_pattern = (
        r'(?<!\bFig\.)(?<!\bFigs\.)'  # Fig. / Figs.
        r'(?<!\bEq\.)(?<!\bEqs\.)'    # Eq. / Eqs.
        r'(?<!\bTab\.)(?<!\bTabs\.)'  # Tab. / Tabs.
        r'(?<!\bRef\.)(?<!\bRefs\.)'  # Ref. / Refs.
        r'(?<!\bVol\.)(?<!\bno\.)'    # Vol. / no.
        r'(?<!\bal\.)(?<!\bvs\.)'     # et al. / vs.
        r'(?<!\bi\.e\.)(?<!\be\.g\.)' # i.e. / e.g.
    )
    
    # 2. 切分逻辑：
    # (?<=[.?!;]) : 必须以标点结尾
    # \s+         : 分隔符是空格
    # (?=[A-Z0-9\[]) : 后面接大写/数字/方括号(引用)
    split_marker = r'(?<=[.?!;])\s+(?=[A-Z0-9\[])'
    
    final_pattern = protect_pattern + split_marker

    try:
        # 使用 IGNORECASE 以防 fig. 1
        sentences = re.split(final_pattern, text, flags=re.IGNORECASE)
    except re.error:
        # 如果环境不支持复杂 lookbehind，回退到简单切分
        print("⚠️ [Warning] Regex lookbehind failed, using simple split.")
        sentences = re.split(r'(?<=[.?!;])\s+(?=[A-Z0-9])', text)
    
    chunks = []
    current_chunk = ""
    
    for sentence in sentences:
        # 如果当前块加上新句子会超长，且当前块不为空 -> 封包
        if len(current_chunk) + len(sentence) > max_len and current_chunk:
            chunks.append(current_chunk)
            current_chunk = sentence
        else:
            # 拼接
            if current_chunk:
                current_chunk += " " + sentence
            else:
                current_chunk = sentence
                
    if current_chunk:
        chunks.append(current_chunk)
        
    return chunks

# --- 辅助函数：段落流处理 (含长难句拆分) ---
def smart_merge_paragraphs(blocks, max_split_len=500):
    """
    blocks: 原始文本块流
    调试增强版：使用 repr() 显示不可见字符，强制打印 Fig 附近的匹配情况
    """
    if not blocks: return []
    merged = []
    buffer = ""
    
    terminals = ('.', '?', '!', ':', ';', '。', '？', '！', '：', '；')
    hard_boundary_pattern = re.compile(r'^\[\[(HEADER|ASSET_|META_).*?\]\]')
    
    # --- 调试用：放宽正则，先抓到再说 ---
    # 移除 (?:^|\s) 限制，直接匹配结尾的关键词
    hanging_abbrev_pattern = re.compile(
        r'(Fig|Figure|Eq|Equation|Tab|Table|Ref|Reference|Sec|Section)\.?\s*$', 
        re.IGNORECASE
    )

    print(f"🔍 [DEBUG] 开始处理 {len(blocks)} 个文本块...")

    for i, block in enumerate(blocks):
        block = block.strip()
        if not block: continue
        
        # 1. 硬性边界 -> 强制刷新
        if hard_boundary_pattern.match(block):
            if buffer:
                merged.extend(split_long_buffer_safely(buffer, max_split_len))
                buffer = ""
            merged.append(block)
            continue
        
        # 2. 初始化
        if not buffer:
            buffer = block
            continue
            
        # 3. 合并逻辑
        prev_end_char = buffer[-1] if buffer else ""
        
        # --- 🕵️‍♂️ 显微镜调试区 ---
        # 取 buffer 最后 20 个字符
        tail = buffer[-20:]
        # 如果结尾看起来像是 Fig，打印出来看看究竟是什么
        if "Fig" in tail or "Tab" in tail:
            is_match = hanging_abbrev_pattern.search(buffer) is not None
            print(f"🧐 [Chunk {i}] 发现疑似缩写:")
            print(f"   Buffer尾部(repr): {repr(tail)}") # <--- 重点看这里！
            print(f"   正则匹配结果: {is_match}")
            if not is_match:
                print(f"   ⚠️ 警告：虽然包含关键字，但正则未匹配！")

        # 情况 A: 连字符
        if prev_end_char == '-':
            buffer = buffer[:-1] + block
            
        # --- 情况 B: 悬挂缩写修复 ---
        elif hanging_abbrev_pattern.search(buffer):
            # print(f"🔗 [MERGE] 成功合并跨行缩写: ...{buffer[-10:]} + {block[:10]}...")
            buffer = buffer + " " + block
            
        # 情况 C: 句子未结束
        elif (not buffer.endswith(terminals)) or (block[0].islower()):
            buffer = buffer + " " + block
            
        # 情况 D: 正常分段
        else:
            # 调试：如果刚才 Fig 没匹配上，这里就会执行切分
            if "Fig" in tail:
                print(f"✂️ [SPLIT] 执行切分 (因为正则未匹配): ...{repr(tail)} || {repr(block[:10])}...")
            
            merged.extend(split_long_buffer_safely(buffer, max_split_len))
            buffer = block 

    if buffer:
        merged.extend(split_long_buffer_safely(buffer, max_split_len))
    
    return merged

# --- 核心提取逻辑 ---
def extract_text_and_save_assets_smart(pdf_path: str, raw_text_dir: str, vis_output_root: str) -> tuple[str, str, str, int]:
    if not os.path.exists(pdf_path): raise FileNotFoundError(f"PDF missing: {pdf_path}")
    
    clean_name = sanitize_filename(pdf_path)
    os.makedirs(raw_text_dir, exist_ok=True)
    txt_path = os.path.join(raw_text_dir, f"{clean_name}_context.txt")
    
    # 资源目录 (extracted_output/{PaperName}/assets)
    extracted_assets_dir = os.path.join(raw_text_dir, clean_name, "assets")
    
    # 标注配置文件路径
    layout_config_path = os.path.join(raw_text_dir, clean_name, "layout_config.json")
    
    if not os.path.exists(os.path.dirname(layout_config_path)):
        os.makedirs(os.path.dirname(layout_config_path), exist_ok=True)

    doc = fitz.open(pdf_path)
    
    # =========================================================
    # 1. 初始化数据 (核心修复：逐页合并历史与默认值)
    # =========================================================
    init_data = {}
    saved_json = {}

    # 尝试读取历史文件
    if os.path.exists(layout_config_path):
        print(f"📂 检测到历史标注记录: {layout_config_path}，正在加载...")
        try:
            with open(layout_config_path, 'r', encoding='utf-8') as f:
                saved_json = json.load(f)
        except Exception as e:
            print(f"⚠️ 加载历史记录失败 ({e})，将忽略历史文件。")
            saved_json = {}

    # 遍历每一页进行初始化
    for i, page in enumerate(doc):
        w, h = page.rect.width, page.rect.height
        page_items = []
        
        # A. 尝试获取该页的历史数据
        # JSON 的 key 是字符串类型的数字 "0", "1"...
        if str(i) in saved_json:
            raw_items = saved_json[str(i)]
            for item in raw_items:
                # 恢复 fitz.Rect 对象
                r = item['rect'] # [x0, y0, x1, y1]
                page_items.append({
                    'rect': fitz.Rect(r[0], r[1], r[2], r[3]),
                    'type': item['type'],
                    'id': item['id'],
                    'role': item['role']
                })
        
        # B. 检查并补全 ContentArea (正文范围)
        # 如果历史记录里没有这一页，或者这一页被删除了正文范围，必须补一个默认的
        has_content_area = any(x['type'] == 'ContentArea' for x in page_items)
        
        if not has_content_area:
            # 默认正文范围：页眉留 8% 空白
            default_rect = fitz.Rect(0, h*0.08, w, h*0.92)
            # 插入到列表头部，确保层级在最底层（虽然逻辑上不影响，但看着舒服）
            page_items.insert(0, {
                'rect': default_rect,
                'type': 'ContentArea',
                'id': 0,      # ID 对 ContentArea 无意义，给 0
                'role': 'Body'
            })
            
        init_data[i] = page_items

    # =========================================================
    # 2. 启动交互编辑器
    # =========================================================
    editor = LayoutEditor(doc, init_data)
    verified_data = editor.data

    # =========================================================
    # 3. 保存标注结果 (序列化)
    # =========================================================
    serializable_data = {}
    for page_idx, items in verified_data.items():
        serializable_data[page_idx] = []
        for item in items:
            r = item['rect']
            serializable_data[page_idx].append({
                'rect': [r.x0, r.y0, r.x1, r.y1],
                'type': item['type'],
                'id': item['id'],
                'role': item['role']
            })
            
    with open(layout_config_path, 'w', encoding='utf-8') as f:
        json.dump(serializable_data, f, indent=2)
    print(f"💾 标注进度已保存至: {layout_config_path}")

    # =========================================================
    # 4. 后续处理 (资源提取 & 文本生成)
    # =========================================================
    if os.path.exists(extracted_assets_dir): shutil.rmtree(extracted_assets_dir)
    os.makedirs(extracted_assets_dir, exist_ok=True)

    print(f"🧩 正在处理资源 (保存至: {extracted_assets_dir})...")
    assets_agg = {}
    meta_info_blocks = [] 
    
    for p_idx in range(len(doc)):
        page = doc[p_idx]
        items = verified_data.get(p_idx, [])
        
        for item in items:
            if item['type'] == 'Title':
                txt = page.get_text("text", clip=item['rect']).strip().replace('\n', ' ')
                meta_info_blocks.append(f"[[META_TITLE: {txt}]]")
                continue
            if item['type'] == 'Author':
                txt = page.get_text("text", clip=item['rect']).strip().replace('\n', ' ')
                meta_info_blocks.append(f"[[META_AUTHOR: {txt}]]")
                continue
            
            if item['type'] in ['ContentArea', 'Mask']: continue
            
            key = f"{item['type']}_{item['id']}" 
            if key not in assets_agg: assets_agg[key] = {'bodies': [], 'captions': [], 'rects': [], 'page': p_idx}
            assets_agg[key]['rects'].append(item['rect']) 
            
            if item['role'] == 'Body':
                pix = page.get_pixmap(clip=item['rect'], matrix=fitz.Matrix(3,3))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                assets_agg[key]['bodies'].append(img)
            elif item['role'] == 'Caption':
                text = page.get_text("text", clip=item['rect']).strip().replace('\n', ' ')
                assets_agg[key]['captions'].append(text)

    ref_map = [] 
    asset_count = 0
    final_asset_captions = {} 

    for key, data in assets_agg.items():
        if data['bodies']:
            widths, heights = zip(*(i.size for i in data['bodies']))
            total_h = sum(heights)
            max_w = max(widths)
            merged_img = Image.new('RGB', (max_w, total_h), (255, 255, 255))
            y_off = 0
            for img in data['bodies']:
                merged_img.paste(img, (0, y_off))
                y_off += img.height
            
            merged_img.save(os.path.join(extracted_assets_dir, f"{key}.png"))
            asset_count += 1
        
        full_caption = " ".join(data['captions'])
        final_asset_captions[key] = full_caption
        
        type_str, id_str = key.split('_')
        ref_map.append(f"{type_str} {id_str} -> {key}")
        if type_str == "Figure": ref_map.append(f"Fig. {id_str} -> {key}")
        elif type_str == "Table": ref_map.append(f"Tab. {id_str} -> {key}")

    ref_map_str = "\n".join(ref_map)

    print("📝 提取正文文本...")
    raw_paragraph_stream = [] 
    raw_paragraph_stream.extend(meta_info_blocks)
    
    header_pattern = re.compile(r'^(\d+(\.\d+)*\.?|[IVX]+\.|[A-Z]\.)\s+|^(Abstract|References|Introduction|Conclusion|Method)', re.IGNORECASE)

    for p_idx, page in enumerate(doc):
        page_asset_inserts = []
        page_items = verified_data.get(p_idx, [])
        ignore_rects = []
        content_rect = page.rect # 默认全页，会被下面的 ContentArea 覆盖

        for item in page_items:
            if item['type'] == 'ContentArea': 
                content_rect = item['rect']
            elif item['type'] in ['Mask', 'Title', 'Author']: 
                ignore_rects.append(item['rect'])
            else:
                ignore_rects.append(item['rect'])
                key = f"{item['type']}_{item['id']}"
                page_asset_inserts.append({
                    "rect": item['rect'],
                    "text": f"[[ASSET_INSERT: {key}]]",
                    "id": key
                })

        unique_inserts = {}
        for ins in page_asset_inserts:
            k = ins['id']
            if k not in unique_inserts or ins['rect'].y0 < unique_inserts[k]['rect'].y0:
                unique_inserts[k] = ins
        sorted_inserts = sorted(unique_inserts.values(), key=lambda x: x['rect'].y0)

        raw_blocks = page.get_text("blocks", clip=content_rect)
        mixed_blocks = []
        mid_x = (content_rect.x0 + content_rect.x1) / 2
        left_col, right_col = [], []
        for b in raw_blocks:
            if (b[0] + b[2]) / 2 < mid_x: left_col.append(b)
            else: right_col.append(b)
        left_col.sort(key=lambda b: (b[1], b[0]))
        right_col.sort(key=lambda b: (b[1], b[0]))
        sorted_text_blocks = left_col + right_col

        for b in sorted_text_blocks:
            bbox = fitz.Rect(b[:4])
            text = b[4].strip()
            is_masked = False
            for ir in ignore_rects:
                if is_box_in_rect(bbox, ir, 0.6): 
                    is_masked = True; break
            
            if not is_masked and text:
                mixed_blocks.append({
                    "type": "text",
                    "y_sort": bbox.y0 + (0 if bbox.x0 < mid_x else 10000),
                    "text": text
                })

        for ins in sorted_inserts:
            bbox = ins['rect']
            mixed_blocks.append({
                "type": "asset_tag",
                "y_sort": bbox.y0 + (0 if bbox.x0 < mid_x else 10000),
                "text": ins['text']
            })
            
        mixed_blocks.sort(key=lambda x: x['y_sort'])

        for b in mixed_blocks:
            text = b['text']
            if b['type'] == "text":
                text = re.sub(r'-\n', '', text)
                text = text.replace('\n', ' ')
                lines = text.split('\n')
                first_line = lines[0].strip()
                if header_pattern.match(first_line) and len(first_line) < 80:
                    raw_paragraph_stream.append(f"[[HEADER: {first_line}]]")
                    if len(lines) > 1: raw_paragraph_stream.append(" ".join(lines[1:]))
                else:
                    raw_paragraph_stream.append(text)
            else:
                raw_paragraph_stream.append(text)

    merged_text_blocks = smart_merge_paragraphs(raw_paragraph_stream)

    assets_xml_snippets = []
    sorted_keys = sorted(assets_agg.keys(), key=lambda k: (k.split('_')[0], int(k.split('_')[1])))
    
    assets_xml_snippets.append("\n\n--- ASSETS METADATA ---\n")
    for key in sorted_keys:
        cap = final_asset_captions[key]
        if cap:
            assets_xml_snippets.append(f"[[ASSET_CAPTION: {key} | {cap}]]")
        else:
            assets_xml_snippets.append(f"[[ASSET_PLACEHOLDER: {key}]]")
    
    final_content = "\n\n".join(merged_text_blocks) + "\n\n" + "\n".join(assets_xml_snippets)
    
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"[[REF_MAP_START]]\n{ref_map_str}\n[[REF_MAP_END]]\n\n")
        f.write(final_content)

    vis_final_dir = os.path.join(vis_output_root, clean_name)
    return final_content, txt_path, vis_final_dir, asset_count

# --- 辅助函数：计算文本指纹 ---
def compute_hash(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()

# --- 辅助函数：智能分离（修复 Assets 被误吞的问题）---
# --- 辅助函数：智能分离 (四段式切分：Meta / Body / Assets / Refs) ---
def split_content_smart(text):
    """
    将文本切分为四部分，确保每一部分都能被独立处理：
    1. 元数据 (Meta) -> 必须单独翻译，防止被正文吞没
    2. 资源说明 (Assets) -> 必须翻译
    3. 参考文献 (Refs) -> 跳过
    4. 正文 (Body) -> 切分翻译
    """
    # --- 1. 剥离 ASSETS METADATA (从尾部找) ---
    asset_marker = "--- ASSETS METADATA ---"
    assets_part = ""
    content_remaining = text
    
    if asset_marker in text:
        parts = text.rsplit(asset_marker, 1)
        if len(parts) == 2:
            content_remaining = parts[0]
            assets_part = asset_marker + parts[1] # 保留标记头

    # --- 2. 剥离 References (在剩余中找) ---
    # 兼容 [[HEADER: References]] 或 [[HEADER: REFERENCE]] 等
    ref_pattern = re.compile(r'(\[\[HEADER:\s*References?.*?\]\])', re.IGNORECASE)
    split_parts = ref_pattern.split(content_remaining, maxsplit=1)
    
    body_with_meta = ""
    ref_part = ""
    
    if len(split_parts) >= 3:
        # split_parts[0]: 正文
        # split_parts[1]: 标题 ([[HEADER: References]])
        # split_parts[2]: 参考文献列表内容
        body_with_meta = split_parts[0].strip()
        ref_part = split_parts[1] + split_parts[2]
    else:
        body_with_meta = content_remaining.strip()

    # --- 3. 剥离 Meta Data (从头部找) ---
    # 匹配连续的 [[META_...]] 块
    meta_pattern = re.compile(r'(\[\[META_.*?:.*?\]\]\s*)+')
    meta_match = meta_pattern.match(body_with_meta)
    
    meta_part = ""
    body_part = body_with_meta
    
    if meta_match:
        meta_part = meta_match.group(0).strip()
        # 从匹配结束的位置开始截取正文
        body_part = body_with_meta[meta_match.end():].strip()

    # 返回 4 个部分
    return meta_part, body_part, assets_part, ref_part

def split_text_into_chunks_with_layout(text, max_chars):
    """
    切分文本，同时提取 [[ASSET_INSERT]] 标记。
    返回: (chunks, layout_map)
    layout_map = { chunk_index: [asset_id1, asset_id2] }
    """
    header_pattern = re.compile(r'(\[\[HEADER:.*?\]\])', re.IGNORECASE)
    # 正则用于提取并移除 INSERT 标记
    insert_pattern = re.compile(r'\[\[ASSET_INSERT:\s*(.*?)\]\]')
    
    segments = header_pattern.split(text)
    final_chunks = []
    layout_map = {}
    
    current_chunk_idx = 0
    
    for seg in segments:
        seg = seg.strip()
        if not seg: continue
        
        # 1. 检查是否有 Insert 标记
        found_inserts = insert_pattern.findall(seg)
        # 移除标记，净化文本
        clean_seg = insert_pattern.sub('', seg).strip()
        
        if not clean_seg and not found_inserts: continue # 只有标记且被移除后为空，跳过? 不，标记位置很重要
        
        # 2. 如果是 Header -> 独立成块
        if header_pattern.match(seg): # 注意：这里匹配的是原始 seg，所以 Header 里不应该有 Insert 标记，假如有也要处理
             # Header 还是原样保留，假设 Header 里没有 Insert
             final_chunks.append(clean_seg)
             if found_inserts:
                 if current_chunk_idx not in layout_map: layout_map[current_chunk_idx] = []
                 layout_map[current_chunk_idx].extend(found_inserts)
             current_chunk_idx += 1
             
        # 3. 正文 -> 按长度切分
        else:
            paragraphs = clean_seg.split('\n\n')
            buffer = []
            buffer_len = 0
            
            # 如果这一段全是 Insert 标记，文本为空
            if not clean_seg and found_inserts:
                # 把它挂在当前即将在生成的 chunk (或者上一个)
                # 为了简化，我们挂在 "下一个即将生成的 chunk" 索引上
                if current_chunk_idx not in layout_map: layout_map[current_chunk_idx] = []
                layout_map[current_chunk_idx].extend(found_inserts)
                continue

            for p in paragraphs:
                p = p.strip()
                if not p: continue
                
                if buffer_len + len(p) > max_chars and buffer:
                    final_chunks.append("\n\n".join(buffer))
                    # 注意：如果刚才的 inserts 是在这个段落里的，逻辑上很难精确到“段落级”。
                    # 我们目前的粒度是 Chunk 级。
                    # 简单策略：如果这个大段里有 insert，我们统一挂在第一个 chunk 上，
                    # 或者挂在当前 chunk。
                    # 改进策略：found_inserts 是属于整个 seg 的。我们把它挂在这个 seg 生成的 *第一个* chunk 上。
                    if found_inserts:
                         if current_chunk_idx not in layout_map: layout_map[current_chunk_idx] = []
                         layout_map[current_chunk_idx].extend(found_inserts)
                         found_inserts = [] # 只要挂载一次
                    
                    current_chunk_idx += 1
                    buffer = []
                    buffer_len = 0
                
                buffer.append(p)
                buffer_len += len(p)
            
            if buffer:
                final_chunks.append("\n\n".join(buffer))
                if found_inserts: # 处理剩余的 (或者该段只有一个 chunk 的情况)
                     if current_chunk_idx not in layout_map: layout_map[current_chunk_idx] = []
                     layout_map[current_chunk_idx].extend(found_inserts)
                current_chunk_idx += 1
                
    return final_chunks, layout_map

# --- 核心 LLM 调用函数 (应用新的切分逻辑) ---
def run_smart_analysis(full_text_path_or_content: str, output_path: str, cache_path: str = None):
    # 【模式配置】
    API_KEY = "ollama" 
    BASE_URL = "http://localhost:11434/v1"
    MODEL_NAME = "qwen2.5:7b"
    
    from openai import OpenAI

    if os.path.isfile(full_text_path_or_content):
         with open(full_text_path_or_content, 'r', encoding='utf-8') as f: content = f.read()
    else:
        content = full_text_path_or_content

    ref_map_str = ""
    body_text = content
    map_match = re.search(r'\[\[REF_MAP_START\]\]\n(.*?)\n\[\[REF_MAP_END\]\]', content, re.DOTALL)
    if map_match:
        ref_map_str = map_match.group(1)
        body_text = content.replace(map_match.group(0), "").strip()
    
    meta_text, body_text, assets_text, raw_refs_text = split_content_smart(body_text)
    
    raw_chunks = []
    layout_map_global = {} 
    
    if meta_text: raw_chunks.append({"text": meta_text, "type": "meta"})
        
    if body_text:
        body_parts, local_layout_map = split_text_into_chunks_with_layout(body_text, MAX_CHUNK_CHARS)
        offset = len(raw_chunks) 
        for idx, part in enumerate(body_parts):
            raw_chunks.append({"text": part, "type": "body"})
            if idx in local_layout_map:
                layout_map_global[idx + offset] = local_layout_map[idx]
            
    if assets_text: raw_chunks.append({"text": assets_text, "type": "asset"})

    # 阶段一
    print(f"📋 [阶段一] 编排任务: 总片段 {len(raw_chunks)} 个")
    
    old_tasks_map = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                old_json = json.load(f)
                for t in old_json.get("tasks", []):
                    old_tasks_map[t["chunk_hash"]] = t
        except: pass

    current_tasks = []
    for i, item in enumerate(raw_chunks):
        c_text = item["text"]
        c_type = item["type"]
        h = compute_hash(c_text)
        cached_task = old_tasks_map.get(h)
        if cached_task:
            task_entry = cached_task
            task_entry["id"] = i
            if "type" not in task_entry: task_entry["type"] = c_type
            if task_entry["status"] == "failed":
                task_entry["status"] = "pending"
                print(f"   🔹 Part {i+1}: 之前失败，已重置为 pending")
        else:
            task_entry = { "id": i, "type": c_type, "chunk_hash": h, "status": "pending", "src": c_text, "trans": "" }
        current_tasks.append(task_entry)

    # 阶段一点五：人工审查
    suspicious_tasks = [t for t in current_tasks if t.get("status") == "suspicious"]
    
    if suspicious_tasks:
        print(f"\n⚠️ 检测到 {len(suspicious_tasks)} 个 'suspicious' 任务，请审核：")
        for st in suspicious_tasks:
            print("=" * 60)
            print(f"【ID: {st['id']} | Type: {st['type']}】")
            # --- 【修改点】完整显示，不再截断 ---
            print("🔻 原文:")
            print(st['src']) 
            print("-" * 30)
            print("🔻 译文:")
            print(st['trans']) 
            print("=" * 60)
            
            while True:
                user_choice = input("👉 操作? (y=通过 / n=重译 / s=跳过): ").strip().lower()
                if user_choice == 'y':
                    st['status'] = 'success'
                    print("   ✅ Marked as Success")
                    break
                elif user_choice == 'n':
                    st['status'] = 'pending'
                    st['trans'] = ""
                    print("   🔄 Marked as Pending")
                    break
                elif user_choice == 's':
                    print("   ⏭️ Skipped")
                    break
        
        _save_cache(cache_path, MODEL_NAME, current_tasks, raw_refs_text, layout_map_global)

    # 阶段二
    pending_tasks = [t for t in current_tasks if t["status"] == "pending"]
    if pending_tasks:
        print(f"\n🚀 [阶段二] 开始推理 (剩余 {len(pending_tasks)} 个)...")
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        PROMPT_MAP = {
            "meta": SYSTEM_PROMPT_META,
            "body": SYSTEM_PROMPT_BODY.replace("{ref_map_str}", ref_map_str),
            "asset": SYSTEM_PROMPT_ASSET.replace("{ref_map_str}", ref_map_str)
        }
        for task in current_tasks:
            if task["status"] != "pending": continue
            print(f"   ⚡ Part {task['id']+1}/{len(current_tasks)} [{task['type'].upper()}] ...", end="", flush=True)
            
            messages = [
                {"role": "system", "content": PROMPT_MAP.get(task['type'], PROMPT_MAP["body"])},
                {"role": "user", "content": task["src"]}
            ]
            
            success = False
            for attempt in range(3):
                try:
                    response = client.chat.completions.create(model=MODEL_NAME, messages=messages, temperature=0.1)
                    res_text = response.choices[0].message.content
                    res_text = re.sub(r'^```xml\s*', '', res_text)
                    res_text = re.sub(r'```$', '', res_text)
                    if res_text:
                        task["trans"] = res_text.strip()
                        task["status"] = "success"
                        print(" ✅")
                        success = True
                        break
                except Exception as e:
                    print(f" ⚠️ {e}")
                    time.sleep(2)
            if not success:
                task["status"] = "failed"
                print(" ❌")
            if cache_path: _save_cache(cache_path, MODEL_NAME, current_tasks, raw_refs_text, layout_map_global)
    else:
        print("\n🎉 无需新增推理。")

    # 阶段三
    print("💾 [阶段三] 刷新结果文件...")
    final_body = "\n".join([t["trans"] for t in current_tasks if t["status"] == "success"])
    final_refs = ""
    if raw_refs_text:
        final_refs = f"\n<header_block><src>References</src><trans>参考文献</trans></header_block>\n"
        clean_ref_content = re.sub(r'\[\[HEADER:.*?\]\]', '', raw_refs_text).strip()
        final_refs += f"<ref_block><src>{clean_ref_content}</src></ref_block>"

    with open(output_path, 'w', encoding='utf-8') as f: 
        f.write(final_body + "\n" + final_refs)
    return output_path

def _save_cache(path, model, tasks, refs, layout):
    if not path: return
    structure = { "model": model, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "tasks": tasks, "raw_references": refs, "layout_map": layout }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(structure, f, ensure_ascii=False, indent=2)

# 辅助函数：保存 Cache，避免代码重复
def _save_cache(path, model, tasks, refs, layout):
    if not path: return
    structure = {
        "model": model,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": tasks,
        "raw_references": refs,
        "layout_map": layout
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(structure, f, ensure_ascii=False, indent=2)

# --- 辅助函数：任务切分 (升级版：强制隔离标题) ---
def split_text_into_chunks(text, max_chars):
    """
    将文本切分为 LLM 任务片段。
    策略：
    1. 遇到 [[HEADER:...]] 必须强制切断，独立成一个任务。
    2. 普通正文再按 max_chars 进行长度切分。
    """
    # 1. 使用正则保留分隔符的方式切分
    # 捕获组 () 会让 split 保留分隔符本身
    header_pattern = re.compile(r'(\[\[HEADER:.*?\]\])', re.IGNORECASE)
    segments = header_pattern.split(text)
    
    final_chunks = []
    
    for seg in segments:
        seg = seg.strip()
        if not seg: continue
        
        # --- 情况 A: 是标题 -> 强制独立 ---
        if header_pattern.match(seg):
            final_chunks.append(seg)
            
        # --- 情况 B: 是正文 -> 按长度切分 ---
        else:
            # 原有的按段落长度合并逻辑
            paragraphs = seg.split('\n\n')
            buffer = []
            buffer_len = 0
            
            for p in paragraphs:
                p = p.strip()
                if not p: continue
                
                # 如果当前缓冲 + 新段落 > 最大长度，则封包
                if buffer_len + len(p) > max_chars and buffer:
                    final_chunks.append("\n\n".join(buffer))
                    buffer = []
                    buffer_len = 0
                
                buffer.append(p)
                buffer_len += len(p)
            
            # 处理残留 buffer
            if buffer:
                final_chunks.append("\n\n".join(buffer))
                
    return final_chunks

# --- HTML 生成器 (最终增强版：修复标题漏网、公式丢失、图表错位) ---
def generate_html_report(llm_result_path: str, paper_vis_dir: str):
    # 1. 路径准备
    cache_path = llm_result_path.replace("_llm_result.txt", "_llm_cache.json")
    if not os.path.exists(cache_path):
        return "Error: 找不到缓存文件，无法执行高级可视化。"
    
    # 获取论文名称 (即文件夹名)
    raw_name = os.path.basename(paper_vis_dir)
    html_path = os.path.join(paper_vis_dir, f"{raw_name}_Report.html")
    
    # --- 【新增】资源搬运准备 ---
    # 目标目录: ./vis_output/{PaperName}/assets
    vis_assets_dest = os.path.join(paper_vis_dir, "assets")
    if not os.path.exists(vis_assets_dest):
        os.makedirs(vis_assets_dest, exist_ok=True)
        
    # 源目录推导: 假设 extracted_output 与 vis_output 在同一级根目录下
    # paper_vis_dir 通常是 .../vis_output/{PaperName}
    # 我们需要找到 .../extracted_output/{PaperName}/assets
    root_dir = os.path.dirname(os.path.dirname(paper_vis_dir)) 
    extracted_assets_src = os.path.join(root_dir, "extracted_output", raw_name, "assets")
    
    # 调试信息 (可选)
    # print(f"DEBUG: Copying assets from {extracted_assets_src} to {vis_assets_dest}")
    
    # 读取 Cache JSON
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
    except Exception as e:
        return f"JSON 读取失败: {e}"

    tasks = cache_data.get("tasks", [])
    raw_refs = cache_data.get("raw_references", "")
    layout_map = cache_data.get("layout_map", {})

    # ==========================================================================
    # 2. 构建资源字典 & 执行搬运 (Copy)
    # ==========================================================================
    meta_task = None
    asset_task = None
    body_tasks = []

    for t in tasks:
        if t['type'] == 'meta': meta_task = t
        elif t['type'] == 'asset': asset_task = t
        else: body_tasks.append(t)

    assets_map = {}
    
    # 定义搬运函数
    def copy_and_get_rel_path(asset_id):
        filename = f"{asset_id}.png"
        src_file = os.path.join(extracted_assets_src, filename)
        dst_file = os.path.join(vis_assets_dest, filename)
        
        # 执行拷贝
        if os.path.exists(src_file):
            shutil.copy2(src_file, dst_file)
        
        # 返回 HTML 用的相对路径
        return f"./assets/{filename}"

    if asset_task:
        src_full = asset_task.get('src', '')
        trans_full = asset_task.get('trans', '')
        
        # A. Captioned Assets (有标题的图表)
        src_iter = re.finditer(r'\[\[ASSET_CAPTION:\s*(.*?)\s*\|\s*(.*?)\]\]', src_full, re.DOTALL)
        for m in src_iter:
            aid = m.group(1).strip()
            src_txt = m.group(2).strip()
            
            trans_match = re.search(fr'<asset id=["\']?{re.escape(aid)}["\']?>(.*?)</asset>', trans_full, re.DOTALL)
            trans_txt = trans_match.group(1).strip() if trans_match else "(未找到译文)"
            
            # --- 核心：在这里搬运 ---
            rel_path = copy_and_get_rel_path(aid)
            
            assets_map[aid] = {
                "id": aid, "type": "captioned", "src": src_txt, "trans": trans_txt, "path": rel_path
            }
            
        # B. Placeholder Assets (无标题的公式/插图)
        ph_iter = re.finditer(r'\[\[ASSET_PLACEHOLDER:\s*(.*?)\]\]', src_full)
        for m in ph_iter:
            aid = m.group(1).strip()
            if aid not in assets_map:
                # --- 核心：在这里搬运 ---
                rel_path = copy_and_get_rel_path(aid)
                assets_map[aid] = {
                    "id": aid, "type": "placeholder", "src": "", "trans": "", "path": rel_path
                }

    # ==========================================================================
    # 3. 渲染逻辑 (物理优先 + 逻辑引用兜底)
    # ==========================================================================
    def clean_xml_and_headers(text):
        if not text: return ""
        text = re.sub(r'^```xml', '', text).replace('```', '')
        text = re.sub(r'\[\[HEADER:\s*(.*?)\]\]', r'\1', text)
        text = text.replace('<header>', '').replace('</header>', '') 
        text = text.replace('<p>', '').replace('</p>', '<br>')
        text = re.sub(r'\[\[LINK:\s*([^\|]+)\|(.*?)\]\]', r'<a href="#\1" class="internal-link">\2</a>', text)
        def ref_sub(m):
            full_str = m.group(1) 
            first_num = re.search(r'\d+', full_str)
            if first_num: return f'<a href="#ref-{first_num.group(0)}" class="citation-mark">{full_str}</a>'
            return f'<span class="citation-mark">{full_str}</span>'
        text = re.sub(r'(\[\s*\d+(?:[\s,\-~]+\d+)*\s*\])', ref_sub, text)
        return text

    # --- HTML 组装 ---
    html_meta = ""
    if meta_task:
        m_src = meta_task.get('src', '')
        m_trans = meta_task.get('trans', '')
        t_en = re.search(r'\[\[META_TITLE:(.*?)\]\]', m_src, re.DOTALL)
        t_en = t_en.group(1).strip() if t_en else ""
        t_zh = re.search(r'<meta_title>(.*?)</meta_title>', m_trans, re.DOTALL)
        t_zh = t_zh.group(1).strip() if t_zh else ""
        a_en = re.search(r'\[\[META_AUTHOR:(.*?)\]\]', m_src, re.DOTALL)
        a_en = a_en.group(1).strip() if a_en else ""
        a_zh = re.search(r'<meta_author>(.*?)</meta_author>', m_trans, re.DOTALL)
        a_zh = a_zh.group(1).strip() if a_zh else ""
        html_meta = f"""<div class="meta-section"><h1 class="meta-title-en">{t_en}</h1><h1 class="meta-title-zh">{t_zh}</h1><div class="meta-author-en">{a_en}</div><div class="meta-author-zh">{a_zh}</div></div><hr class="meta-divider">"""

    html_body = ""
    placed_assets = set()
    
    for task in body_tasks:
        global_task_id = task['id']
        layout_assets = layout_map.get(str(global_task_id), [])
        
        # 1. 物理位置插入
        for aid in layout_assets:
            if aid in assets_map and aid not in placed_assets:
                asset = assets_map[aid]
                html_body += render_asset_html(aid, asset)
                placed_assets.add(aid)
        
        # 2. 文本
        src_txt = task.get('src', '')
        trans_txt = task.get('trans', '')
        is_header_src = "[[HEADER:" in src_txt
        is_header_trans = "[[HEADER:" in trans_txt or "<header>" in trans_txt
        row_class = "header-row" if (is_header_src or is_header_trans) else "text-row"
        display_src = re.sub(r'\[\[HEADER:\s*(.*?)\]\]', r'\1', src_txt)
        display_src = re.sub(r'(\[\s*\d+(?:[\s,\-~]+\d+)*\s*\])', r'<span class="citation-mark-src">\1</span>', display_src)
        display_trans = clean_xml_and_headers(trans_txt)
        
        html_body += f"""<div class="row {row_class}"><div class="col-src">{display_src}</div><div class="col-trans">{display_trans}</div></div>"""
        
        # 3. 逻辑引用补漏
        mentions = re.findall(r'\[\[LINK:\s*([^\|]+)\|', src_txt)
        for mid in mentions:
            if mid in assets_map and mid not in placed_assets:
                asset = assets_map[mid]
                html_body += render_asset_html(mid, asset)
                placed_assets.add(mid)

    # 4. 剩余资源
    remaining = [k for k in assets_map.keys() if k not in placed_assets]
    if remaining:
        html_body += '<div class="row"><div style="width:100%; text-align:center; color:#999; padding:20px;">--- 附录资源 (未在正文位置或引用中检测到) ---</div></div>'
        for mid in remaining:
            asset = assets_map[mid]
            html_body += render_asset_html(mid, asset)

    html_refs = ""
    if raw_refs:
        refs_content = re.sub(r'\[\[HEADER:.*?\]\]', '', raw_refs).strip()
        ref_entries = re.split(r'\[(\d+)\]', refs_content)
        ref_items = ""
        for i in range(1, len(ref_entries), 2):
            rid = ref_entries[i]
            rtext = ref_entries[i+1].strip()
            ref_items += f"""<div class="ref-item" id="ref-{rid}"><div class="ref-id">[{rid}]</div><div class="ref-text">{rtext}</div></div>"""
        html_refs = f"""<div class="ref-section"><h2 class="ref-title">References</h2><div class="ref-list">{ref_items}</div></div>"""

    # --- HTML Template ---
    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{raw_name}</title>
    <style>
        :root {{ --primary: #2c3e50; --accent: #3498db; --bg: #f8f9fa; --border: #e0e0e0; --header-bg: #eef6fc; --header-text: #2980b9; }}
        body {{ font-family: "Segoe UI", Roboto, "Microsoft YaHei", sans-serif; margin: 0; background: var(--bg); color: #333; line-height: 1.6; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: #fff; box-shadow: 0 0 20px rgba(0,0,0,0.05); }}
        .meta-section {{ padding: 40px; text-align: center; background: #fff; }}
        .meta-title-en {{ font-size: 1.8em; color: #2c3e50; margin-bottom: 10px; font-weight: 700; }}
        .meta-title-zh {{ font-size: 1.6em; color: #34495e; margin-top: 0; margin-bottom: 20px; font-weight: 400; }}
        .meta-author-en {{ font-size: 1em; color: #7f8c8d; font-style: italic; }}
        .meta-author-zh {{ font-size: 1em; color: #16a085; font-weight: bold; margin-top: 5px; }}
        .meta-divider {{ border: 0; border-top: 1px solid #eee; margin: 0; }}
        .row {{ display: flex; border-bottom: 1px solid var(--border); }}
        .col-src {{ flex: 1; padding: 20px; border-right: 1px solid var(--border); color: #555; font-family: "Cambria", serif; font-size: 15px; background: #fff; }}
        .col-trans {{ flex: 1; padding: 20px; color: #111; font-size: 16px; background: #fdfdfd; }}
        .header-row {{ background-color: var(--header-bg) !important; border-bottom: 2px solid #d6eaf8; }}
        .header-row .col-src, .header-row .col-trans {{ font-weight: bold; color: var(--header-text); font-size: 1.2em; background: transparent; }}
        .asset-row {{ display: block; background: #f4f4f4; padding: 20px; border-bottom: 1px solid #ddd; }}
        .asset-card {{ background: #fff; max-width: 90%; margin: 0 auto; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); overflow: hidden; }}
        .placeholder-card {{ max-width: 60%; }} 
        .asset-header {{ background: #f8f9fa; padding: 10px 20px; font-weight: bold; color: #555; border-bottom: 1px solid #eee; }}
        .asset-header-mini {{ background: #f8f9fa; padding: 5px 15px; font-size: 0.9em; color: #888; border-bottom: 1px solid #eee; }}
        .asset-tag {{ background: #3498db; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 0.8em; margin-right: 5px; }}
        .asset-img {{ display: block; max-width: 100%; max-height: 600px; margin: 0 auto; }}
        .asset-img-raw {{ display: block; max-width: 100%; margin: 10px auto; }}
        .asset-desc-box {{ padding: 20px; background: #fffdf5; border-top: 1px solid #eee; }}
        .asset-desc-en {{ font-style: italic; color: #666; margin-bottom: 10px; font-size: 0.95em; border-bottom: 1px dashed #ddd; padding-bottom: 8px; }}
        .asset-desc-zh {{ font-weight: 500; color: #2c3e50; }}
        .ref-section {{ padding: 40px; background: #fff; border-top: 4px solid #2c3e50; }}
        .ref-title {{ text-align: center; color: #2c3e50; margin-bottom: 30px; }}
        .ref-list {{ display: grid; grid-template-columns: 1fr; gap: 15px; }}
        .ref-item {{ display: flex; align-items: flex-start; }}
        .ref-id {{ min-width: 40px; font-weight: bold; color: #e74c3c; text-align: right; margin-right: 15px; }}
        .ref-text {{ font-size: 0.95em; color: #555; word-break: break-word; }}
        .citation-mark {{ color: #e74c3c; font-weight: bold; cursor: pointer; background: rgba(231, 76, 60, 0.1); padding: 0 2px; border-radius: 2px; font-size: 0.9em; }}
        .citation-mark-src {{ color: #999; font-size: 0.9em; }}
        .internal-link {{ color: #3498db; text-decoration: none; font-weight: 500; background: rgba(52,152,219,0.1); padding: 0 4px; border-radius: 3px; }}
        .internal-link:hover {{ background: rgba(52,152,219,0.2); text-decoration: underline; }}
        :target {{ scroll-margin-top: 20px; animation: highlight 2s ease; }}
        @keyframes highlight {{ 0% {{ background-color: #fff3cd; }} 100% {{ background-color: transparent; }} }}
    </style>
</head>
<body>
    <div class="container">
        {html_meta}
        <div class="main-content">{html_body}</div>
        {html_refs}
    </div>
</body>
</html>"""

    try:
        with open(html_path, 'w', encoding='utf-8') as f: f.write(full_html)
        return html_path
    except Exception as e:
        return f"HTML 写入失败: {e}"

# 单独的渲染函数
def render_asset_html(mid, asset):
    if asset["type"] == "placeholder":
        return f"""<div class="row asset-row" id="{mid}"><div class="asset-card placeholder-card"><div class="asset-header-mini">{mid}</div><img src="{asset['path']}" class="asset-img-raw" loading="lazy"></div></div>"""
    else:
        return f"""<div class="row asset-row" id="{mid}"><div class="asset-card"><div class="asset-header"><span class="asset-tag">Resource</span> {mid}</div><img src="{asset['path']}" class="asset-img" loading="lazy"><div class="asset-desc-box"><div class="asset-desc-en">{asset['src']}</div><div class="asset-desc-zh">{asset['trans']}</div></div></div></div>"""