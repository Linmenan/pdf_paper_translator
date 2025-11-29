import os
import re
import time
import shutil
import platform
import fitz  # PyMuPDF
import json
import hashlib
import traceback # 新增：用于打印详细报错
import tkinter as tk
from tkinter import messagebox, font, ttk
from PIL import Image, ImageTk, ImageOps
from openai import OpenAI
from urllib.parse import urlparse
from functools import partial
import http.server
import socketserver
import webbrowser
import prompts as P  # 引入新模块

# --- 配置常量 ---
MAX_CHUNK_CHARS = 1000
TIMEOUT_MS = 600000
MAX_RETRIES = 3

# ==============================================================================
#  基础辅助工具 (Utils) - 必须最先定义
# ==============================================================================
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

def compute_hash(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def _save_cache(path, model, tasks, refs, layout, ref_map=""):
    if not path: return
    structure = {
        "model": model,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": tasks,
        "raw_references": refs,
        "layout_map": layout,
        "ref_map": ref_map
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(structure, f, ensure_ascii=False, indent=2)

def tag_text_elements(text, ref_map_str):
    """简单处理：将 Fig. 1 等转换为 [[LINK:Figure_1|Fig. 1]]"""
    return text

def split_long_buffer_safely(text, max_len):
    if len(text) <= max_len:
        return [text]
    
    protect_pattern = (
        r'(?<!\bFig\.)(?<!\bFigs\.)'
        r'(?<!\bEq\.)(?<!\bEqs\.)'
        r'(?<!\bTab\.)(?<!\bTabs\.)'
        r'(?<!\bRef\.)(?<!\bRefs\.)'
        r'(?<!\bVol\.)(?<!\bno\.)'
        r'(?<!\bal\.)(?<!\bvs\.)'
        r'(?<!\bi\.e\.)(?<!\be\.g\.)'
    )
    split_marker = r'(?<=[.?!;])\s+(?=[A-Z0-9\[])'
    final_pattern = protect_pattern + split_marker

    try:
        sentences = re.split(final_pattern, text, flags=re.IGNORECASE)
    except re.error:
        print("⚠️ [Warning] Regex lookbehind failed, using simple split.")
        sentences = re.split(r'(?<=[.?!;])\s+(?=[A-Z0-9])', text)
    
    chunks = []
    current_chunk = ""
    for sentence in sentences:
        if len(current_chunk) + len(sentence) > max_len and current_chunk:
            chunks.append(current_chunk)
            current_chunk = sentence
        else:
            if current_chunk: current_chunk += " " + sentence
            else: current_chunk = sentence
    if current_chunk: chunks.append(current_chunk)
    return chunks

def smart_merge_paragraphs(blocks, max_split_len=500):
    if not blocks: return []
    merged = []
    buffer = ""
    terminals = ('.', '?', '!', ':', ';', '。', '？', '！', '：', '；')
    hard_boundary_pattern = re.compile(r'^\[\[(HEADER|ASSET_|META_).*?\]\]')
    hanging_abbrev_pattern = re.compile(
        r'(Fig|Figure|Eq|Equation|Tab|Table|Ref|Reference|Sec|Section)\.?\s*$', 
        re.IGNORECASE
    )

    print(f"🔍 [DEBUG] 开始处理 {len(blocks)} 个文本块...")
    for i, block in enumerate(blocks):
        block = block.strip()
        if not block: continue
        
        if hard_boundary_pattern.match(block):
            if buffer:
                merged.extend(split_long_buffer_safely(buffer, max_split_len))
                buffer = ""
            merged.append(block)
            continue
        
        if not buffer:
            buffer = block
            continue
            
        prev_end_char = buffer[-1] if buffer else ""
        tail = buffer[-20:]
        
        if prev_end_char == '-':
            buffer = buffer[:-1] + block
        elif hanging_abbrev_pattern.search(buffer):
            buffer = buffer + " " + block
        elif (not buffer.endswith(terminals)) or (block[0].islower()):
            buffer = buffer + " " + block
        else:
            merged.extend(split_long_buffer_safely(buffer, max_split_len))
            buffer = block 

    if buffer:
        merged.extend(split_long_buffer_safely(buffer, max_split_len))
    return merged

def split_content_smart(text):
    # 1. Assets
    asset_marker = "--- ASSETS METADATA ---"
    assets_part = ""
    content_remaining = text
    if asset_marker in text:
        parts = text.rsplit(asset_marker, 1)
        if len(parts) == 2:
            content_remaining = parts[0]
            assets_part = asset_marker + parts[1]

    # 2. References
    ref_pattern = re.compile(r'(\[\[HEADER:\s*References?.*?\]\])', re.IGNORECASE)
    split_parts = ref_pattern.split(content_remaining, maxsplit=1)
    
    body_with_meta = ""
    ref_part = ""
    if len(split_parts) >= 3:
        body_with_meta = split_parts[0].strip()
        ref_part = split_parts[1] + split_parts[2]
    else:
        body_with_meta = content_remaining.strip()

    # 3. Meta
    meta_pattern = re.compile(r'(\[\[META_.*?:.*?\]\]\s*)+')
    meta_match = meta_pattern.match(body_with_meta)
    
    meta_part = ""
    body_part = body_with_meta
    if meta_match:
        meta_part = meta_match.group(0).strip()
        body_part = body_with_meta[meta_match.end():].strip()

    return meta_part, body_part, assets_part, ref_part

def split_text_into_chunks_with_layout(text, max_chars):
    header_pattern = re.compile(r'(\[\[HEADER:.*?\]\])', re.IGNORECASE)
    insert_pattern = re.compile(r'\[\[ASSET_INSERT:\s*(.*?)\]\]')
    
    segments = header_pattern.split(text)
    final_chunks = []
    layout_map = {}
    current_chunk_idx = 0
    
    for seg in segments:
        seg = seg.strip()
        if not seg: continue
        
        found_inserts = insert_pattern.findall(seg)
        clean_seg = insert_pattern.sub('', seg).strip()
        
        if not clean_seg and not found_inserts: continue 
        
        if header_pattern.match(seg):
             final_chunks.append(clean_seg)
             if found_inserts:
                 if current_chunk_idx not in layout_map: layout_map[current_chunk_idx] = []
                 layout_map[current_chunk_idx].extend(found_inserts)
             current_chunk_idx += 1
        else:
            paragraphs = clean_seg.split('\n\n')
            buffer = []
            buffer_len = 0
            
            if not clean_seg and found_inserts:
                if current_chunk_idx not in layout_map: layout_map[current_chunk_idx] = []
                layout_map[current_chunk_idx].extend(found_inserts)
                continue

            for p in paragraphs:
                p = p.strip()
                if not p: continue
                if buffer_len + len(p) > max_chars and buffer:
                    final_chunks.append("\n\n".join(buffer))
                    if found_inserts:
                         if current_chunk_idx not in layout_map: layout_map[current_chunk_idx] = []
                         layout_map[current_chunk_idx].extend(found_inserts)
                         found_inserts = [] 
                    current_chunk_idx += 1
                    buffer = []
                    buffer_len = 0
                buffer.append(p)
                buffer_len += len(p)
            
            if buffer:
                final_chunks.append("\n\n".join(buffer))
                if found_inserts:
                     if current_chunk_idx not in layout_map: layout_map[current_chunk_idx] = []
                     layout_map[current_chunk_idx].extend(found_inserts)
                current_chunk_idx += 1
                
    return final_chunks, layout_map

# ==============================================================================
# 核心逻辑：构建任务 (Build Tasks)
# ==============================================================================
def build_initial_tasks(content):
    """
    根据文本内容，执行四段式切分并构建初始任务列表。
    """
    # 1. 预处理：分离 RefMap
    ref_map_str = ""
    body_text = content
    map_match = re.search(r'\[\[REF_MAP_START\]\]\n(.*?)\n\[\[REF_MAP_END\]\]', content, re.DOTALL)
    if map_match:
        ref_map_str = map_match.group(1)
        body_text = content.replace(map_match.group(0), "").strip()
    
    # 2. 四段式切分
    meta_text, body_text, assets_text, raw_refs_text = split_content_smart(body_text)
    
    # 3. 构建任务列表
    raw_chunks = []
    layout_map_global = {} 
    
    # A. Meta
    if meta_text: 
        raw_chunks.append({"text": meta_text, "type": "meta"})
        
    # B. Body
    if body_text:
        body_parts, local_layout_map = split_text_into_chunks_with_layout(body_text, MAX_CHUNK_CHARS)
        offset = len(raw_chunks) 
        
        for idx, part in enumerate(body_parts):
            tagged_part = tag_text_elements(part, ref_map_str)
            raw_chunks.append({"text": tagged_part, "type": "body"})
            if idx in local_layout_map:
                layout_map_global[idx + offset] = local_layout_map[idx]
            
    # C. Assets
    if assets_text: 
        raw_chunks.append({"text": assets_text, "type": "asset"})

    # 4. 封装为 Task 对象
    current_tasks = []
    for i, item in enumerate(raw_chunks):
        c_text = item["text"]
        c_type = item["type"]
        h = compute_hash(c_text)
        
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
        
    return current_tasks, raw_refs_text, layout_map_global

# ==============================================================================
# 主流程类与函数 (LayoutEditor, Extract, Analysis)
# ==============================================================================
class LayoutEditor:
    def __init__(self, doc, initial_data):
        self.doc = doc
        self.data = initial_data 
        self.page_count = len(doc)
        self.current_page = 0
        
        self.root = tk.Tk()
        self.ui_font = get_optimal_font(self.root)
        self.root.title(f"PDF 结构化校对")
        
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
            "Figure": "#e74c3c",    "Table": "#3498db",     "Equation": "#27ae60",
            "Algorithm": "#9b59b6", "Title": "#d35400",     "Author": "#1abc9c",
            "Mask": "#7f8c8d",      "Header": "#8e44ad",    "ContentArea": "#f1c40f"
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
        
        type_frame = tk.LabelFrame(self.sidebar_frame, text="1. 元素类型", font=f_bold, bg="#f0f0f0")
        type_frame.pack(fill=tk.X, padx=p, pady=5)
        
        types_group1 = [("图 (Figure) - [1]", "Figure"), ("表 (Table) - [2]", "Table"), ("式 (Equation) - [3]", "Equation"), ("算 (Algorithm) - [4]", "Algorithm")]
        types_group2 = [("标题 (Title) - [5]", "Title"), ("作者 (Author) - [6]", "Author"), ("遮罩 (Mask) - [7]", "Mask"), ("章节 (Header) - [8]", "Header")]
        
        for text, val in types_group1:
            tk.Radiobutton(type_frame, text=text, variable=self.current_tool_type, value=val, command=self.update_id_suggestion, font=f_norm, bg="#f0f0f0", anchor="w").pack(fill=tk.X, padx=5)
        ttk.Separator(type_frame, orient='horizontal').pack(fill='x', padx=5, pady=5)
        for text, val in types_group2:
            tk.Radiobutton(type_frame, text=text, variable=self.current_tool_type, value=val, command=self.update_id_suggestion, font=f_norm, bg="#f0f0f0", anchor="w").pack(fill=tk.X, padx=5)
        ttk.Separator(type_frame, orient='horizontal').pack(fill='x', padx=5, pady=5)
        tk.Radiobutton(type_frame, text="正文范围 - [0]", variable=self.current_tool_type, value="ContentArea", command=self.update_id_suggestion, font=f_norm, bg="#f0f0f0", anchor="w").pack(fill=tk.X, padx=5)

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

        list_frame = tk.LabelFrame(self.sidebar_frame, text="当前页列表 (Del删除)", font=f_bold, bg="#f0f0f0")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=p, pady=5)
        self.item_listbox = tk.Listbox(list_frame, bg="white", height=10, font=(self.ui_font, 11))
        self.item_listbox.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.item_listbox.bind("<Delete>", self.delete_selected_list_item)

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
        stipple = 'gray50' if item['type'] == 'Mask' else ''
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
        new_item = {'rect': pdf_rect, 'type': self.current_tool_type.get(), 'id': self.current_id.get(), 'role': self.current_role.get()}
        if self.current_page not in self.data: self.data[self.current_page] = []
        if new_item['type'] == 'ContentArea':
             self.data[self.current_page] = [x for x in self.data[self.current_page] if x['type'] != 'ContentArea']
        self.data[self.current_page].append(new_item)
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
        curr_content = next((x for x in self.data.get(self.current_page, []) if x['type'] == 'ContentArea'), None)
        next_idx = self.current_page + 1
        if next_idx < self.page_count:
            if next_idx not in self.data: self.data[next_idx] = []
            next_has_content = any(x['type'] == 'ContentArea' for x in self.data[next_idx])
            if not next_has_content and curr_content:
                if self.doc[self.current_page].rect == self.doc[next_idx].rect:
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

def extract_text_and_save_assets_smart(pdf_path: str, raw_text_dir: str, vis_output_root: str, skip_ui: bool = False) -> tuple[str, str, str, int]:
    print(f"🔍 [Debug] 开始提取流程: PDF={pdf_path}", flush=True)
    
    if not os.path.exists(pdf_path): 
        raise FileNotFoundError(f"PDF missing: {pdf_path}")
    
    clean_name = sanitize_filename(pdf_path)
    os.makedirs(raw_text_dir, exist_ok=True)
    txt_path = os.path.join(raw_text_dir, f"{clean_name}_context.txt")
    
    extracted_assets_dir = os.path.join(raw_text_dir, clean_name, "assets")
    layout_config_path = os.path.join(raw_text_dir, clean_name, "layout_config.json")
    
    if not os.path.exists(os.path.dirname(layout_config_path)):
        os.makedirs(os.path.dirname(layout_config_path), exist_ok=True)

    doc = fitz.open(pdf_path)
    init_data = {}
    saved_json = {}

    # 1. 加载或初始化布局数据
    if os.path.exists(layout_config_path):
        print(f"📂 [Debug] 加载历史布局: {layout_config_path}", flush=True)
        try:
            with open(layout_config_path, 'r', encoding='utf-8') as f:
                saved_json = json.load(f)
        except Exception as e:
            print(f"⚠️ 加载历史记录失败 ({e})，将忽略。", flush=True)
            saved_json = {}
    
    for i, page in enumerate(doc):
        w, h = page.rect.width, page.rect.height
        page_items = []
        if str(i) in saved_json:
            raw_items = saved_json[str(i)]
            for item in raw_items:
                r = item['rect']
                page_items.append({'rect': fitz.Rect(r[0], r[1], r[2], r[3]), 'type': item['type'], 'id': item['id'], 'role': item['role']})
        
        has_content_area = any(x['type'] == 'ContentArea' for x in page_items)
        if not has_content_area:
            default_rect = fitz.Rect(0, h*0.08, w, h*0.92)
            page_items.insert(0, {'rect': default_rect, 'type': 'ContentArea', 'id': 0, 'role': 'Body'})
        init_data[i] = page_items

    # 2. 启动编辑器或读取 Web 数据
    if not skip_ui:
        editor = LayoutEditor(doc, init_data)
        verified_data = editor.data
    else:
        if os.path.exists(layout_config_path):
            print("🚀 [Debug] Web模式: 读取已保存布局配置...", flush=True)
            with open(layout_config_path, 'r', encoding='utf-8') as f:
                web_saved_data = json.load(f)
                verified_data = {}
                for p_str, items in web_saved_data.items():
                    verified_data[int(p_str)] = []
                    for item in items:
                        r = item['rect']
                        verified_data[int(p_str)].append({'rect': fitz.Rect(r[0], r[1], r[2], r[3]), 'type': item['type'], 'id': item['id'], 'role': item['role']})
        else:
             print("⚠️ [Debug] Web模式: 未找到布局文件，使用默认推断数据。", flush=True)
             verified_data = init_data

    # 3. 保存布局配置
    serializable_data = {}
    for page_idx, items in verified_data.items():
        serializable_data[page_idx] = []
        for item in items:
            r = item['rect']
            serializable_data[page_idx].append({'rect': [r.x0, r.y0, r.x1, r.y1], 'type': item['type'], 'id': item['id'], 'role': item['role']})
    
    with open(layout_config_path, 'w', encoding='utf-8') as f:
        json.dump(serializable_data, f, indent=2)
    print(f"💾 [Debug] 布局已保存至: {layout_config_path}", flush=True)

    # 4. 资源提取 (截图)
    if os.path.exists(extracted_assets_dir): shutil.rmtree(extracted_assets_dir)
    os.makedirs(extracted_assets_dir, exist_ok=True)

    print(f"🧩 [Debug] 正在截图资源...", flush=True)
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
            if item['type'] in ['ContentArea', 'Mask', 'Header']: continue
            
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

    # 5. 文本提取与重组
    print("📝 [Debug] 提取正文文本...", flush=True)
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
            elif item['type'] == 'Header':
                ignore_rects.append(item['rect'])
                header_text = page.get_text("text", clip=item['rect']).strip().replace('\n', ' ')
                page_asset_inserts.append({"rect": item['rect'], "text": f"[[HEADER: {header_text}]]", "id": f"Header_{item['id']}" })
            else:
                ignore_rects.append(item['rect'])
                key = f"{item['type']}_{item['id']}"
                page_asset_inserts.append({"rect": item['rect'], "text": f"[[ASSET_INSERT: {key}]]", "id": key})

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
                mixed_blocks.append({"type": "text", "y_sort": bbox.y0 + (0 if bbox.x0 < mid_x else 10000), "text": text})

        for ins in sorted_inserts:
            bbox = ins['rect']
            mixed_blocks.append({"type": "asset_tag", "y_sort": bbox.y0 + (0 if bbox.x0 < mid_x else 10000), "text": ins['text']})
            
        mixed_blocks.sort(key=lambda x: x['y_sort'])

        for b in mixed_blocks:
            text = b['text']
            if b['type'] == "text":
                text = re.sub(r'-\n', '', text)
                text = text.replace('\n', ' ')
                raw_paragraph_stream.append(text)
            else:
                raw_paragraph_stream.append(text)

    merged_text_blocks = smart_merge_paragraphs(raw_paragraph_stream)
    print("📝 [Debug] 智能分段结束...", flush=True)

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
    
    # ----------------------------------------------------------------------
    #  自动生成 Cache JSON (增加大量日志)
    # ----------------------------------------------------------------------
    print("⚡ [Auto-Init] 正在生成初始任务列表...", flush=True)
    try:
        # 1. 路径调试
        print(f"   [Debug] 原始 raw_text_dir: {raw_text_dir}", flush=True)
        raw_text_dir_clean = os.path.normpath(raw_text_dir)
        abs_raw_dir = os.path.abspath(raw_text_dir_clean)
        print(f"   [Debug] 绝对路径 abs_raw_dir: {abs_raw_dir}", flush=True)
        
        root_dir = os.path.dirname(abs_raw_dir)
        print(f"   [Debug] 推算的 root_dir: {root_dir}", flush=True)
        
        llm_dir = os.path.join(root_dir, "llm_output")
        print(f"   [Debug] 目标 llm_dir: {llm_dir}", flush=True)
        
        if not os.path.exists(llm_dir):
            print("   [Debug] llm_dir 不存在，正在创建...", flush=True)
            os.makedirs(llm_dir, exist_ok=True)
        
        cache_path = os.path.join(llm_dir, f"{clean_name}_llm_cache.json")
        print(f"   [Debug] 最终 cache_path: {cache_path}", flush=True)
        
        # 2. 任务构建调试
        print("   [Debug] 正在调用 build_initial_tasks...", flush=True)
        full_content_with_map = f"[[REF_MAP_START]]\n{ref_map_str}\n[[REF_MAP_END]]\n\n{final_content}"
        
        tasks, refs, layout = build_initial_tasks(full_content_with_map)
        print(f"   [Debug] build_initial_tasks 返回任务数: {len(tasks)}", flush=True)
        
        # 3. 保存
        _save_cache(cache_path, "init", tasks, refs, layout)
        
        # 4. 双重确认
        if os.path.exists(cache_path):
            print(f"✅ [Success] 初始任务文件已生成: {cache_path}", flush=True)
        else:
            print(f"❌ [Error] 文件保存函数执行完毕，但文件依然不存在!", flush=True)
        # 5. 保存 (传入 ref_map_str)
        _save_cache(cache_path, "init", tasks, refs, layout, ref_map=ref_map_str) # <--- 修改这里
    except Exception as e:
        print(f"⚠️ [Fatal Error] 无法自动生成任务缓存: {e}", flush=True)
        traceback.print_exc()
    # ----------------------------------------------------------------------

    return final_content, txt_path, vis_final_dir, asset_count

def run_smart_analysis(full_text_path_or_content: str, output_path: str, cache_path: str = None):
    API_KEY = "ollama" 
    BASE_URL = "http://localhost:11434/v1"
    MODEL_NORMAL = "qwen2.5:7b" 
    MODEL_STRONG = "qwen2.5:14b" 

    if os.path.isfile(full_text_path_or_content):
         with open(full_text_path_or_content, 'r', encoding='utf-8') as f: content = f.read()
    else:
        content = full_text_path_or_content

    # [修改] 直接复用 build_initial_tasks
    raw_tasks, raw_refs_text, layout_map_global = build_initial_tasks(content)
    print(f"📋 [阶段一] 任务编排: 总片段 {len(raw_tasks)} 个")
    
    old_tasks_map = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                old_json = json.load(f)
                for t in old_json.get("tasks", []):
                    old_tasks_map[t["chunk_hash"]] = t
        except: pass

    current_tasks = []
    for newTask in raw_tasks:
        h = newTask["chunk_hash"]
        cached_task = old_tasks_map.get(h)
        if cached_task:
            task_entry = cached_task
            task_entry["id"] = newTask["id"] 
            if task_entry.get("status") == "failed": task_entry["status"] = "pending"
            if "user_hint" not in task_entry: task_entry["user_hint"] = ""
            if "old_trans" not in task_entry: task_entry["old_trans"] = ""
        else:
            task_entry = newTask
        current_tasks.append(task_entry)

    pending_tasks = [t for t in current_tasks if t["status"] == "pending"]
    
    if pending_tasks:
        print(f"\n🚀 [阶段二] 开始推理 (待处理: {len(pending_tasks)})...")
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        PROMPT_MAP = {
            "meta": P.SYSTEM_PROMPT_META,
            "body": P.SYSTEM_PROMPT_BODY, 
            "asset": P.SYSTEM_PROMPT_ASSET.replace("{ref_map_str}", raw_refs_text) 
        }
        
        for task in current_tasks:
            if task["status"] != "pending": continue
            idx = task["id"]
            t_type = task["type"]
            user_hint = task.get("user_hint", "").strip()
            old_trans = task.get("old_trans", "").strip()
            is_correction_mode = bool(user_hint and old_trans)
            current_model = MODEL_STRONG if is_correction_mode else MODEL_NORMAL
            
            if is_correction_mode:
                print(f"   🔥 Part {idx+1} [纠错模式 -> {current_model}] ...", end="", flush=True)
                sys_prompt = P.SYSTEM_PROMPT_CORRECTION
                user_content = (f"【原文】:\n{task['src']}\n\n【旧译文(有误)】:\n{old_trans}\n\n【用户指引(最高优先级)】:\n{user_hint}")
            else:
                print(f"   ⚡ Part {idx+1} [普通翻译 -> {current_model}] ...", end="", flush=True)
                sys_prompt = PROMPT_MAP.get(t_type, PROMPT_MAP["body"])
                user_content = task['src']

            messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}]
            success = False
            for attempt in range(3):
                try:
                    response = client.chat.completions.create(model=current_model, messages=messages, temperature=0.1, stream=False)
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
            if cache_path: 
                _save_cache(cache_path, MODEL_NORMAL, current_tasks, raw_refs_text, layout_map_global)
    else:
        print("\n🎉 所有任务已完成，无需新增推理。")

    print("💾 [阶段三] 刷新结果文件...")
    final_body = "\n".join([t["trans"] for t in current_tasks if t["status"] == "success"])
    final_refs = ""
    if raw_refs_text:
        clean_ref_content = re.sub(r'\[\[HEADER:.*?\]\]', '', raw_refs_text).strip()
        final_refs = f"\n<header_block><src>References</src><trans>参考文献</trans></header_block>\n<ref_block><src>{clean_ref_content}</src></ref_block>"

    with open(output_path, 'w', encoding='utf-8') as f: 
        f.write(final_body + "\n" + final_refs)
        
    if cache_path: 
            # 这里如果是 run_analysis，ref_map 可以在上面通过 build_initial_tasks 解析出来，或者简单传空(不影响显示)
            # 为了严谨，建议保持 data 完整性，但如果暂时不想改动太大，这里可以传 ""，因为提取阶段已经存过了
            _save_cache(cache_path, MODEL_NORMAL, current_tasks, raw_refs_text, layout_map_global, ref_map=raw_refs_text) # 暂用 raw_refs_text 或传空
    return output_path

def generate_html_report(llm_result_path: str, paper_vis_dir: str):
    cache_path = llm_result_path.replace("_llm_result.txt", "_llm_cache.json")
    if not os.path.exists(cache_path): return "Error: 找不到缓存文件。"
    
    raw_name = os.path.basename(paper_vis_dir)
    html_path = os.path.join(paper_vis_dir, f"{raw_name}_Report.html")
    vis_assets_dest = os.path.join(paper_vis_dir, "assets")
    if not os.path.exists(vis_assets_dest): os.makedirs(vis_assets_dest, exist_ok=True)
    root_dir = os.path.dirname(os.path.dirname(paper_vis_dir)) 
    extracted_assets_src = os.path.join(root_dir, "extracted_output", raw_name, "assets")
    
    try:
        with open(cache_path, 'r', encoding='utf-8') as f: cache_data = json.load(f)
    except Exception as e: return f"JSON 读取失败: {e}"

    tasks = cache_data.get("tasks", [])
    raw_refs = cache_data.get("raw_references", "")
    layout_map = cache_data.get("layout_map", {})

    meta_task = None; asset_task = None; body_tasks = []
    for t in tasks:
        if t['type'] == 'meta': meta_task = t
        elif t['type'] == 'asset': asset_task = t
        else: body_tasks.append(t)

    assets_map = {}
    def copy_asset_image(asset_id):
        filename = f"{asset_id}.png"
        src_file = os.path.join(extracted_assets_src, filename)
        dst_file = os.path.join(vis_assets_dest, filename)
        if os.path.exists(src_file): shutil.copy2(src_file, dst_file)
        return f"./assets/{filename}"

    if asset_task:
        src_full = asset_task.get('src', '')
        trans_full = asset_task.get('trans', '')
        src_iter = re.finditer(r'\[\[ASSET_CAPTION:\s*(.*?)\s*\|\s*(.*?)\]\]', src_full, re.DOTALL)
        for m in src_iter:
            aid = m.group(1).strip()
            src_txt = m.group(2).strip()
            trans_match = re.search(fr'<asset id=["\']?{re.escape(aid)}["\']?>(.*?)</asset>', trans_full, re.DOTALL)
            trans_txt = trans_match.group(1).strip() if trans_match else "(未找到译文)"
            rel_path = copy_asset_image(aid)
            assets_map[aid] = { "id": aid, "type": "captioned", "src": src_txt, "trans": trans_txt, "path": rel_path }
        ph_iter = re.finditer(r'\[\[ASSET_PLACEHOLDER:\s*(.*?)\]\]', src_full)
        for m in ph_iter:
            aid = m.group(1).strip()
            if aid not in assets_map:
                rel_path = copy_asset_image(aid)
                assets_map[aid] = { "id": aid, "type": "placeholder", "src": "", "trans": "", "path": rel_path }

    def clean_xml_and_headers(text):
        if not text: return ""
        text = re.sub(r'^```xml', '', text).replace('```', '')
        text = re.sub(r'<header>(.*?)</header>', r'<b>\1</b>', text)
        text = text.replace('<p>', '').replace('</p>', '<br>')
        text = re.sub(r'\[\[HEADER:\s*(.*?)\]\]', r'\1', text)
        return text
    
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
        task_id = task['id']
        existing_hint = task.get("user_hint", "")
        hint_class = "has-hint" if existing_hint else ""
        status_text = f"(状态: {task.get('status')})" if existing_hint else ""
        layout_assets = layout_map.get(str(task_id), [])
        for aid in layout_assets:
            if aid in assets_map and aid not in placed_assets:
                html_body += render_asset_html(aid, assets_map[aid])
                placed_assets.add(aid)
        src_txt = task.get('src', '')
        trans_txt = clean_xml_and_headers(task.get('trans', ''))
        html_body += f"""<div class="row-container" id="task-{task_id}"><div class="row text-row {hint_class}"><div class="col-src">{src_txt}</div><div class="col-trans">{trans_txt}<div class="hint-badge" style="display: {'block' if existing_hint else 'none'}">💡 上次提示: {existing_hint} {status_text}</div></div></div><div class="feedback-panel" style="display: none;"><div class="feedback-header">🛠️ 人工纠错向导 (Task {task_id})</div><textarea class="feedback-input" placeholder="请输入给 AI 的翻译提示...">{existing_hint}</textarea><div style="margin-top:5px;"><button class="btn btn-primary" style="font-size:0.8em; padding:4px 10px;" onclick="saveFeedback('{task_id}', this)">确认修改并标记</button><span class="status-saved">✅ 保存成功</span></div></div></div>"""

    html_refs = ""
    if raw_refs:
        refs_content = re.sub(r'\[\[HEADER:.*?\]\]', '', raw_refs).strip()
        html_refs = f'<div class="ref-section"><pre>{refs_content}</pre></div>'

    full_html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>{raw_name} - Interactive Mode</title><style>:root {{ --primary: #2c3e50; --accent: #3498db; --bg: #f8f9fa; --border: #e0e0e0; }}body {{ font-family: "Segoe UI", sans-serif; margin: 0; background: var(--bg); padding-bottom: 100px; }}.container {{ max-width: 1200px; margin: 0 auto; background: #fff; box-shadow: 0 0 20px rgba(0,0,0,0.05); }}.meta-section {{ padding: 40px; text-align: center; background: #fff; }}.meta-title-en {{ font-size: 1.8em; color: #2c3e50; font-weight: 700; }}.meta-title-zh {{ font-size: 1.6em; color: #34495e; font-weight: 400; }}.meta-author-en {{ font-style: italic; color: #7f8c8d; }}.meta-author-zh {{ color: #16a085; font-weight: bold; }}.toolbar {{ position: fixed; top: 20px; right: 20px; background: #fff; padding: 10px 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border-radius: 8px; z-index: 999; display: flex; gap: 10px; align-items: center; }}.btn {{ padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; transition: 0.2s; }}.btn-primary {{ background: var(--accent); color: #fff; }}.btn-danger {{ background: #e74c3c; color: #fff; }}.btn-success {{ background: #27ae60; color: #fff; }}.btn:disabled {{ background: #ccc; cursor: not-allowed; }}#loading-mask {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(255,255,255,0.8); z-index: 2000; display: none; justify-content: center; align-items: center; flex-direction: column; }}.spinner {{ width: 40px; height: 40px; border: 4px solid #f3f3f3; border-top: 4px solid var(--accent); border-radius: 50%; animation: spin 1s linear infinite; }}@keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}.row-container {{ border-bottom: 1px solid var(--border); }}.row {{ display: flex; }}.col-src, .col-trans {{ flex: 1; padding: 20px; }}.col-src {{ border-right: 1px solid var(--border); color: #555; background: #fff; }}body.feedback-mode .row:hover {{ background: #fdfdfd; }}body.feedback-mode .col-trans {{ cursor: pointer; outline: 1px dashed #ccc; }}.feedback-panel {{ background: #f1f8ff; padding: 15px 20px; border-top: 1px solid #d6eaf8; display: none; }}.feedback-header {{ font-weight: bold; color: #2c3e50; margin-bottom: 5px; font-size: 0.9em; }}.feedback-input {{ width: 100%; height: 60px; padding: 8px; border: 1px solid #bdc3c7; border-radius: 4px; font-family: inherit; margin-bottom: 5px; }}.hint-badge {{ margin-top: 10px; padding: 5px 10px; background: #fff3cd; border: 1px solid #ffeeba; color: #856404; font-size: 0.85em; border-radius: 4px; }}.status-saved {{ color: #27ae60; font-weight: bold; margin-left: 10px; display: none; }}.asset-row {{ background: #f4f4f4; padding: 20px; display: block; }}.asset-card {{ background: #fff; max-width: 90%; margin: 0 auto; border-radius: 8px; padding: 10px; text-align: center; }}.asset-img {{ max-width: 100%; }}</style></head><body><div id="loading-mask"><div class="spinner"></div><div style="margin-top: 15px; font-size: 1.2em; color: #555;">正在后台重译并生成报告，请稍候...</div></div><div class="toolbar"><div id="status-text" style="margin-right: 10px; color: #666;">浏览模式</div><button class="btn btn-primary" id="toggle-btn" onclick="toggleFeedbackMode()">进入纠错模式</button><button class="btn btn-success" id="run-btn" onclick="triggerRerun()" style="display:none;">🚀 应用修改并重译</button></div><div class="container">{html_meta}<div class="main-content">{html_body}</div>{html_refs}</div><script>const API_BASE = "";let isFeedbackMode = false;function toggleFeedbackMode() {{ isFeedbackMode = !isFeedbackMode; document.body.classList.toggle('feedback-mode'); const toggleBtn = document.getElementById('toggle-btn'); const runBtn = document.getElementById('run-btn'); const statusText = document.getElementById('status-text'); if (isFeedbackMode) {{ toggleBtn.textContent = "退出纠错模式"; toggleBtn.classList.replace('btn-primary', 'btn-danger'); runBtn.style.display = 'block'; statusText.textContent = "✏️ 点击译文修改，自动保存"; enableClickHandlers(); }} else {{ toggleBtn.textContent = "进入纠错模式"; toggleBtn.classList.replace('btn-danger', 'btn-primary'); runBtn.style.display = 'none'; statusText.textContent = "浏览模式"; disableClickHandlers(); }} }}function enableClickHandlers() {{ const rows = document.querySelectorAll('.row-container'); rows.forEach(row => {{ const transCol = row.querySelector('.col-trans'); if (transCol.getAttribute('data-bound')) return; transCol.setAttribute('data-bound', 'true'); transCol.onclick = () => {{ if (!isFeedbackMode) return; const panel = row.querySelector('.feedback-panel'); const isHidden = (panel.style.display === 'none' || panel.style.display === ''); panel.style.display = isHidden ? 'block' : 'none'; }}; }}); }}function disableClickHandlers() {{ const panels = document.querySelectorAll('.feedback-panel'); panels.forEach(p => p.style.display = 'none'); }}async function saveFeedback(taskId, btnElement) {{ const container = document.getElementById('task-' + taskId); const input = container.querySelector('.feedback-input'); const hint = input.value.trim(); const statusMsg = container.querySelector('.status-saved'); if (!hint) {{ alert("请输入提示"); return; }} const originalText = btnElement.textContent; btnElement.disabled = true; btnElement.textContent = "保存中..."; try {{ const response = await fetch(API_BASE + '/update_task', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ id: taskId, hint: hint }}) }}); const data = await response.json(); if (data.status === 'success') {{ statusMsg.style.display = 'inline'; setTimeout(() => statusMsg.style.display = 'none', 2000); btnElement.textContent = "已保存 (待重译)"; }} else {{ alert("保存失败: " + data.msg); btnElement.textContent = originalText; btnElement.disabled = false; }} }} catch (err) {{ alert("连接错误: " + err); btnElement.textContent = originalText; btnElement.disabled = false; }} }}async function triggerRerun() {{ if (!confirm("确定要重译吗？")) return; const mask = document.getElementById('loading-mask'); mask.style.display = 'flex'; try {{ const response = await fetch(API_BASE + '/trigger_rerun', {{ method: 'POST' }}); const data = await response.json(); if (data.status === 'success') {{ alert(data.msg); location.reload(); }} else {{ alert("失败: " + data.msg); mask.style.display = 'none'; }} }} catch (err) {{ alert("错误: " + err); mask.style.display = 'none'; }} }}</script></body></html>"""

    try:
        with open(html_path, 'w', encoding='utf-8') as f: f.write(full_html)
        return html_path
    except Exception as e: return f"HTML 写入失败: {e}"

def render_asset_html(mid, asset):
    if asset["type"] == "placeholder":
        return f"""<div class="row asset-row" id="{mid}"><div class="asset-card placeholder-card"><div class="asset-header-mini">{mid}</div><img src="{asset['path']}" class="asset-img-raw" loading="lazy"></div></div>"""
    else:
        return f"""<div class="row asset-row" id="{mid}"><div class="asset-card"><div class="asset-header"><span class="asset-tag">Resource</span> {mid}</div><img src="{asset['path']}" class="asset-img" loading="lazy"><div class="asset-desc-box"><div class="asset-desc-en">{asset['src']}</div><div class="asset-desc-zh">{asset['trans']}</div></div></div></div>"""

def start_interactive_server(project_context, port=8000):
    web_root = project_context['vis_output_dir']
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
            if path == '/update_task': self.handle_update_task()
            elif path == '/trigger_rerun': self.handle_trigger_rerun()
            else: self.send_error(404, "API Endpoint not found")
        def handle_update_task(self):
            try:
                length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(length)
                data = json.loads(post_data.decode('utf-8'))
                task_id = str(data.get('id'))
                user_hint = data.get('hint')
                cache_path = project_context['llm_cache_path']
                with open(cache_path, 'r', encoding='utf-8') as f: cache_data = json.load(f)
                found = False
                for task in cache_data.get("tasks", []):
                    if str(task["id"]) == task_id:
                        task['old_trans'] = task.get('trans', '') 
                        task['user_hint'] = user_hint
                        task['status'] = 'pending'
                        task['trans'] = ""
                        found = True
                        break
                if found:
                    with open(cache_path, 'w', encoding='utf-8') as f: json.dump(cache_data, f, ensure_ascii=False, indent=2)
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
                run_smart_analysis(project_context['context_path'], project_context['llm_result_path'], cache_path=project_context['llm_cache_path'])
                generate_html_report(project_context['llm_result_path'], project_context['vis_output_dir'])
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

    handler_class = partial(Handler, directory=web_root)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), handler_class) as httpd:
        print(f"🚀 服务器已启动: {target_url}")
        print("🔗 正在自动打开浏览器...")
        webbrowser.open(target_url)
        print("(提示：此单元格会一直运行 [*]，这是正常的。如需停止请按 Jupyter 的停止按钮)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n🛑 服务器已停止。")