import os
import re
import time
import fitz  # PyMuPDF
import numpy as np
from google import genai
from google.genai import types

# --- 配置常量 ---
MAX_CHUNK_CHARS = 6000
TIMEOUT_MS = 600000
MAX_RETRIES = 3
RETRY_DELAY = 5

# --- Prompt 优化：新增参考文献处理策略 ---
LLM_PROMPT_TEMPLATE = """
【指令：学术论文双语重构】

你是一个专业的学术文档分析助手。请处理提供的论文片段。
输入文本包含 `[[CAPTION: ...]]` 和 `[[ASSET_REF: ...]]` 标记。

**核心任务：**
将输入内容重构为 **伪XML格式**。

**输出标签规范：**

1.  **章节标题 (Heading):**
    <header_block>
      <src>1. Introduction</src>
      <trans>1. 引言</trans>
    </header_block>

2.  **正文段落 (Body):**
    <text_block>
      <src>Original english text...</src>
      <trans>中文翻译内容...</trans>
    </text_block>

3.  **参考文献 (References) - 特殊处理:**
    当识别到 "References", "Bibliography" 或文献列表时，请使用 <ref_block> 标签。
    **注意：参考文献不需要翻译，请直接保留原文，以保证引用的准确性。**
    <ref_block>
      <src>[1] Mnih, V., et al. Human-level control...</src>
    </ref_block>

4.  **图注 (Caption):**
    <caption_block>
      <src>Figure 1. Architecture</src>
      <trans>图1. 架构</trans>
    </caption_block>

5.  **图片锚点:**
    <asset_anchor>Figure 1</asset_anchor>

**去噪规则：**
* 丢弃页眉页脚和无意义的孤立字符。
* 如果段落被换行符截断，请合并后再处理。

--- 待处理文本片段 ---
"""

def sanitize_filename(filename: str) -> str:
    if not filename: return "untitled"
    name_stem = os.path.splitext(os.path.basename(filename))[0]
    name_stem = re.sub(r'[\\/*?:"<>|]', '', name_stem)
    name_stem = re.sub(r'\s+', '_', name_stem)
    return name_stem.strip('_')

def get_union_rect(rects):
    if not rects: return None
    x0 = min(r.x0 for r in rects); y0 = min(r.y0 for r in rects)
    x1 = max(r.x1 for r in rects); y1 = max(r.y1 for r in rects)
    return fitz.Rect(x0, y0, x1, y1)

def is_box_in_rect(box, rect, threshold=0.5):
    bx0, by0, bx1, by1 = box
    rx0, ry0, rx1, ry1 = rect
    ix0 = max(bx0, rx0); iy0 = max(by0, ry0)
    ix1 = min(bx1, rx1); iy1 = min(by1, ry1)
    if ix1 > ix0 and iy1 > iy0:
        intersection = (ix1 - ix0) * (iy1 - iy0)
        b_area = (bx1 - bx0) * (by1 - by0)
        if b_area > 0 and intersection / b_area > threshold: return True
    return False

def extract_text_and_save_assets_smart(pdf_path: str, raw_text_dir: str, vis_output_root: str) -> tuple[str, str, str, int]:
    if not os.path.exists(pdf_path): raise FileNotFoundError(f"未找到 PDF 文件: {pdf_path}")

    clean_name = sanitize_filename(pdf_path)
    os.makedirs(raw_text_dir, exist_ok=True)
    txt_output_path = os.path.join(raw_text_dir, f"{clean_name}_smart_v2.txt")
    
    paper_vis_dir = os.path.join(vis_output_root, clean_name)
    assets_dir = os.path.join(paper_vis_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    doc = fitz.open(pdf_path)
    full_text_blocks = []
    asset_count = 0
    caption_pattern = re.compile(r'^\s*(Figure|Fig\.|Table)\s*(\d+)', re.IGNORECASE)
    # 移除 is_reference_header 检查，不再提前停止

    print(f"📄 智能解析 PDF (含参考文献): {clean_name}")

    for page_index, page in enumerate(doc):
        page_w = page.rect.width
        page_h = page.rect.height
        
        visual_rects = []
        for img in page.get_image_info(): visual_rects.append(fitz.Rect(img['bbox']))
        for drawing in page.get_drawings(): visual_rects.append(drawing['rect'])
        try:
            tables = page.find_tables()
            if tables.tables:
                for table in tables.tables: visual_rects.append(fitz.Rect(table.bbox))
        except: pass

        text_blocks = page.get_text("blocks")
        clean_text_blocks = []
        caption_blocks = []
        header_limit = page_h * 0.08
        footer_limit = page_h * 0.92

        for b in text_blocks:
            bbox = fitz.Rect(b[:4])
            text = b[4].strip()
            if bbox.y1 < header_limit or bbox.y0 > footer_limit: continue
            
            # 这里不再检查 "References" 并 break，允许全文提取
            
            match = caption_pattern.match(text)
            block_data = {'bbox': bbox, 'text': text, 'is_caption': bool(match), 'match': match}
            if match: caption_blocks.append(block_data)
            clean_text_blocks.append(block_data)

        processed_assets_mask = []
        caption_blocks.sort(key=lambda x: x['bbox'].y0)
        page_content_list = []

        for i, cap in enumerate(caption_blocks):
            match = cap['match']
            label_type = match.group(1)
            label_num = match.group(2)
            asset_prefix = "Table" if 'Tab' in label_type else "Figure"
            asset_name = f"{asset_prefix} {label_num}"
            filename = f"{asset_prefix}_{label_num}.png"
            
            search_y_bottom = cap['bbox'].y0
            search_y_top = 0
            if i > 0: search_y_top = caption_blocks[i-1]['bbox'].y1
            
            candidates = []
            for vr in visual_rects:
                if vr.y1 > search_y_top and vr.y0 < search_y_bottom:
                    if vr.width > 5 and vr.height > 5: candidates.append(vr)
            
            if candidates:
                final_rect = get_union_rect(candidates)
                final_rect += (-5, -5, 5, 5)
                try:
                    pix = page.get_pixmap(clip=final_rect, matrix=fitz.Matrix(2.5, 2.5))
                    pix.save(os.path.join(assets_dir, filename))
                    processed_assets_mask.append(final_rect)
                    cap['asset_ref'] = asset_name
                    asset_count += 1
                except Exception as e: print(f"Warning: {e}")

        clean_text_blocks.sort(key=lambda x: (x['bbox'].y0, x['bbox'].x0))
        
        for b in clean_text_blocks:
            is_inside_asset = False
            for mask_rect in processed_assets_mask:
                if is_box_in_rect(b['bbox'], mask_rect, threshold=0.6):
                    is_inside_asset = True; break
            if is_inside_asset: continue
                
            text = b['text']
            if b.get('is_caption'):
                if 'asset_ref' in b: page_content_list.append(f"\n\n[[ASSET_REF: {b['asset_ref']}]]\n\n")
                text = f"[[CAPTION: {text}]]"
            
            text = re.sub(r'(\w)-\n(\w)', r'\1\2', text).replace('\n', ' ')
            page_content_list.append(text)

        full_text_blocks.append("\n\n".join(page_content_list))

    with open(txt_output_path, "w", encoding="utf-8") as f: f.write("\n\n".join(full_text_blocks))
    return "\n\n".join(full_text_blocks), txt_output_path, paper_vis_dir, asset_count

def split_text_into_chunks(text: str, max_chars: int) -> list[str]:
    paragraphs = text.split('\n\n')
    chunks = []
    current_chunk = []
    current_len = 0
    for para in paragraphs:
        para = para.strip()
        if not para: continue
        if len(para) > max_chars:
            if current_chunk: chunks.append("\n\n".join(current_chunk)); current_chunk = []; current_len = 0
            chunks.append(para)
        elif current_len + len(para) > max_chars:
            chunks.append("\n\n".join(current_chunk)); current_chunk = [para]; current_len = len(para)
        else: current_chunk.append(para); current_len += len(para)
    if current_chunk: chunks.append("\n\n".join(current_chunk))
    return chunks

def run_smart_analysis(full_text: str, output_path: str, model_name: str = 'gemini-2.0-flash') -> str:
    if not os.getenv("GEMINI_API_KEY"): raise EnvironmentError("GEMINI_API_KEY 未设置")
    client = genai.Client(http_options=types.HttpOptions(timeout=TIMEOUT_MS))
    chunks = split_text_into_chunks(full_text, max_chars=MAX_CHUNK_CHARS)
    print(f"🚀 拆分为 {len(chunks)} 个片段处理 (Ref-Aware)...")
    
    all_responses = []
    for i, chunk in enumerate(chunks):
        print(f"   ⚡ 片段 {i+1}/{len(chunks)}...", end="", flush=True)
        success = False
        for attempt in range(MAX_RETRIES):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=LLM_PROMPT_TEMPLATE + f"\n\n(Part {i+1})\n" + chunk,
                    config=types.GenerateContentConfig(temperature=0.1)
                )
                if response.text:
                    all_responses.append(response.text); print(" ✅"); success = True; break
            except: time.sleep(RETRY_DELAY)
        if not success: all_responses.append("<text_block><src>Block Error</src><trans>Error</trans></text_block>")

    with open(output_path, 'w', encoding='utf-8') as f: f.write("\n".join(all_responses))
    return output_path

def generate_html_report(llm_result_path: str, paper_vis_dir: str):
    paper_name = os.path.basename(paper_vis_dir)
    assets_dir_name = "assets"
    html_path = os.path.join(paper_vis_dir, f"{paper_name}_Report.html")
    
    with open(llm_result_path, 'r', encoding='utf-8') as f: content = f.read()

    # 新正则：增加 ref_block 的匹配
    block_pattern = re.compile(r'<((?:header|text|caption|ref)_block|asset_anchor)>(.*?)</\1>', re.DOTALL)

    html_body_content = ""

    for match in block_pattern.finditer(content):
        tag_type = match.group(1)
        inner_content = match.group(2).strip()

        if tag_type == "asset_anchor":
            asset_name = inner_content
            filename = asset_name.replace(" ", "_") + ".png"
            rel_path = f"./{assets_dir_name}/{filename}"
            html_body_content += f"""
            <div class="asset-container" id="{asset_name}">
                <img src="{rel_path}" alt="{asset_name}" onerror="this.style.display='none'">
                <div class="asset-caption-label">Ref: {asset_name}</div>
            </div>"""
            continue

        # 参考文献处理：强制单栏
        if tag_type == "ref_block":
            # 尝试提取 src，如果没有标签，整个 content 就是 src
            src_match = re.search(r'<src>(.*?)</src>', inner_content, re.DOTALL)
            src_text = src_match.group(1).strip() if src_match else inner_content
            
            html_body_content += f"""
            <div class="row ref-row">
                <div class="col src" style="border-right: none; width: 100%; flex: 0 0 100%; background: #fff;">
                    {src_text}
                </div>
            </div>
            """
            continue

        # 常规双栏处理
        css_class = "text-row"
        if tag_type == "header_block": css_class = "header-row"
        if tag_type == "caption_block": css_class = "caption-row"

        src_match = re.search(r'<src>(.*?)</src>', inner_content, re.DOTALL)
        trans_match = re.search(r'<trans>(.*?)</trans>', inner_content, re.DOTALL)

        if src_match and trans_match:
            src = src_match.group(1).strip()
            trans = trans_match.group(1).strip()
            html_body_content += f"""
            <div class="row {css_class}">
                <div class="col src">{src}</div>
                <div class="col trans">{trans}</div>
            </div>"""
        else:
            # 降级处理
            html_body_content += f"""
            <div class="row {css_class}">
                <div class="col src" style="border-right:none; width:100%; flex: 0 0 100%; color:#000;">
                    {inner_content}
                </div>
            </div>"""

    html_template = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>{paper_name} - 智能分析报告</title>
        <style>
            :root {{ --primary: #2c3e50; --caption-bg: #f0f4f8; --ref-bg: #fff; }}
            body {{ font-family: "Segoe UI", Roboto, Arial, sans-serif; background: #f9f9f9; margin: 0; padding: 20px; color: #333; }}
            .container {{ max-width: 1400px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 2px 15px rgba(0,0,0,0.08); overflow: hidden; }}
            h1 {{ text-align: center; padding: 20px; background: var(--primary); color: white; margin: 0; }}
            .row {{ display: flex; border-bottom: 1px solid #eee; flex-wrap: wrap; }}
            .col {{ flex: 1; padding: 16px 24px; line-height: 1.6; text-align: justify; min-width: 0; }} 
            .src {{ color: #444; border-right: 1px solid #eee; }}
            .trans {{ color: #1a1a1a; background-color: #fafafa; }}
            .header-row {{ background: #e9ecef; border-bottom: 2px solid #dee2e6; }}
            .header-row .col {{ font-weight: bold; color: var(--primary); font-size: 1.1em; }}
            .caption-row {{ background: var(--caption-bg); font-size: 0.95em; color: #555; border-bottom: 1px dashed #ccc; }}
            
            /* 参考文献特定样式 */
            .ref-row {{ background: var(--ref-bg); }}
            .ref-row .src {{ font-family: "Cambria", "Times New Roman", serif; font-size: 0.95em; color: #666; }}
            
            .asset-container {{ text-align: center; padding: 20px; background: #fff; border-bottom: 1px solid #eee; width: 100%; }}
            .asset-container img {{ max-width: 90%; max-height: 800px; border: 1px solid #ddd; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
            .asset-caption-label {{ margin-top: 8px; font-size: 0.85em; color: #888; font-weight: bold; text-transform: uppercase; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📄 {paper_name}</h1>
            {html_body_content}
        </div>
    </body>
    </html>
    """
    
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_template)
    return html_path