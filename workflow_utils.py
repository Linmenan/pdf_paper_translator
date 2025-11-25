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
   - <meta_author>中文作者/机构</meta_author>
3. **禁止**: 绝对不要输出原文，不要输出任何解释性文字，不要输出 markdown 代码块。
4. **人名处理**: 如果作者是外国人名，建议保留英文或使用通用音译；机构名请翻译。
"""

# --- 场景 B: 正文专用 (学术风格 + 引用处理) ---
SYSTEM_PROMPT_BODY = """
你是一个专业的学术论文翻译引擎。请将输入的学术段落翻译为中文。
**输入资源映射表 (Ref Map):**
{ref_map_str}

**核心规则:**
1. **风格**: 保持学术论文的严谨、客观、逻辑性。
2. **结构**: 
   - 章节标题 -> <header>译文</header>
   - 正文段落 -> <p>译文</p>
3. **引用**: 遇到文中引用 (如 "Figure 1", "Eq. 2")，必须根据 Map 格式化为 `[[LINK: ID|原文]]`。
   - 示例: "As shown in Fig. 1" -> "如图 [[LINK: Figure_1|Fig. 1]] 所示"
4. **禁止**: 绝对不要输出 <src> 原文标签。只输出译文。
5. **保留**: 请保留原文中的引用标记（如 [1], [1-5]），不要修改其格式。
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
        curr_content = next((x for x in self.data.get(self.current_page, []) if x['type'] == 'ContentArea'), None)
        next_idx = self.current_page + 1
        
        if next_idx < self.page_count:
            if next_idx not in self.data: self.data[next_idx] = []
            
            if curr_content:
                if self.doc[self.current_page].rect == self.doc[next_idx].rect:
                     self.data[next_idx] = [x for x in self.data[next_idx] if x['type'] != 'ContentArea']
                     self.data[next_idx].insert(0, curr_content.copy())

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
    """
    if len(text) <= max_len:
        return [text]
    
    # 正则解释：
    # (?<=[.?!;]) : 前面必须是句号、问号、感叹号或分号
    # \s+         : 中间有空格
    # (?=[A-Z0-9]): 后面必须是大写字母或数字 (防止切断 e.g. 或 Fig. 1)
    # 注意：这只是一个启发式规则，能覆盖绝大多数情况
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
    max_split_len: 单个段落最大字符数，超过则尝试在句号处强行拆分
    """
    if not blocks: return []
    merged = []
    buffer = ""
    terminals = ('.', '?', '!', ':', ';', '。', '？', '！', '：', '；')
    hard_boundary_pattern = re.compile(r'^\[\[(HEADER|ASSET_|META_).*?\]\]')

    for block in blocks:
        block = block.strip()
        if not block: continue
        
        # 1. 遇到硬性边界 -> 强制刷新 Buffer
        if hard_boundary_pattern.match(block):
            if buffer:
                # --- 改动点：Flush 时检查长度并拆分 ---
                merged.extend(split_long_buffer_safely(buffer, max_split_len))
                buffer = ""
            merged.append(block)
            continue
        
        # 2. 初始化 Buffer
        if not buffer:
            buffer = block
            continue
            
        # 3. 逻辑判定
        prev_end_char = buffer[-1] if buffer else ""
        
        # 情况 A: 连字符修复
        if prev_end_char == '-':
            buffer = buffer[:-1] + block
        # 情况 B: 句子未结束 (非终止符结尾 OR 下一段小写开头)
        elif (not buffer.endswith(terminals)) or (block[0].islower()):
            buffer = buffer + " " + block
        # 情况 C: 正常的段落结束 (句号结尾 + 大写开头)
        else:
            # 既然段落结束了，就 Flush 进 merged
            # --- 改动点：Flush 时检查长度并拆分 ---
            merged.extend(split_long_buffer_safely(buffer, max_split_len))
            buffer = block # 新的 block 开启新的 buffer

    # 处理残留
    if buffer:
        merged.extend(split_long_buffer_safely(buffer, max_split_len))
    
    return merged

# --- 核心提取逻辑 ---
def extract_text_and_save_assets_smart(pdf_path: str, raw_text_dir: str, vis_output_root: str) -> tuple[str, str, str, int]:
    if not os.path.exists(pdf_path): raise FileNotFoundError(f"PDF missing: {pdf_path}")
    
    clean_name = sanitize_filename(pdf_path)
    os.makedirs(raw_text_dir, exist_ok=True)
    txt_path = os.path.join(raw_text_dir, f"{clean_name}_context.txt")
    
    vis_dir = os.path.join(vis_output_root, clean_name)
    assets_dir = os.path.join(vis_dir, "assets")
    if os.path.exists(assets_dir): shutil.rmtree(assets_dir)
    os.makedirs(assets_dir, exist_ok=True)

    doc = fitz.open(pdf_path)
    
    # 1. 初始化
    init_data = {}
    for i, page in enumerate(doc):
        w, h = page.rect.width, page.rect.height
        init_data[i] = [{'rect': fitz.Rect(0, h*0.08, w, h*0.92), 'type': 'ContentArea'}]

    # 2. 交互校对
    editor = LayoutEditor(doc, init_data)
    verified_data = editor.data

    # 3. 资源聚合 & 元数据提取
    print("🧩 正在处理元数据与资源...")
    assets_agg = {}
    meta_info_blocks = [] # 存储 Title 和 Author
    
    for p_idx in range(len(doc)):
        page = doc[p_idx]
        items = verified_data.get(p_idx, [])
        
        for item in items:
            # 特殊处理 Title 和 Author
            if item['type'] == 'Title':
                txt = page.get_text("text", clip=item['rect']).strip().replace('\n', ' ')
                meta_info_blocks.append(f"[[META_TITLE: {txt}]]")
                continue
            if item['type'] == 'Author':
                txt = page.get_text("text", clip=item['rect']).strip().replace('\n', ' ')
                meta_info_blocks.append(f"[[META_AUTHOR: {txt}]]")
                continue
            
            # 其他正常资源
            if item['type'] in ['ContentArea', 'Mask']: continue
            
            key = f"{item['type']}_{item['id']}" 
            if key not in assets_agg: assets_agg[key] = {'bodies': [], 'captions': [], 'rects': []}
            assets_agg[key]['rects'].append(item['rect']) 
            
            if item['role'] == 'Body':
                pix = page.get_pixmap(clip=item['rect'], matrix=fitz.Matrix(3,3))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                assets_agg[key]['bodies'].append(img)
            elif item['role'] == 'Caption':
                text = page.get_text("text", clip=item['rect']).strip().replace('\n', ' ')
                assets_agg[key]['captions'].append(text)

    # 4. Ref Map
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
            merged_img.save(os.path.join(assets_dir, f"{key}.png"))
            asset_count += 1
        
        full_caption = " ".join(data['captions'])
        final_asset_captions[key] = full_caption
        
        type_str, id_str = key.split('_')
        if type_str == "Figure": ref_map.append(f"Fig. {id_str} -> {key}")
        elif type_str == "Table": ref_map.append(f"Tab. {id_str} -> {key}")
        elif type_str == "Algorithm": ref_map.append(f"Alg. {id_str} -> {key}")
        elif type_str == "Equation": ref_map.append(f"Eq. {id_str} -> {key}")
        ref_map.append(f"{type_str} {id_str} -> {key}")

    ref_map_str = "\n".join(ref_map)

    # 5. 正文提取 (Masking 逻辑生效处)
    print("📝 提取正文文本...")
    raw_paragraph_stream = [] 
    
    # 将元数据放在最前面
    raw_paragraph_stream.extend(meta_info_blocks)
    
    header_pattern = re.compile(r'^(\d+(\.\d+)*\.?|[IVX]+\.|[A-Z]\.)\s+|^(Abstract|References|Introduction|Conclusion|Method)', re.IGNORECASE)

    for p_idx, page in enumerate(doc):
        ignore_rects = []
        page_items = verified_data.get(p_idx, [])
        content_rect = page.rect
        for item in page_items:
            if item['type'] == 'ContentArea': 
                content_rect = item['rect']
            # --- 核心修改：Mask, Title, Author 都作为遮罩，正文不提取 ---
            elif item['type'] in ['Mask', 'Title', 'Author']: 
                ignore_rects.append(item['rect'])
            # --------------------------------------------------------
            else: 
                ignore_rects.append(item['rect']) # 图表等资源区域也不提取

        raw_blocks = page.get_text("blocks", clip=content_rect)
        mid_x = (content_rect.x0 + content_rect.x1) / 2
        left_col, right_col = [], []
        for b in raw_blocks:
            if (b[0] + b[2]) / 2 < mid_x: left_col.append(b)
            else: right_col.append(b)
        left_col.sort(key=lambda b: (b[1], b[0]))
        right_col.sort(key=lambda b: (b[1], b[0]))
        sorted_blocks = left_col + right_col

        for b in sorted_blocks:
            bbox = fitz.Rect(b[:4])
            text = b[4].strip()
            
            is_asset = False
            for ir in ignore_rects:
                if is_box_in_rect(bbox, ir, 0.6): 
                    is_asset = True; break
            
            if not is_asset and text:
                text = re.sub(r'-\n', '', text)
                text = text.replace('\n', ' ')
                
                lines = text.split('\n')
                first_line = lines[0].strip()
                if header_pattern.match(first_line) and len(first_line) < 80:
                    raw_paragraph_stream.append(f"[[HEADER: {first_line}]]")
                    if len(lines) > 1: raw_paragraph_stream.append(" ".join(lines[1:]))
                else:
                    raw_paragraph_stream.append(text)

    # 6. 合并
    merged_text_blocks = smart_merge_paragraphs(raw_paragraph_stream)

    # 7. Metadata
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

    return final_content, txt_path, vis_dir, asset_count

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

# --- 核心 LLM 调用函数 (应用新的切分逻辑) ---
def run_smart_analysis(full_text_path_or_content: str, output_path: str, cache_path: str = None):
    # 【模式配置】
    API_KEY = "ollama" 
    BASE_URL = "http://localhost:11434/v1"
    MODEL_NAME = "qwen2.5:7b" 

    # 1. 读取
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
    
    # 4. 构建任务列表 (带类型标记)
    # 每个 chunk 结构: {"text": str, "type": "meta"|"body"|"asset"}
    raw_chunks = []
    
    if meta_text:
        raw_chunks.append({"text": meta_text, "type": "meta"})
        
    if body_text:
        body_parts = split_text_into_chunks(body_text, MAX_CHUNK_CHARS)
        for part in body_parts:
            raw_chunks.append({"text": part, "type": "body"})
            
    if assets_text:
        raw_chunks.append({"text": assets_text, "type": "asset"})

    # ---------------------------------------------------------
    # 阶段一：任务编排
    # ---------------------------------------------------------
    print(f"📋 [阶段一] 编排任务: 总片段 {len(raw_chunks)} 个 | 策略: 分类型专用提示词")
    
    old_tasks_map = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                old_json = json.load(f)
                for t in old_json.get("tasks", []):
                    old_tasks_map[t["chunk_hash"]] = t
        except: pass

    current_tasks = []
    pending_count = 0
    
    for i, item in enumerate(raw_chunks):
        c_text = item["text"]
        c_type = item["type"]
        h = compute_hash(c_text)
        
        cached_task = old_tasks_map.get(h)
        if cached_task and cached_task.get("status") == "success":
            task_entry = cached_task
            task_entry["id"] = i
            # 兼容旧缓存：如果没有 type 字段，补上
            if "type" not in task_entry: task_entry["type"] = c_type 
            print(f"   🔹 Part {i+1} [{c_type.upper()}]: 命中缓存")
        else:
            task_entry = {
                "id": i,
                "type": c_type,  # 关键：记录任务类型
                "chunk_hash": h,
                "status": "pending",
                "src": c_text, 
                "trans": ""
            }
            pending_count += 1
            
        current_tasks.append(task_entry)

    # 保存 JSON (注意：不再保存全局 system_prompt，因为现在是动态的)
    cache_structure = {
        "model": MODEL_NAME,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": current_tasks,
        "raw_references": raw_refs_text
    }
    
    if cache_path:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(cache_structure, f, ensure_ascii=False, indent=2)
            
    if pending_count == 0:
        print("🎉 所有任务已完成。")
        final_body = "\n".join([t["trans"] for t in current_tasks])
        
        final_refs = ""
        if raw_refs_text:
            final_refs = f"\n<header_block><src>References</src><trans>参考文献 (原文保留)</trans></header_block>\n"
            clean_ref_content = re.sub(r'\[\[HEADER:.*?\]\]', '', raw_refs_text).strip()
            final_refs += f"<ref_block><src>{clean_ref_content}</src></ref_block>"

        with open(output_path, 'w', encoding='utf-8') as f: 
            f.write(final_body + "\n" + final_refs)
        return output_path

    # ---------------------------------------------------------
    # 阶段二：执行推理 (动态 Prompt)
    # ---------------------------------------------------------
    print(f"\n🚀 [阶段二] 开始推理 (剩余 {pending_count} 个)...")
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    
    # 预填充 Prompt Map
    PROMPT_MAP = {
        "meta": SYSTEM_PROMPT_META,
        "body": SYSTEM_PROMPT_BODY.replace("{ref_map_str}", ref_map_str),
        "asset": SYSTEM_PROMPT_ASSET.replace("{ref_map_str}", ref_map_str)
    }
    
    for task in current_tasks:
        if task["status"] == "success": continue
            
        idx = task["id"]
        t_type = task["type"]
        
        # 打印预览
        preview = task["src"][:30].replace('\n', ' ')
        print(f"   ⚡ Part {idx+1}/{len(current_tasks)} [{t_type.upper()}] ...", end="", flush=True)
        
        # --- 关键：根据类型选择 Prompt ---
        current_sys_prompt = PROMPT_MAP.get(t_type, PROMPT_MAP["body"])
        
        messages = [
            {"role": "system", "content": current_sys_prompt},
            {"role": "user", "content": task["src"]} # 不需要再加 Chunk X/Y 的废话，直接发内容
        ]
        
        success = False
        for attempt in range(3):
            try:
                response = client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=messages,
                    temperature=0.1,
                    stream=False
                )
                res_text = response.choices[0].message.content
                
                # 清洗
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
        
        if cache_path:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(cache_structure, f, ensure_ascii=False, indent=2)

    # ---------------------------------------------------------
    # 阶段三：最终合并
    # ---------------------------------------------------------
    final_body = "\n".join([t["trans"] for t in current_tasks if t["status"] == "success"])
    
    final_refs = ""
    if raw_refs_text:
        final_refs = f"\n<header_block><src>References</src><trans>参考文献</trans></header_block>\n"
        clean_ref_content = re.sub(r'\[\[HEADER:.*?\]\]', '', raw_refs_text).strip()
        final_refs += f"<ref_block><src>{clean_ref_content}</src></ref_block>"

    with open(output_path, 'w', encoding='utf-8') as f: 
        f.write(final_body + "\n" + final_refs)
        
    return output_path

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

# --- HTML 生成器 (完整版：含 CSS 美化与引用正则匹配) ---
def generate_html_report(llm_result_path: str, paper_vis_dir: str):
    # 1. 确定路径
    # 优先读取 JSON 缓存，因为包含结构化的 src 和 trans
    cache_path = llm_result_path.replace("_llm_result.txt", "_llm_cache.json")
    
    if not os.path.exists(cache_path):
        # 如果 JSON 不存在，尝试回退到读取 txt (兼容旧逻辑，但推荐用 json)
        print(f"⚠️ 警告：未找到缓存文件 {cache_path}，尝试仅使用文本结果（可能丢失对齐）。")
        return "Error: Cache JSON not found."

    raw_name = os.path.basename(paper_vis_dir)
    html_path = os.path.join(paper_vis_dir, f"{raw_name}_Report.html")
    assets_rel_path = "./assets"

    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
    except Exception as e:
        return f"读取缓存失败: {e}"

    tasks = cache_data.get("tasks", [])
    raw_refs = cache_data.get("raw_references", "")

    # --- 通用正则：匹配参考文献索引 [1], [1,2], [1-5], [10, 12-14] ---
    # 说明：
    # \[        : 左中括号
    # \s* : 允许空格
    # \d+       : 数字
    # (?: ...)* : 非捕获组，匹配后续的 ", 2" 或 "-5" 或 "~8"
    # [\s,\-~]+ : 分隔符
    # \]        : 右中括号
    citation_pattern = r'(\[\s*\d+(?:[\s,\-~]+\d+)*\s*\])'

    # --- 内部渲染函数：处理左侧原文 ---
    def render_src(text):
        if not text: return ""
        
        # 1. 基础 HTML 转义
        html = text \
            .replace("<", "&lt;").replace(">", "&gt;") \
            .replace("\n", "<br>")
        
        # 2. 高亮 Header 和 Meta
        html = re.sub(r'\[\[HEADER:\s*(.*?)\]\]', r'<div class="tag-header">\1</div>', html)
        html = re.sub(r'\[\[META_(.*?):\s*(.*?)\]\]', r'<div class="tag-meta">[\1] \2</div>', html)
        
        # 3. 处理资源占位符与说明
        html = re.sub(r'\[\[ASSET_PLACEHOLDER:\s*(.*?)\]\]', 
                      fr'<div class="tag-asset">[资源占位: \1]</div><img src="{assets_rel_path}/\1.png" class="mini-img">', html)
        
        def asset_cap_sub(m):
            aid, txt = m.group(1), m.group(2)
            return f'<div class="tag-asset-cap">[资源说明: {aid}]</div><div class="src-cap">{txt}</div><img src="{assets_rel_path}/{aid}.png" class="full-img">'
        html = re.sub(r'\[\[ASSET_CAPTION:\s*(.*?)\s*\|\s*(.*?)\]\]', asset_cap_sub, html)
        
        # 4. 【新增】高亮参考文献引用
        html = re.sub(citation_pattern, r'<span class="citation-mark">\1</span>', html)
        
        return html

    # --- 内部渲染函数：处理右侧译文 ---
    def render_trans(text):
        if not text: return "..."
        if "FAILED" in text: return '<span style="color:red;">翻译失败</span>'
        
        # 1. 移除 LLM 可能残留的 markdown
        text = re.sub(r'^```xml', '', text).replace('```', '')
        
        # 2. 解析伪 XML 标签 -> HTML
        text = re.sub(r'<header>(.*?)</header>', r'<h3 class="trans-header">\1</h3>', text, flags=re.DOTALL)
        text = re.sub(r'<meta_title>(.*?)</meta_title>', r'<h1 class="trans-title">\1</h1>', text, flags=re.DOTALL)
        text = re.sub(r'<meta_author>(.*?)</meta_author>', r'<div class="trans-author">\1</div>', text, flags=re.DOTALL)
        text = re.sub(r'<p>(.*?)</p>', r'<p class="trans-p">\1</p>', text, flags=re.DOTALL)
        text = re.sub(r'<asset id=["\'](.*?)["\']>(.*?)</asset>', r'<div class="trans-asset-box"><b>图表 \1:</b> \2</div>', text, flags=re.DOTALL)
        
        # 3. 处理跳转链接 Link
        text = re.sub(r'\[\[LINK:\s*([^\|]+)\|(.*?)\]\]', r'<a href="#\1" class="ref-link">\2</a>', text)
        
        # 4. 【新增】高亮参考文献引用
        text = re.sub(citation_pattern, r'<span class="citation-mark">\1</span>', text)
        
        return text

    # --- 构建主 HTML 内容 ---
    rows_html = ""
    for task in tasks:
        src_html = render_src(task.get('src', ''))
        trans_html = render_trans(task.get('trans', ''))
        
        row_class = "normal-row"
        if "[[HEADER:" in task.get('src', ''): row_class = "header-row-bg"
        
        rows_html += f"""
        <div class="chunk-row {row_class}">
            <div class="col-src">{src_html}</div>
            <div class="col-trans">{trans_html}</div>
        </div>
        """

    # --- 处理参考文献部分 ---
    refs_html = ""
    if raw_refs:
        # 简单的格式化：换行转 <br>，并也应用引用高亮
        clean_refs = raw_refs.replace('\n', '<br>')
        clean_refs = re.sub(r'^(\[\d+\])', r'<b class="ref-id">\1</b>', clean_refs, flags=re.MULTILINE)
        
        refs_html = f"""
        <div class="chunk-row ref-row">
            <div class="col-src">
                <h3 style="color:#2c3e50; border-bottom:2px solid #eee; padding-bottom:10px;">References (Original)</h3>
                <div class="ref-content">{clean_refs}</div>
            </div>
            <div class="col-trans">
                <h3 style="color:#2c3e50; border-bottom:2px solid #eee; padding-bottom:10px;">参考文献</h3>
                <div style="color:#7f8c8d; padding:20px; text-align:center; background:#f9f9f9;">
                    (参考文献通常保留原文以供精确检索，未进行翻译)
                </div>
            </div>
        </div>
        """

    # --- 完整的 HTML 模板 (含 CSS) ---
    html_template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{raw_name} - 双语对照报告</title>
    <style>
        :root {{ --bg: #f4f7f6; --border: #e0e0e0; --primary: #2c3e50; --link-color: #3498db; }}
        body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; background: var(--bg); color: #333; }}
        .container {{ max-width: 96%; margin: 30px auto; background: #fff; box-shadow: 0 4px 20px rgba(0,0,0,0.08); border-radius: 8px; overflow: hidden; }}
        
        /* 布局网格 */
        .chunk-row {{ display: flex; border-bottom: 1px solid var(--border); }}
        .chunk-row:last-child {{ border-bottom: none; }}
        .chunk-row:hover {{ background-color: #fafafa; transition: background 0.2s; }}
        
        .col-src {{ flex: 1; padding: 25px; border-right: 1px solid var(--border); font-family: "Cambria", serif; color: #444; font-size: 15px; line-height: 1.6; overflow-x: auto; background: #fff; }}
        .col-trans {{ flex: 1; padding: 25px; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; color: #111; font-size: 16px; line-height: 1.7; background: #fcfcfc; }}
        
        /* 标题与元数据样式 */
        .header-row-bg {{ background-color: #f0f8ff; }}
        .tag-header {{ font-weight: 800; color: #2980b9; font-size: 1.1em; margin-bottom: 8px; display: inline-block; background: rgba(41,128,185,0.1); padding: 2px 8px; border-radius: 4px; }}
        .tag-meta {{ color: #16a085; font-size: 0.85em; margin-bottom: 4px; font-family: monospace; }}
        
        .trans-header {{ color: #2980b9; margin-top: 0; font-size: 1.4em; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
        .trans-title {{ color: #2c3e50; text-align: center; font-size: 2em; margin: 20px 0; }}
        .trans-author {{ color: #16a085; text-align: center; margin-bottom: 30px; font-weight: bold; font-size: 1.1em; }}
        .trans-p {{ margin-bottom: 15px; text-align: justify; text-justify: inter-ideograph; }}
        
        /* 资源图片样式 */
        .tag-asset {{ background: #f0f0f0; padding: 2px 6px; font-size: 0.8em; color: #888; border-radius: 4px; }}
        .tag-asset-cap {{ background: #fff3cd; color: #856404; padding: 2px 6px; font-size: 0.8em; font-weight: bold; border-radius: 4px; margin-bottom: 5px; display:inline-block; }}
        .src-cap {{ font-style: italic; color: #666; margin-bottom: 10px; }}
        .mini-img {{ max-height: 40px; display: block; margin: 5px 0; opacity: 0.5; border: 1px solid #eee; }}
        .full-img {{ max-width: 98%; border: 1px solid #eee; margin: 10px auto; display: block; box-shadow: 0 2px 5px rgba(0,0,0,0.05); border-radius: 4px; }}
        
        .trans-asset-box {{ background: #fffdf5; padding: 15px; border-left: 4px solid #f1c40f; margin: 15px 0; border-radius: 0 4px 4px 0; font-size: 0.95em; color: #555; }}
        
        /* 链接与引用样式 (核心修改) */
        .ref-link {{ color: var(--link-color); text-decoration: none; background: rgba(52,152,219,0.1); padding: 0 4px; border-radius: 3px; font-weight: 500; }}
        .ref-link:hover {{ text-decoration: underline; background: rgba(52,152,219,0.2); }}
        
        .citation-mark {{ 
            color: #d35400; /* 橙褐色 */
            font-weight: bold;
            font-size: 0.9em;
            background-color: rgba(230, 126, 34, 0.12);
            padding: 0 3px;
            border-radius: 3px;
            cursor: help; /* 鼠标变成问号，提示可关注 */
            margin: 0 1px;
        }}
        .citation-mark:hover {{ 
            background-color: rgba(230, 126, 34, 0.3); 
            color: #c0392b;
        }}
        
        /* 参考文献列表区 */
        .ref-id {{ color: #c0392b; font-weight: bold; margin-right: 5px; }}
        .ref-content {{ font-size: 0.9em; color: #555; line-height: 1.8; }}
        
    </style>
</head>
<body>
    <div class="container">
        {rows_html}
        {refs_html}
    </div>
</body>
</html>"""

    try:
        with open(html_path, 'w', encoding='utf-8') as f: f.write(html_template)
        return html_path
    except Exception as e:
        return f"写入HTML文件失败: {e}"