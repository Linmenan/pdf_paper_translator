import os
import json
import base64
import uvicorn
import fitz  # PyMuPDF
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any
from fastapi.responses import FileResponse
import prompts

# 引入核心库
import workflow_utils as wf

app = FastAPI()

# --- 1. 全局配置 (硬编码路径) ---
# 用户不可修改，前端也不显示
CONFIG = {
    "pdf_dir": "./academic_papers",
    "extract_dir": "./extracted_output",
    "llm_dir": "./llm_output",
    "vis_dir": "./vis_output"
}

# 自动创建目录
for d in CONFIG.values():
    os.makedirs(d, exist_ok=True)

# 挂载静态资源
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/output", StaticFiles(directory="vis_output"), name="output")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def read_index():
    # 这样访问 http://localhost:8000 就会直接显示网页
    return FileResponse("static/index.html")

# --- 数据模型 ---
class LayoutData(BaseModel):
    page_index: int
    items: List[Dict[str, Any]] # 包含 rect, type, id, role

class SaveLayoutRequest(BaseModel):
    filename: str
    layout_data: Dict[str, List[Dict[str, Any]]] # Key是页码字符串

# --- API: 首页提示词 ---
@app.get("/api/config/prompts")
def get_prompts_config():
    """返回后端真实使用的 Prompt，供前端预览"""
    return {
        "meta": prompts.SYSTEM_PROMPT_META,
        "body": prompts.SYSTEM_PROMPT_BODY,
        "asset": prompts.SYSTEM_PROMPT_ASSET,
        "correction": prompts.SYSTEM_PROMPT_CORRECTION
    }

# --- API: 首页获取项目列表 ---
@app.get("/api/papers")
def list_papers():
    """扫描目录，返回所有PDF及其状态"""
    papers = []
    if not os.path.exists(CONFIG["pdf_dir"]):
        return []
    
    for f in os.listdir(CONFIG["pdf_dir"]):
        if f.lower().endswith(".pdf"):
            raw_name = wf.sanitize_filename(f)
            # 简单状态判断
            status = "未开始"
            if os.path.exists(os.path.join(CONFIG["vis_dir"], raw_name, f"{raw_name}_Report.html")):
                status = "已完成"
            elif os.path.exists(os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_result.txt")):
                status = "已翻译"
            elif os.path.exists(os.path.join(CONFIG["extract_dir"], f"{raw_name}_context.txt")):
                status = "已提取"
            
            papers.append({
                "filename": f,
                "raw_name": raw_name,
                "status": status
            })
    return papers

# --- API: 获取 PDF 某一页的图片 (用于前端 Canvas 背景) ---
@app.get("/api/pdf/{filename}/page/{page_idx}")
def get_pdf_page_image(filename: str, page_idx: int):
    pdf_path = os.path.join(CONFIG["pdf_dir"], filename)
    if not os.path.exists(pdf_path):
        raise HTTPException(404, "PDF not found")
    
    doc = fitz.open(pdf_path)
    if page_idx < 0 or page_idx >= len(doc):
        raise HTTPException(400, "Page index out of range")
        
    page = doc[page_idx]
    # 2倍缩放以保证前端清晰度
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) 
    img_data = pix.tobytes("png")
    base64_str = base64.b64encode(img_data).decode('utf-8')
    
    return {
        "image": f"data:image/png;base64,{base64_str}",
        "width": page.rect.width,
        "height": page.rect.height,
        "total_pages": len(doc)
    }

# --- API: 加载/保存 布局信息 (JSON) ---
@app.get("/api/layout/{filename}")
def load_layout(filename: str):
    raw_name = wf.sanitize_filename(filename)
    json_path = os.path.join(CONFIG["extract_dir"], raw_name, "layout_config.json")
    if os.path.exists(json_path):
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

@app.post("/api/layout/save")
def save_layout(req: SaveLayoutRequest):
    raw_name = wf.sanitize_filename(req.filename)
    target_dir = os.path.join(CONFIG["extract_dir"], raw_name)
    os.makedirs(target_dir, exist_ok=True)
    json_path = os.path.join(target_dir, "layout_config.json")
    
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(req.layout_data, f, ensure_ascii=False, indent=2)
    return {"status": "success"}

# [新增] API: 获取提取后的任务列表 (Step 2 使用)
@app.get("/api/extract/{filename}")
def get_extract_data(filename: str):
    raw_name = wf.sanitize_filename(filename)
    
    # 优先去 llm_output 找 (这是 workflow_utils 生成的标准位置)
    cache_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_cache.json")
    
    # 备用方案：如果还没生成，尝试去 extracted_output 找 (兼容旧逻辑)
    if not os.path.exists(cache_path):
        # 尝试构建临时任务 (如果提取了但没生成JSON)
        context_path = os.path.join(CONFIG["extract_dir"], f"{raw_name}_context.txt")
        if os.path.exists(context_path):
            with open(context_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # 实时构建任务列表返回 (不保存文件，仅预览)
            tasks, _, _ = wf.build_initial_tasks(content)
            return tasks
        else:
            raise HTTPException(404, "Extract data not found (Run Step 1 first)")
            
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 返回 tasks 数组
            return data.get("tasks", [])
    except Exception as e:
        raise HTTPException(500, f"Failed to load cache: {str(e)}")
    
# --- API: 触发工作流 ---
def _run_extract_task(pdf_path, extract_dir, vis_dir):
    # 调用修改后的 utils，skip_ui=True
    wf.extract_text_and_save_assets_smart(pdf_path, extract_dir, vis_dir, skip_ui=True)

@app.post("/api/workflow/extract/{filename}")
def trigger_extract(filename: str, background_tasks: BackgroundTasks):
    pdf_path = os.path.join(CONFIG["pdf_dir"], filename)
    # 后台运行，避免阻塞网页
    background_tasks.add_task(_run_extract_task, pdf_path, CONFIG["extract_dir"], CONFIG["vis_dir"])
    return {"status": "started", "msg": "后台提取任务已启动"}

def _run_translate_task(context_path, result_path, cache_path):
    wf.run_smart_analysis(context_path, result_path, cache_path=cache_path)

@app.post("/api/workflow/translate/{filename}")
def trigger_translate(filename: str, background_tasks: BackgroundTasks):
    raw_name = wf.sanitize_filename(filename)
    ctx_path = os.path.join(CONFIG["extract_dir"], f"{raw_name}_context.txt")
    res_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_result.txt")
    cache_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_cache.json")
    
    background_tasks.add_task(_run_translate_task, ctx_path, res_path, cache_path)
    return {"status": "started", "msg": "LLM 翻译任务已启动"}

@app.post("/api/workflow/generate_report/{filename}")
def generate_report(filename: str):
    raw_name = wf.sanitize_filename(filename)
    res_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_result.txt")
    vis_base = os.path.join(CONFIG["vis_dir"], raw_name)
    
    try:
        report_path = wf.generate_html_report(res_path, vis_base)
        # 返回相对路径供前端 iframe 访问
        rel_path = f"/output/{raw_name}/{raw_name}_Report.html"
        return {"status": "success", "url": rel_path}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

if __name__ == "__main__":
    print("🚀 启动 Web 服务: http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)