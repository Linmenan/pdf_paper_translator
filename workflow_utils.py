import os
import re
import time
import shutil
import platform
import fitz  # PyMuPDF
import tkinter as tk
from tkinter import messagebox, font
from PIL import Image, ImageTk
from google import genai
from google.genai import types

# --- 配置常量 ---
MAX_CHUNK_CHARS = 5000
TIMEOUT_MS = 600000
MAX_RETRIES = 3
RETRY_DELAY = 5

# --- Prompt ---
LLM_PROMPT_TEMPLATE = """
【指令：学术论文双语重构】
输入文本包含人工校对的 `[[ASSET_REF: ID]]` 标记。

**核心原则：**
1. **ID 绝对禁止修改**：`[[ASSET_REF: Figure_P1_1]]` 必须严格输出为 `<asset_anchor>Figure_P1_1</asset_anchor>`。
2. **结构优先**：必须精准识别摘要、参考文献和标题。

**输出标签规范（伪XML）：**
1. <header_block><src>...</src><trans>...</trans></header_block>
2. <text_block><src>...</src><trans>...</trans></text_block>
3. <caption_block><src>...</src><trans>...</trans></caption_block>
4. <ref_block><src>...</src></ref_block> (⚠️不翻译，保留原文)
5. <asset_anchor>Figure_P1_1</asset_anchor> (保留原ID)

--- 待处理文本片段 ---
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

def get_optimal_cjk_font(root):
    system = platform.system()
    available_families = set(font.families(root))
    candidates = []
    if system == "Windows":
        candidates = ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "KaiTi"]
    elif system == "Darwin":
        candidates = ["PingFang SC", "Heiti SC", "STHeiti"]
    else:
        candidates = ["Noto Sans CJK SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Droid Sans Fallback", "Source Han Sans CN", "SimHei"]
    for f in candidates:
        if f in available_families: return f
    return "Helvetica"

# --- 交互式编辑器 ---
class LayoutEditor:
    def __init__(self, doc, initial_data):
        self.doc = doc
        self.data = initial_data
        self.page_count = len(doc)
        self.current_page = 0
        
        self.root = tk.Tk()
        self.ui_font = get_optimal_cjk_font(self.root)
        
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        page = doc[0]
        pdf_w, pdf_h = page.rect.width, page.rect.height
        target_h = screen_h * 0.85
        self.scale = target_h / pdf_h
        
        win_w = min(int(pdf_w * self.scale) + 40, int(screen_w * 0.95))
        win_h = min(int(pdf_h * self.scale) + 120, int(screen_h * 0.95))
        
        self.root.geometry(f"{win_w}x{win_h}")
        self.root.title(f"PDF人工校对 (Page 1/{self.page_count})")

        self.current_type = "Figure"
        self.type_colors = {
            "Figure": "#e74c3c",    # 红
            "Table": "#3498db",     # 蓝
            "Equation": "#27ae60",  # 绿
            "Algorithm": "#9b59b6", # 紫
            "ContentArea": "#f1c40f" # 黄 (正文区域)
        }
        self.rect_id_map = {}
        self.start_x = None; self.start_y = None; self.current_rect_id = None

        # UI
        self.control_panel = tk.Frame(self.root, height=100, bg="#f0f0f0")
        self.control_panel.pack(side=tk.TOP, fill=tk.X)
        self.control_panel.pack_propagate(False)
        
        self.info_label = tk.Label(self.control_panel, text="加载中...", 
                                   font=(self.ui_font, 18, "bold"), bg="#f0f0f0", fg="#333")
        self.info_label.pack(side=tk.TOP, pady=5)
        
        help_txt = "快捷键: 1-图 2-表 3-式 4-算 | [0-正文区域(黄色)] | 右键删除 | 空格下一页"
        self.help_label = tk.Label(self.control_panel, text=help_txt, 
                                   font=(self.ui_font, 12), bg="#f0f0f0", fg="#555")
        self.help_label.pack(side=tk.TOP, pady=2)

        self.btn_frame = tk.Frame(self.control_panel, bg="#f0f0f0")
        self.btn_frame.pack(side=tk.TOP, pady=5)
        
        self.btn_prev = tk.Button(self.btn_frame, text="< 上一页", command=self.prev_page, font=(self.ui_font, 10))
        self.btn_prev.pack(side=tk.LEFT, padx=10)
        self.btn_next = tk.Button(self.btn_frame, text="下一页 >", command=self.next_page, font=(self.ui_font, 10, "bold"), bg="#4CAF50", fg="white")
        self.btn_next.pack(side=tk.LEFT, padx=10)

        self.canvas_frame = tk.Frame(self.root)
        self.canvas_frame.pack(fill=tk.BOTH, expand=True)
        self.v_scroll = tk.Scrollbar(self.canvas_frame, orient=tk.VERTICAL)
        self.h_scroll = tk.Scrollbar(self.canvas_frame, orient=tk.HORIZONTAL)
        self.canvas = tk.Canvas(self.canvas_frame, bg="#555",
                                yscrollcommand=self.v_scroll.set, xscrollcommand=self.h_scroll.set)
        self.v_scroll.config(command=self.canvas.yview); self.h_scroll.config(command=self.canvas.xview)
        self.v_scroll.pack(side=tk.RIGHT, fill=tk.Y); self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.root.bind("<Key>", self.on_key_press)
        
        self.load_page()
        self.root.mainloop()

    def load_page(self):
        self.canvas.delete("all")
        self.rect_id_map = {}
        page = self.doc[self.current_page]
        
        type_map = {"Figure": "图", "Table": "表", "Equation": "公式", "Algorithm": "算法", "ContentArea": "正文区域(屏蔽页眉页脚)"}
        t_str = type_map.get(self.current_type, self.current_type)
        self.info_label.config(text=f"第 {self.current_page + 1}/{self.page_count} 页 - 当前工具: [{t_str}]")
        
        self.btn_prev.config(state=tk.DISABLED if self.current_page == 0 else tk.NORMAL)
        self.btn_next.config(text="完成 (Finish)" if self.current_page == self.page_count - 1 else "下一页 >")

        pix = page.get_pixmap(matrix=fitz.Matrix(self.scale, self.scale))
        img_data = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        self.tk_img = ImageTk.PhotoImage(img_data)
        
        self.canvas.config(scrollregion=(0, 0, pix.width, pix.height))
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_img)
        
        if self.current_page in self.data:
            for idx, item in enumerate(self.data[self.current_page]):
                r = item['rect']
                x0, y0, x1, y1 = r.x0*self.scale, r.y0*self.scale, r.x1*self.scale, r.y1*self.scale
                color = self.type_colors.get(item['type'], "black")
                
                dash = (5, 5) if item['type'] == "ContentArea" else None
                width = 4 if item['type'] == "ContentArea" else 2
                
                rect_id = self.canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=width, dash=dash, tags="box")
                label_bg = self.canvas.create_rectangle(x0, y0-25, x0+120, y0, fill=color, outline=color, tags="box")
                text_id = self.canvas.create_text(x0+5, y0-12, text=item['type'], anchor=tk.W, 
                                                  fill="white", font=(self.ui_font, 12, "bold"), tags="box")
                self.rect_id_map[rect_id] = idx; self.rect_id_map[text_id] = idx; self.rect_id_map[label_bg] = idx

    def on_mouse_down(self, event):
        self.start_x = self.canvas.canvasx(event.x); self.start_y = self.canvas.canvasy(event.y)
        color = self.type_colors.get(self.current_type, "black")
        self.current_rect_id = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, 
                                                            outline=color, width=2, dash=(4,4))

    def on_mouse_drag(self, event):
        self.canvas.coords(self.current_rect_id, self.start_x, self.start_y, self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))

    def on_mouse_up(self, event):
        x0, x1 = sorted([self.start_x, self.canvas.canvasx(event.x)])
        y0, y1 = sorted([self.start_y, self.canvas.canvasy(event.y)])
        if x1 - x0 < 10 or y1 - y0 < 10: self.canvas.delete(self.current_rect_id); return
        
        pdf_rect = fitz.Rect(x0/self.scale, y0/self.scale, x1/self.scale, y1/self.scale)
        if self.current_page not in self.data: self.data[self.current_page] = []
        
        if self.current_type == "ContentArea":
            self.data[self.current_page] = [x for x in self.data[self.current_page] if x['type'] != "ContentArea"]
            
        self.data[self.current_page].append({'rect': pdf_rect, 'type': self.current_type})
        self.load_page()

    def on_right_click(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        items = self.canvas.find_overlapping(x-5, y-5, x+5, y+5)
        for item_id in items:
            if item_id in self.rect_id_map:
                del self.data[self.current_page][self.rect_id_map[item_id]]
                self.load_page(); break

    def on_key_press(self, event):
        k = event.keysym
        if k == '1': self.set_type("Figure")
        elif k == '2': self.set_type("Table")
        elif k == '3': self.set_type("Equation")
        elif k == '4': self.set_type("Algorithm")
        elif k == '0': self.set_type("ContentArea")
        elif k in ['space', 'Return', 'Right']: self.next_page()
        elif k in ['BackSpace', 'Left']: self.prev_page()
    
    def set_type(self, t): self.current_type = t; self.load_page()
    def prev_page(self):
        if self.current_page > 0: self.current_page -= 1; self.load_page()
    
    def next_page(self):
        current_content_rect = None
        if self.current_page in self.data:
            for item in self.data[self.current_page]:
                if item['type'] == 'ContentArea':
                    current_content_rect = item['rect']
                    break
        
        next_idx = self.current_page + 1
        if current_content_rect and next_idx < self.page_count:
            if next_idx not in self.data: self.data[next_idx] = []
            self.data[next_idx] = [x for x in self.data[next_idx] if x['type'] != 'ContentArea']
            self.data[next_idx].insert(0, {'rect': fitz.Rect(current_content_rect), 'type': 'ContentArea'})
            print(f"   ℹ️ [继承] P{self.current_page+1} 的正文区域已应用到 P{next_idx+1}")

        if self.current_page < self.page_count - 1:
            self.current_page += 1
            self.load_page()
        else: 
            if messagebox.askyesno("完成", "确认完成校对？"): self.root.destroy()

# --- 自动检测 ---
def generate_initial_candidates(doc):
    candidates = {}
    for i, page in enumerate(doc):
        items = []
        w, h = page.rect.width, page.rect.height
        content_rect = fitz.Rect(0, h * 0.08, w, h * 0.92) 
        items.append({'rect': content_rect, 'type': 'ContentArea'})

        for img in page.get_image_info(): items.append({'rect': fitz.Rect(img['bbox']), 'type': 'Figure'})
        try:
            for t in page.find_tables().tables: items.append({'rect': fitz.Rect(t.bbox), 'type': 'Table'})
        except: pass
        
        assets = [x for x in items if x['type'] != 'ContentArea']
        content = [x for x in items if x['type'] == 'ContentArea']
        
        merged = []
        while assets:
            curr = assets.pop(0)
            overlap = False
            for ex in merged:
                if is_box_in_rect(curr['rect'], ex['rect'], 0.1) or is_box_in_rect(ex['rect'], curr['rect'], 0.1):
                    ex['rect'] |= curr['rect']; overlap = True; break
            if not overlap: merged.append(curr)
            
        candidates[i] = content + merged
    return candidates

# --- 核心提取 ---
def extract_text_and_save_assets_smart(pdf_path: str, raw_text_dir: str, vis_output_root: str) -> tuple[str, str, str, int]:
    if not os.path.exists(pdf_path): raise FileNotFoundError(f"未找到 PDF: {pdf_path}")
    clean_name = sanitize_filename(pdf_path)
    os.makedirs(raw_text_dir, exist_ok=True)
    txt_output_path = os.path.join(raw_text_dir, f"{clean_name}_human_verified.txt")
    
    paper_vis_dir = os.path.join(vis_output_root, clean_name)
    assets_dir = os.path.join(paper_vis_dir, "assets")
    if os.path.exists(assets_dir): shutil.rmtree(assets_dir)
    os.makedirs(assets_dir, exist_ok=True)

    doc = fitz.open(pdf_path)
    print("🤖 预检测...")
    init_data = generate_initial_candidates(doc)
    
    print("🖥️ 启动校对界面...")
    editor = LayoutEditor(doc, init_data)
    verified_data = editor.data
    
    print("✅ 提取中...")
    full_text_blocks = []
    asset_count = 0
    
    # --- 标题检测正则优化 ---
    header_pattern = re.compile(r'^(\d+(\.\d+)*\.?|[IVX]+\.|[A-Z]\.)\s+|^(Abstract|References|Acknowledgments|Introduction|Conclusion|Method|Methodology|Experiments|Discussion|Related Work)$', re.IGNORECASE)
    caption_pattern = re.compile(r'^\s*(Figure|Fig\.|Table|Tab\.|Algorithm)\s*(\d+)', re.IGNORECASE)

    for page_idx, page in enumerate(doc):
        page_items = verified_data.get(page_idx, [])
        
        content_area = None
        assets = []
        for item in page_items:
            if item['type'] == 'ContentArea': content_area = item['rect']
            else: assets.append(item)
            
        if not content_area: content_area = page.rect
        
        assets.sort(key=lambda x: x['rect'].y0)
        masks = []; flow_assets = []
        
        for i, item in enumerate(assets):
            rect = item['rect']; atype = item['type']
            asset_id = f"{atype}_P{page_idx+1}_{i+1}"
            filename = f"{asset_id}.png"
            try:
                pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(3, 3))
                pix.save(os.path.join(assets_dir, filename))
                masks.append(rect)
                flow_assets.append({'rect': rect, 'id': asset_id, 'type': atype})
                asset_count += 1
            except: pass

        blocks = page.get_text("blocks", clip=content_area)
        
        mixed = []
        for b in blocks:
            bbox = fitz.Rect(b[:4]); text = b[4].strip()
            is_in = False
            for m in masks:
                if is_box_in_rect(bbox, m, 0.6): is_in = True; break
            if not is_in:
                # --- 行级标题检测 ---
                lines = text.split('\n')
                if lines:
                    first_line = lines[0].strip()
                    # 1. 检查是否是标题
                    if header_pattern.match(first_line) and len(first_line) < 100:
                        # 独立添加标题
                        mixed.append({'bbox': bbox, 'text': first_line, 'is_header': True, 'is_asset': False})
                        # 剩余部分作为正文
                        if len(lines) > 1:
                            rem = "\n".join(lines[1:])
                            mixed.append({'bbox': bbox, 'text': rem, 'is_header': False, 'is_asset': False})
                    else:
                        # 2. 检查是否是Caption
                        match = caption_pattern.match(text)
                        mixed.append({'bbox': bbox, 'text': text, 'is_asset': False, 'is_caption': bool(match), 'is_header': False})
        
        for asset in flow_assets: mixed.append({'bbox': asset['rect'], 'text': asset['id'], 'is_asset': True})
        mixed.sort(key=lambda x: (x['bbox'].y0, x['bbox'].x0))
        
        page_content = []
        for b in mixed:
            if b.get('is_asset'):
                page_content.append(f"\n\n[[ASSET_REF: {b['text']}]]\n\n")
            elif b.get('is_header'):
                # --- 关键：注入 Header 标记 ---
                page_content.append(f"\n\n[[HEADER: {b['text']}]]\n\n")
            else:
                text = b['text']
                if b.get('is_caption'): text = f"[[CAPTION: {text}]]"
                text = re.sub(r'(\w)-\n(\w)', r'\1\2', text).replace('\n', ' ')
                page_content.append(text)
        full_text_blocks.append("\n\n".join(page_content))

    with open(txt_output_path, "w", encoding="utf-8") as f: f.write("\n\n".join(full_text_blocks))
    return "\n\n".join(full_text_blocks), txt_output_path, paper_vis_dir, asset_count

# --- LLM ---
def split_text_into_chunks(text: str, max_chars: int) -> list[str]:
    paragraphs = text.split('\n\n')
    chunks = []; curr = []; curr_len = 0
    for p in paragraphs:
        p = p.strip(); 
        if not p: continue
        if len(p) > max_chars:
            if curr: chunks.append("\n\n".join(curr)); curr = []; curr_len = 0
            chunks.append(p)
        elif curr_len + len(p) > max_chars:
            chunks.append("\n\n".join(curr)); curr = [p]; curr_len = len(p)
        else: curr.append(p); curr_len += len(p)
    if curr: chunks.append("\n\n".join(curr))
    return chunks

def run_smart_analysis(full_text: str, output_path: str, model_name: str = 'gemini-2.0-flash') -> str:
    if not os.getenv("GEMINI_API_KEY"): raise EnvironmentError("No API Key")
    client = genai.Client(http_options=types.HttpOptions(timeout=TIMEOUT_MS))
    chunks = split_text_into_chunks(full_text, max_chars=MAX_CHUNK_CHARS)
    print(f"🚀 处理 {len(chunks)} 个片段...")
    res = []
    for i, c in enumerate(chunks):
        print(f"   ⚡ P{i+1}...", end="", flush=True)
        ok = False
        for _ in range(MAX_RETRIES):
            try:
                r = client.models.generate_content(model=model_name, contents=LLM_PROMPT_TEMPLATE + f"\n\n(Part {i+1})\n" + c, config=types.GenerateContentConfig(temperature=0.1))
                if r.text: 
                    # --- 强力去噪 ---
                    clean_text = re.sub(r'\(\s*Part\s+\d+\s*\)', '', r.text) # 删除任何位置的 (Part X)
                    res.append(clean_text)
                    print(" ✅"); ok = True; break
            except: time.sleep(RETRY_DELAY)
        if not ok: res.append("<text_block><src>Error</src><trans>Fail</trans></text_block>")
    with open(output_path, 'w', encoding='utf-8') as f: f.write("\n".join(res))
    return output_path

def generate_html_report(llm_result_path: str, paper_vis_dir: str):
    paper_name = os.path.basename(paper_vis_dir)
    assets_dir_name = "assets"
    html_path = os.path.join(paper_vis_dir, f"{paper_name}_Report.html")
    with open(llm_result_path, 'r', encoding='utf-8') as f: content = f.read()

    block_pattern = re.compile(r'<((?:header|text|caption|ref)_block|asset_anchor)>(.*?)</\1>', re.DOTALL)
    body = ""

    for match in block_pattern.finditer(content):
        tag = match.group(1); inner = match.group(2).strip()
        if tag == "asset_anchor":
            asset_id = inner; filename = f"{asset_id}.png"; rel_path = f"./{assets_dir_name}/{filename}"
            label = asset_id.replace("_", " ")
            body += f"""<div class="asset-container" id="{asset_id}"><img src="{rel_path}" alt="{asset_id}" onerror="this.style.display='none'"><div class="asset-caption-label">{label}</div></div>"""
            continue
        
        if tag == "ref_block":
            src_m = re.search(r'<src>(.*?)</src>', inner, re.DOTALL)
            txt = src_m.group(1).strip() if src_m else inner
            txt = txt.replace('\n', ' ') 
            # 严格匹配 [1-3位数字]
            txt = re.sub(r'(\[\d{1,3}\])', r'<br><b style="color:#e74c3c;">\1</b>', txt)
            if txt.startswith('<br>'): txt = txt[4:]
            body += f"""<div class="row ref-row"><div class="col src" style="width:100%;border:none;">{txt}</div></div>"""
            continue
        
        css = "text-row"
        if tag == "header_block": css = "header-row"
        if tag == "caption_block": css = "caption-row"
        src_m = re.search(r'<src>(.*?)</src>', inner, re.DOTALL)
        trans_m = re.search(r'<trans>(.*?)</trans>', inner, re.DOTALL)
        if src_m and trans_m:
            body += f"""<div class="row {css}"><div class="col src">{src_m.group(1).strip()}</div><div class="col trans">{trans_m.group(1).strip()}</div></div>"""
        else:
            body += f"""<div class="row {css}"><div class="col src" style="width:100%;border:none;">{inner}</div></div>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>{paper_name}</title>
    <style>
        :root {{ --p: #2c3e50; --bg: #f9f9f9; }}
        body {{ font: 16px/1.6 "Segoe UI", Roboto, sans-serif; background: var(--bg); margin: 0; padding: 20px; color: #333; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: #fff; box-shadow: 0 2px 10px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }}
        h1 {{ background: var(--p); color: #fff; padding: 20px; margin: 0; text-align: center; }}
        .row {{ display: flex; border-bottom: 1px solid #eee; }}
        .col {{ flex: 1; padding: 20px; text-align: justify; min-width: 0; }}
        .src {{ border-right: 1px solid #eee; color: #444; }} .trans {{ background: #fafafa; }}
        .header-row {{ background: #e9ecef; border-bottom: 2px solid #cbd5e0; }} 
        .header-row .col {{ font-weight: 700; color: var(--p); font-size: 1.1em; }}
        .caption-row {{ background: #f0f4f8; font-style: italic; font-size: 0.9em; color: #555; border-bottom: 1px dashed #ccc; }}
        .ref-row .src {{ font-family: Cambria, serif; color: #555; font-size: 0.95em; line-height: 1.6; padding-left: 2.5em; text-indent: -2.5em; }}
        .asset-container {{ text-align: center; padding: 20px; background: #fff; border-bottom: 1px solid #eee; }}
        .asset-container img {{ max-width: 95%; max-height: 800px; border: 1px solid #ddd; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }}
        .asset-caption-label {{ margin-top: 10px; font-size: 0.85em; color: #999; font-weight: bold; text-transform: uppercase; letter-spacing: 1px; }}
    </style></head><body><div class="container"><h1>📄 {paper_name}</h1>{body}</div></body></html>"""
    with open(html_path, 'w', encoding='utf-8') as f: f.write(html)
    return html_path