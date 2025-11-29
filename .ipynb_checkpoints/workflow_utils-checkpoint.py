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
# --- 场景 D: 强力纠错专用 (原文 + 旧译文 + 用户指引) ---
SYSTEM_PROMPT_CORRECTION = """
你是一个高级学术翻译纠错专家。你的任务是根据【用户指引】修正一段【有瑕疵的译文】。

**输入信息:**
1. **原文**: 原始英文片段。
2. **旧译文**: 之前被判定为不准确的翻译。
3. **用户指引**: 用户指出的错误点或修改要求（这是最高指令）。

**核心规则:**
1. **精准修正**: 严格遵循用户的指引（例如：修正术语、调整语序、保留原文等）。
2. **格式保持**: 根据用户提示，新增或删除原文中的结构化标记，如 `[[REFERENCE: ...]]`, `[[FIGURE: ...]]`, `[[HEADER: ...]]`。
3. **引用链接 (Link)**: 
   - 仅针对图表引用 (如 "Fig. 1", "Table 2", "Eq. 3", "Algorithm. 4") 使用 `[[LINK: ID|原文]]` 格式。
   - **严格禁止**对参考文献引用 (如 "[1]", "[22]", "[1-5]") 添加链接。参考文献引用必须原样保留，如 `[22]`。
4. **仅输出结果**: 直接输出修正后的译文，不要输出“好的”、“已修改”等废话。
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
            "Header": "#8e44ad",    # 深紫 (章节名)
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
            ("遮罩 (Mask) - [7]", "Mask"),
            ("章节 (Header) - [8]", "Header")
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
        elif k == '5': self.set_tool("Title")   
        elif k == '6': self.set_tool("Author")  
        elif k == '7': self.set_tool("Mask")    
        elif k == '8': self.set_tool("Header")    
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
    
    extracted_assets_dir = os.path.join(raw_text_dir, clean_name, "assets")
    layout_config_path = os.path.join(raw_text_dir, clean_name, "layout_config.json")
    
    if not os.path.exists(os.path.dirname(layout_config_path)):
        os.makedirs(os.path.dirname(layout_config_path), exist_ok=True)

    doc = fitz.open(pdf_path)
    
    # =========================================================
    # 1. 初始化数据
    # =========================================================
    init_data = {}
    saved_json = {}

    if os.path.exists(layout_config_path):
        print(f"📂 检测到历史标注记录: {layout_config_path}，正在加载...")
        try:
            with open(layout_config_path, 'r', encoding='utf-8') as f:
                saved_json = json.load(f)
        except Exception as e:
            print(f"⚠️ 加载历史记录失败 ({e})，将忽略历史文件。")
            saved_json = {}

    for i, page in enumerate(doc):
        w, h = page.rect.width, page.rect.height
        page_items = []
        
        if str(i) in saved_json:
            raw_items = saved_json[str(i)]
            for item in raw_items:
                r = item['rect']
                page_items.append({
                    'rect': fitz.Rect(r[0], r[1], r[2], r[3]),
                    'type': item['type'],
                    'id': item['id'],
                    'role': item['role']
                })
        
        has_content_area = any(x['type'] == 'ContentArea' for x in page_items)
        if not has_content_area:
            default_rect = fitz.Rect(0, h*0.08, w, h*0.92)
            page_items.insert(0, {
                'rect': default_rect,
                'type': 'ContentArea',
                'id': 0, 
                'role': 'Body'
            })
            
        init_data[i] = page_items

    # =========================================================
    # 2. 启动交互编辑器
    # =========================================================
    editor = LayoutEditor(doc, init_data)
    verified_data = editor.data

    # =========================================================
    # 3. 保存标注结果
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
            
            # --- [关键修改] 这里加入 Header 跳过，防止对其截图 ---
            if item['type'] in ['ContentArea', 'Mask', 'Header']: continue
            # -----------------------------------------------
            
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
    

    for p_idx, page in enumerate(doc):
        page_asset_inserts = []
        page_items = verified_data.get(p_idx, [])
        ignore_rects = []
        content_rect = page.rect 

        for item in page_items:
            if item['type'] == 'ContentArea': 
                content_rect = item['rect']
            elif item['type'] in ['Mask', 'Title', 'Author']: 
                ignore_rects.append(item['rect'])
            
            # --- [关键修改] 处理 Header 逻辑 ---
            elif item['type'] == 'Header':
                # 1. 视为 Mask，防止正文重复提取
                ignore_rects.append(item['rect'])
                # 2. OCR 提取文字
                header_text = page.get_text("text", clip=item['rect']).strip().replace('\n', ' ')
                # 3. 构造强制 Header 标签
                page_asset_inserts.append({
                    "rect": item['rect'],
                    "text": f"[[HEADER: {header_text}]]", 
                    "id": f"Header_{item['id']}" 
                })
            # -----------------------------------
            
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
                # --- [关键修改] 删除原有的 header_pattern 猜测逻辑 ---
                # 原逻辑: if header_pattern.match(first_line)...
                # 现逻辑: 只要是正文文本，全部原样进入流，由上方 Header 标签进行截断
                raw_paragraph_stream.append(text)
                # ------------------------------------------------
            else:
                # 这里包含 [[ASSET_INSERT]] 和我们生成的 [[HEADER: ...]]
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
    # ================= 配置区域 =================
    API_KEY = "ollama" 
    BASE_URL = "http://localhost:11434/v1"
    
    # 🟢 普通模型 (初译): 速度快，用于首次生成
    MODEL_NORMAL = "qwen2.5:7b" 
    
    # 🔴 强力模型 (纠错): 逻辑强，用于处理用户反馈
    # (如果没有下载 14b/32b，请改回 qwen2.5:7b)
    MODEL_STRONG = "qwen2.5:14b" 
    # ===========================================
    
    from openai import OpenAI

    # 1. 读取内容
    if os.path.isfile(full_text_path_or_content):
         with open(full_text_path_or_content, 'r', encoding='utf-8') as f: content = f.read()
    else:
        content = full_text_path_or_content

    # 2. 预处理：分离 RefMap
    ref_map_str = ""
    body_text = content
    map_match = re.search(r'\[\[REF_MAP_START\]\]\n(.*?)\n\[\[REF_MAP_END\]\]', content, re.DOTALL)
    if map_match:
        ref_map_str = map_match.group(1)
        body_text = content.replace(map_match.group(0), "").strip()
    
    # 3. 四段式切分
    meta_text, body_text, assets_text, raw_refs_text = split_content_smart(body_text)
    
    # 4. 构建任务列表 (剥离 Layout Info + 注入结构化 Tags)
    raw_chunks = []
    layout_map_global = {} 
    
    # A. Meta
    if meta_text: 
        raw_chunks.append({"text": meta_text, "type": "meta"})
        
    # B. Body
    if body_text:
        # 切分文本块
        body_parts, local_layout_map = split_text_into_chunks_with_layout(body_text, MAX_CHUNK_CHARS)
        offset = len(raw_chunks) 
        
        for idx, part in enumerate(body_parts):
            # --- 关键步骤：在发送给 LLM 前，将 [1] 和 Fig. 1 转化为死板的 [[TAG]] ---
            tagged_part = tag_text_elements(part, ref_map_str)
            
            raw_chunks.append({"text": tagged_part, "type": "body"})
            
            # 映射布局信息
            if idx in local_layout_map:
                layout_map_global[idx + offset] = local_layout_map[idx]
            
    # C. Assets
    if assets_text: 
        raw_chunks.append({"text": assets_text, "type": "asset"})

    # ---------------------------------------------------------
    # 阶段一：任务编排与缓存同步
    # ---------------------------------------------------------
    print(f"📋 [阶段一] 任务编排: 总片段 {len(raw_chunks)} 个")
    
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
            
            # 自动重置失败的任务，给它重来的机会
            if task_entry.get("status") == "failed":
                task_entry["status"] = "pending"
                
            # 确保 user_hint 字段存在 (兼容旧缓存)
            if "user_hint" not in task_entry: task_entry["user_hint"] = ""
            if "old_trans" not in task_entry: task_entry["old_trans"] = ""
                
        else:
            # 新任务
            task_entry = { 
                "id": i, 
                "type": c_type, 
                "chunk_hash": h, 
                "status": "pending", 
                "src": c_text, 
                "trans": "",
                "user_hint": "",
                "old_trans": ""
            }
        
        current_tasks.append(task_entry)

    # ---------------------------------------------------------
    # 阶段二：执行推理 (双模型切换)
    # ---------------------------------------------------------
    pending_tasks = [t for t in current_tasks if t["status"] == "pending"]
    
    if pending_tasks:
        print(f"\n🚀 [阶段二] 开始推理 (待处理: {len(pending_tasks)})...")
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        
        # 基础 Prompt 映射
        PROMPT_MAP = {
            "meta": SYSTEM_PROMPT_META,
            "body": SYSTEM_PROMPT_BODY, # 此时 Body 已经是极简版
            "asset": SYSTEM_PROMPT_ASSET.replace("{ref_map_str}", ref_map_str)
        }
        
        for task in current_tasks:
            if task["status"] != "pending": continue
            
            idx = task["id"]
            t_type = task["type"]
            
            # --- 核心逻辑：判断是否需要纠错模式 ---
            user_hint = task.get("user_hint", "").strip()
            old_trans = task.get("old_trans", "").strip()
            
            # 只有当既有用户提示，又有旧译文时，才进入纠错模式
            is_correction_mode = bool(user_hint and old_trans)
            
            # 1. 选择模型
            current_model = MODEL_STRONG if is_correction_mode else MODEL_NORMAL
            
            # 2. 构建 Prompt 和 Input
            if is_correction_mode:
                # === 🔴 纠错模式 ===
                print(f"   🔥 Part {idx+1} [纠错模式 -> {current_model}] ...", end="", flush=True)
                
                sys_prompt = SYSTEM_PROMPT_CORRECTION
                # 构造复合输入：原文 + 旧译文 + 用户指引
                user_content = (
                    f"【原文】:\n{task['src']}\n\n"
                    f"【旧译文(有误)】:\n{old_trans}\n\n"
                    f"【用户指引(最高优先级)】:\n{user_hint}"
                )
            else:
                # === 🟢 普通模式 ===
                print(f"   ⚡ Part {idx+1} [普通翻译 -> {current_model}] ...", end="", flush=True)
                sys_prompt = PROMPT_MAP.get(t_type, PROMPT_MAP["body"])
                user_content = task['src']

            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_content}
            ]
            
            # 3. API 调用
            success = False
            for attempt in range(3):
                try:
                    response = client.chat.completions.create(
                        model=current_model, 
                        messages=messages, 
                        temperature=0.1,
                        stream=False
                    )
                    res_text = response.choices[0].message.content
                    
                    # 清洗 Markdown 代码块
                    res_text = re.sub(r'^```xml\s*', '', res_text)
                    res_text = re.sub(r'```$', '', res_text)
                    
                    if res_text:
                        task["trans"] = res_text.strip()
                        task["status"] = "success"
                        
                        # 注意：纠错成功后，保留 user_hint 和 old_trans 作为历史记录，
                        # 但 status 设为 success，防止无限重跑。
                        
                        print(" ✅")
                        success = True
                        break
                except Exception as e:
                    print(f" ⚠️ {e}")
                    time.sleep(2)
            
            if not success:
                task["status"] = "failed"
                print(" ❌")
            
            # 每次任务完成后立即保存，防止中断
            if cache_path: 
                _save_cache(cache_path, MODEL_NORMAL, current_tasks, raw_refs_text, layout_map_global)
    else:
        print("\n🎉 所有任务已完成，无需新增推理。")

    # ---------------------------------------------------------
    # 阶段三：刷新输出文件
    # ---------------------------------------------------------
    print("💾 [阶段三] 刷新结果文件...")
    
    # 只合并成功的任务
    final_body = "\n".join([t["trans"] for t in current_tasks if t["status"] == "success"])
    
    # 处理参考文献块
    final_refs = ""
    if raw_refs_text:
        final_refs = f"\n<header_block><src>References</src><trans>参考文献</trans></header_block>\n"
        # 简单清洗 header 标记
        clean_ref_content = re.sub(r'\[\[HEADER:.*?\]\]', '', raw_refs_text).strip()
        final_refs += f"<ref_block><src>{clean_ref_content}</src></ref_block>"

    with open(output_path, 'w', encoding='utf-8') as f: 
        f.write(final_body + "\n" + final_refs)
        
    return output_path

# 辅助保存函数
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
    """
    生成交互式 HTML 报告，支持前端直接调用 Python API 进行纠错和重译。
    """
    # 1. 路径准备
    cache_path = llm_result_path.replace("_llm_result.txt", "_llm_cache.json")
    if not os.path.exists(cache_path):
        return "Error: 找不到缓存文件，无法执行高级可视化。"
    
    raw_name = os.path.basename(paper_vis_dir)
    html_path = os.path.join(paper_vis_dir, f"{raw_name}_Report.html")
    
    # 资源搬运准备
    vis_assets_dest = os.path.join(paper_vis_dir, "assets")
    if not os.path.exists(vis_assets_dest):
        os.makedirs(vis_assets_dest, exist_ok=True)
        
    root_dir = os.path.dirname(os.path.dirname(paper_vis_dir)) 
    extracted_assets_src = os.path.join(root_dir, "extracted_output", raw_name, "assets")
    
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
    # 2. 构建资源字典 & 执行搬运
    # ==========================================================================
    meta_task = None
    asset_task = None
    body_tasks = []

    for t in tasks:
        if t['type'] == 'meta': meta_task = t
        elif t['type'] == 'asset': asset_task = t
        else: body_tasks.append(t)

    assets_map = {}
    
    # Helper: 搬运单张图片
    def copy_asset_image(asset_id):
        filename = f"{asset_id}.png"
        src_file = os.path.join(extracted_assets_src, filename)
        dst_file = os.path.join(vis_assets_dest, filename)
        if os.path.exists(src_file):
            shutil.copy2(src_file, dst_file)
        return f"./assets/{filename}"

    if asset_task:
        src_full = asset_task.get('src', '')
        trans_full = asset_task.get('trans', '')
        
        # A. Captioned
        src_iter = re.finditer(r'\[\[ASSET_CAPTION:\s*(.*?)\s*\|\s*(.*?)\]\]', src_full, re.DOTALL)
        for m in src_iter:
            aid = m.group(1).strip()
            src_txt = m.group(2).strip()
            trans_match = re.search(fr'<asset id=["\']?{re.escape(aid)}["\']?>(.*?)</asset>', trans_full, re.DOTALL)
            trans_txt = trans_match.group(1).strip() if trans_match else "(未找到译文)"
            rel_path = copy_asset_image(aid)
            assets_map[aid] = { "id": aid, "type": "captioned", "src": src_txt, "trans": trans_txt, "path": rel_path }
            
        # B. Placeholder
        ph_iter = re.finditer(r'\[\[ASSET_PLACEHOLDER:\s*(.*?)\]\]', src_full)
        for m in ph_iter:
            aid = m.group(1).strip()
            if aid not in assets_map:
                rel_path = copy_asset_image(aid)
                assets_map[aid] = { "id": aid, "type": "placeholder", "src": "", "trans": "", "path": rel_path }

    # ==========================================================================
    # 3. 渲染逻辑
    # ==========================================================================
    def clean_xml_and_headers(text):
        if not text: return ""
        text = re.sub(r'^```xml', '', text).replace('```', '')
        # 简单处理：加粗标题
        text = re.sub(r'<header>(.*?)</header>', r'<b>\1</b>', text)
        text = text.replace('<p>', '').replace('</p>', '<br>')
        text = re.sub(r'\[\[HEADER:\s*(.*?)\]\]', r'\1', text)
        return text

    # --- HTML 组装 ---
    
    # 1. Meta (关键修复: 初始化为空)
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
        
        html_meta = f"""
        <div class="meta-section">
            <h1 class="meta-title-en">{t_en}</h1>
            <h1 class="meta-title-zh">{t_zh}</h1>
            <div class="meta-author-en">{a_en}</div>
            <div class="meta-author-zh">{a_zh}</div>
        </div>
        <hr class="meta-divider">
        """

    # 2. Body
    html_body = ""
    placed_assets = set()
    
    for task in body_tasks:
        task_id = task['id']
        
        # 获取用户提示
        existing_hint = task.get("user_hint", "")
        hint_class = "has-hint" if existing_hint else ""
        status_text = f"(状态: {task.get('status')})" if existing_hint else ""
        
        # 物理插入资源
        layout_assets = layout_map.get(str(task_id), [])
        for aid in layout_assets:
            if aid in assets_map and aid not in placed_assets:
                html_body += render_asset_html(aid, assets_map[aid])
                placed_assets.add(aid)
        
        src_txt = task.get('src', '')
        trans_txt = clean_xml_and_headers(task.get('trans', ''))
        
        # 渲染带有反馈面板的行
        html_body += f"""
        <div class="row-container" id="task-{task_id}">
            <div class="row text-row {hint_class}">
                <div class="col-src">{src_txt}</div>
                <div class="col-trans">
                    {trans_txt}
                    <div class="hint-badge" style="display: {'block' if existing_hint else 'none'}">
                        💡 上次提示: {existing_hint} {status_text}
                    </div>
                </div>
            </div>
            <div class="feedback-panel" style="display: none;">
                <div class="feedback-header">🛠️ 人工纠错向导 (Task {task_id})</div>
                <textarea class="feedback-input" placeholder="请输入给 AI 的翻译提示...">{existing_hint}</textarea>
                <div style="margin-top:5px;">
                    <button class="btn btn-primary" style="font-size:0.8em; padding:4px 10px;" 
                            onclick="saveFeedback('{task_id}', this)">确认修改并标记</button>
                    <span class="status-saved">✅ 保存成功</span>
                </div>
            </div>
        </div>
        """
        
        # 逻辑引用补漏 (略，逻辑同前)
        # ...

    # 3. Refs
    html_refs = ""
    if raw_refs:
        refs_content = re.sub(r'\[\[HEADER:.*?\]\]', '', raw_refs).strip()
        # 简单渲染
        html_refs = f'<div class="ref-section"><pre>{refs_content}</pre></div>'

    # --- CSS & JS (完整版) ---
    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{raw_name} - Interactive Mode</title>
    <style>
        :root {{ --primary: #2c3e50; --accent: #3498db; --bg: #f8f9fa; --border: #e0e0e0; }}
        body {{ font-family: "Segoe UI", sans-serif; margin: 0; background: var(--bg); padding-bottom: 100px; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: #fff; box-shadow: 0 0 20px rgba(0,0,0,0.05); }}
        
        /* Meta Styles */
        .meta-section {{ padding: 40px; text-align: center; background: #fff; }}
        .meta-title-en {{ font-size: 1.8em; color: #2c3e50; font-weight: 700; }}
        .meta-title-zh {{ font-size: 1.6em; color: #34495e; font-weight: 400; }}
        .meta-author-en {{ font-style: italic; color: #7f8c8d; }}
        .meta-author-zh {{ color: #16a085; font-weight: bold; }}
        
        /* Toolbar */
        .toolbar {{ position: fixed; top: 20px; right: 20px; background: #fff; padding: 10px 20px; 
                    box-shadow: 0 4px 12px rgba(0,0,0,0.15); border-radius: 8px; z-index: 999; display: flex; gap: 10px; align-items: center; }}
        .btn {{ padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; transition: 0.2s; }}
        .btn-primary {{ background: var(--accent); color: #fff; }}
        .btn-danger {{ background: #e74c3c; color: #fff; }}
        .btn-success {{ background: #27ae60; color: #fff; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}

        /* Loading Overlay */
        #loading-mask {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(255,255,255,0.8); 
                         z-index: 2000; display: none; justify-content: center; align-items: center; flex-direction: column; }}
        .spinner {{ width: 40px; height: 40px; border: 4px solid #f3f3f3; border-top: 4px solid var(--accent); border-radius: 50%; animation: spin 1s linear infinite; }}
        @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}

        /* Grid */
        .row-container {{ border-bottom: 1px solid var(--border); }}
        .row {{ display: flex; }}
        .col-src, .col-trans {{ flex: 1; padding: 20px; }}
        .col-src {{ border-right: 1px solid var(--border); color: #555; background: #fff; }}
        
        /* Feedback Mode */
        body.feedback-mode .row:hover {{ background: #fdfdfd; }}
        body.feedback-mode .col-trans {{ cursor: pointer; outline: 1px dashed #ccc; }}
        
        .feedback-panel {{ background: #f1f8ff; padding: 15px 20px; border-top: 1px solid #d6eaf8; display: none; }}
        .feedback-header {{ font-weight: bold; color: #2c3e50; margin-bottom: 5px; font-size: 0.9em; }}
        .feedback-input {{ width: 100%; height: 60px; padding: 8px; border: 1px solid #bdc3c7; border-radius: 4px; font-family: inherit; margin-bottom: 5px; }}
        
        .hint-badge {{ margin-top: 10px; padding: 5px 10px; background: #fff3cd; border: 1px solid #ffeeba; color: #856404; font-size: 0.85em; border-radius: 4px; }}
        .status-saved {{ color: #27ae60; font-weight: bold; margin-left: 10px; display: none; }}
        
        /* Assets */
        .asset-row {{ background: #f4f4f4; padding: 20px; display: block; }}
        .asset-card {{ background: #fff; max-width: 90%; margin: 0 auto; border-radius: 8px; padding: 10px; text-align: center; }}
        .asset-img {{ max-width: 100%; }}
    </style>
</head>
<body>
    <div id="loading-mask">
        <div class="spinner"></div>
        <div style="margin-top: 15px; font-size: 1.2em; color: #555;">正在后台重译并生成报告，请稍候...</div>
    </div>

    <div class="toolbar">
        <div id="status-text" style="margin-right: 10px; color: #666;">浏览模式</div>
        <button class="btn btn-primary" id="toggle-btn" onclick="toggleFeedbackMode()">进入纠错模式</button>
        <button class="btn btn-success" id="run-btn" onclick="triggerRerun()" style="display:none;">🚀 应用修改并重译</button>
    </div>

    <div class="container">
        {html_meta}
        <div class="main-content">{html_body}</div>
        {html_refs}
    </div>

    <script>
        const API_BASE = "";
        let isFeedbackMode = false;

        function toggleFeedbackMode() {{
            isFeedbackMode = !isFeedbackMode;
            document.body.classList.toggle('feedback-mode');
            
            const toggleBtn = document.getElementById('toggle-btn');
            const runBtn = document.getElementById('run-btn');
            const statusText = document.getElementById('status-text');
            
            if (isFeedbackMode) {{
                toggleBtn.textContent = "退出纠错模式";
                toggleBtn.classList.replace('btn-primary', 'btn-danger');
                runBtn.style.display = 'block';
                statusText.textContent = "✏️ 点击译文修改，自动保存";
                enableClickHandlers();
            }} else {{
                toggleBtn.textContent = "进入纠错模式";
                toggleBtn.classList.replace('btn-danger', 'btn-primary');
                runBtn.style.display = 'none';
                statusText.textContent = "浏览模式";
                disableClickHandlers();
            }}
        }}

        function enableClickHandlers() {{
            const rows = document.querySelectorAll('.row-container');
            rows.forEach(row => {{
                const transCol = row.querySelector('.col-trans');
                const taskId = row.id.replace('task-', '');
                
                if (transCol.getAttribute('data-bound')) return;
                transCol.setAttribute('data-bound', 'true');

                transCol.onclick = () => {{
                    if (!isFeedbackMode) return;
                    const panel = row.querySelector('.feedback-panel');
                    const isHidden = (panel.style.display === 'none' || panel.style.display === '');
                    panel.style.display = isHidden ? 'block' : 'none';
                }};
            }});
        }}

        function disableClickHandlers() {{
            const panels = document.querySelectorAll('.feedback-panel');
            panels.forEach(p => p.style.display = 'none');
        }}

        async function saveFeedback(taskId, btnElement) {{
            const container = document.getElementById('task-' + taskId);
            const input = container.querySelector('.feedback-input');
            const hint = input.value.trim();
            const statusMsg = container.querySelector('.status-saved');
            
            if (!hint) {{ alert("请输入提示"); return; }}

            const originalText = btnElement.textContent;
            btnElement.disabled = true;
            btnElement.textContent = "保存中...";

            try {{
                const response = await fetch(API_BASE + '/update_task', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ id: taskId, hint: hint }})
                }});
                const data = await response.json();
                
                if (data.status === 'success') {{
                    statusMsg.style.display = 'inline';
                    setTimeout(() => statusMsg.style.display = 'none', 2000);
                    btnElement.textContent = "已保存 (待重译)";
                }} else {{
                    alert("保存失败: " + data.msg);
                    btnElement.textContent = originalText;
                    btnElement.disabled = false;
                }}
            }} catch (err) {{
                alert("连接错误: " + err);
                btnElement.textContent = originalText;
                btnElement.disabled = false;
            }}
        }}

        async function triggerRerun() {{
            if (!confirm("确定要重译吗？")) return;
            const mask = document.getElementById('loading-mask');
            mask.style.display = 'flex';

            try {{
                const response = await fetch(API_BASE + '/trigger_rerun', {{ method: 'POST' }});
                const data = await response.json();
                if (data.status === 'success') {{
                    alert(data.msg);
                    location.reload(); 
                }} else {{
                    alert("失败: " + data.msg);
                    mask.style.display = 'none';
                }}
            }} catch (err) {{
                alert("错误: " + err);
                mask.style.display = 'none';
            }}
        }}
    </script>
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
    

# ==============================================================================
# 6. 交互式服务模块
# ==============================================================================
import http.server
import socketserver
import json
import webbrowser  # <--- 新增
import os
from urllib.parse import urlparse
from functools import partial

def start_interactive_server(project_context, port=8000):
    """
    启动全能服务器：托管 HTML + 处理 API + 自动打开浏览器。
    """
    web_root = project_context['vis_output_dir']
    
    # 自动计算正确的访问地址
    html_name = os.path.basename(project_context['llm_result_path']).replace("_llm_result.txt", "_Report.html")
    target_url = f"http://localhost:{port}/{html_name}"
    
    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'X-Requested-With, Content-type')
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(200, "ok")
            self.end_headers()

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            print(f"📨 [Server] 收到请求: {path}") 

            if path == '/update_task':
                self.handle_update_task()
            elif path == '/trigger_rerun':
                self.handle_trigger_rerun()
            else:
                self.send_error(404, "API Endpoint not found")

        def handle_update_task(self):
            try:
                length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(length)
                data = json.loads(post_data.decode('utf-8'))
                
                task_id = str(data.get('id'))
                user_hint = data.get('hint')
                cache_path = project_context['llm_cache_path']
                
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                
                found = False
                for task in cache_data.get("tasks", []):
                    if str(task["id"]) == task_id:
                        # --- 核心修改：保存旧译文用于对比 ---
                        # 如果这是第一次改，把 trans 存入 old_trans
                        # 如果已经是第二次改，保留最初的 old_trans 或更新它，这里选择更新
                        task['old_trans'] = task.get('trans', '') 
                        
                        task['user_hint'] = user_hint
                        task['status'] = 'pending' # 触发重译
                        task['trans'] = "" # 清空当前显示
                        found = True
                        break
                
                if found:
                    with open(cache_path, 'w', encoding='utf-8') as f:
                        json.dump(cache_data, f, ensure_ascii=False, indent=2)
                    print(f"   ✅ Task {task_id} 反馈已保存 (旧译文已归档)")
                    self.respond_json({'status': 'success'})
                else:
                    print(f"   ❌ Task {task_id} 未找到")
                    self.respond_json({'status': 'error', 'msg': 'Task not found'})
            except Exception as e:
                print(f"   ❌ 处理出错: {e}")
                self.respond_json({'status': 'error', 'msg': str(e)})

        def handle_trigger_rerun(self):
            try:
                print("\n⚡ [Server] 前端触发重译，开始执行...")
                run_smart_analysis(
                    project_context['context_path'], 
                    project_context['llm_result_path'],
                    cache_path=project_context['llm_cache_path']
                )
                generate_html_report(
                    project_context['llm_result_path'], 
                    project_context['vis_output_dir']
                )
                print("✅ [Server] 重译完成，通知前端刷新！")
                self.respond_json({'status': 'success', 'msg': '重译完成'})
            except Exception as e:
                print(f"❌ [Server] 重译出错: {e}")
                self.respond_json({'status': 'error', 'msg': str(e)})

        def respond_json(self, data):
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))

    # 绑定静态目录
    handler_class = partial(Handler, directory=web_root)
    socketserver.TCPServer.allow_reuse_address = True
    
    # 启动服务
    with socketserver.TCPServer(("", port), handler_class) as httpd:
        print(f"🚀 服务器已启动: {target_url}")
        print("🔗 正在自动打开浏览器...")
        
        # --- 核心修改：自动用正确的 http:// 地址打开浏览器 ---
        webbrowser.open(target_url)
        
        print("(提示：此单元格会一直运行 [*]，这是正常的。如需停止请按 Jupyter 的停止按钮)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n🛑 服务器已停止。")