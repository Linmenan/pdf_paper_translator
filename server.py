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
import asyncio
from fastapi.responses import StreamingResponse
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


# 定义请求体模型 (在文件上方位置)
class FeedbackUpdateModel(BaseModel):
    filename: str
    id: int
    hint: str

class FeedbackRerunModel(BaseModel):
    filename: str

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
        "header": prompts.SYSTEM_PROMPT_HEADER,
        "body": prompts.SYSTEM_PROMPT_BODY,
        "asset": prompts.SYSTEM_PROMPT_ASSET,
        "correction": prompts.SYSTEM_PROMPT_CORRECTION
    }

# --- API: 首页获取项目列表 ---
@app.get("/api/papers")
def list_papers():
    """扫描目录，返回所有PDF及其精确状态 (缓存状态优先)"""
    papers = []
    if not os.path.exists(CONFIG["pdf_dir"]):
        return []
    
    for f in os.listdir(CONFIG["pdf_dir"]):
        if f.lower().endswith(".pdf"):
            raw_name = wf.sanitize_filename(f)
            status = "未开始"
            
            # 路径定义
            report_path = os.path.join(CONFIG["vis_dir"], raw_name, f"{raw_name}_Report.html")
            result_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_result.txt")
            cache_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_cache.json")
            context_path = os.path.join(CONFIG["extract_dir"], f"{raw_name}_context.txt")
            
            # --- 状态判定逻辑 (优先级调整) ---
            
            # 1. 最高优先级：检查 Cache JSON 的完成度
            # 只要 Cache 存在，就以 Cache 内部的任务状态为准
            is_cache_valid = False
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, 'r', encoding='utf-8') as cf:
                        data = json.load(cf)
                        tasks = data.get("tasks", [])
                        total = len(tasks)
                        success_count = sum(1 for t in tasks if t.get("status") == "success")
                        
                        if total > 0:
                            is_cache_valid = True
                            if success_count < total:
                                # 只要有未完成的任务，无论是否有 Report，都算“翻译中”
                                status = f"翻译中 ({success_count}/{total})"
                            else:
                                # 全部完成
                                if os.path.exists(report_path):
                                    status = "已完成"
                                else:
                                    status = "翻译完成"
                except Exception as e:
                    print(f"Error reading cache for {raw_name}: {e}")
            
            # 2. 如果 Cache 不存在或读取失败，才降级检查其他文件
            if not is_cache_valid:
                if os.path.exists(report_path):
                    # 只有在没有 active cache 的情况下，才认为旧 Report 有效
                    # (这通常发生在你手动删除了 llm_output 但保留了 vis_output 的情况)
                    status = "已完成" 
                elif os.path.exists(result_path):
                    status = "翻译完成"
                elif os.path.exists(context_path):
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
    
    # 1. 尝试读取现有的缓存 (进度优先)
    cache_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_cache.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data 
        except Exception as e:
            print(f"Cache read error: {e}")
            
    # 2. 如果没有缓存，尝试从 Context 实时构建 (兜底方案)
    context_path = os.path.join(CONFIG["extract_dir"], f"{raw_name}_context.txt")
    if os.path.exists(context_path):
        try:
            with open(context_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # [核心修复] 这里必须接收 4 个返回值，否则会发生错位或报错
            # 旧代码: tasks, ref_map_str, _ = ... (错误地把 refs 赋给了 ref_map_str)
            # 新代码:
            tasks, refs, layout, ref_map_str = wf.build_initial_tasks(content)
            
            # 构造临时对象返回
            return {
                "tasks": tasks,
                "ref_map": ref_map_str, # 现在这里是正确的映射表
                "raw_references": refs,
                "is_temp": True
            }
        except Exception as e:
            # 打印详细错误方便调试
            print(f"Build tasks error: {e}")
            raise HTTPException(500, f"Failed to build from context: {str(e)}")
            
    # 3. 都没有
    raise HTTPException(404, "Data not found. Please run Step 1 Extract first.")
    
# --- API: 触发工作流 ---
def _run_extract_task(pdf_path, extract_dir, vis_dir):
    # 调用修改后的 utils，skip_ui=True
    wf.extract_text_and_save_assets_smart(pdf_path, extract_dir, vis_dir, skip_ui=True)

@app.post("/api/workflow/extract/{filename}")
def trigger_extract(filename: str):
    pdf_path = os.path.join(CONFIG["pdf_dir"], filename)
    
    try:
        # 直接运行，不再使用 background_tasks.add_task
        # 这会阻塞请求直到提取完成（通常几秒到十几秒）
        _run_extract_task(pdf_path, CONFIG["extract_dir"], CONFIG["vis_dir"])
        return {"status": "success", "msg": "提取完成"}
    except Exception as e:
        print(f"Extraction failed: {e}")
        # 返回 500 错误，前端 catch 到后会弹窗提示
        raise HTTPException(status_code=500, detail=f"提取失败: {str(e)}")

def _run_translate_task(context_path, result_path, cache_path):
    wf.run_smart_analysis(context_path, result_path, cache_path=cache_path)

@app.post("/api/workflow/translate/{filename}")
def trigger_translate(filename: str, background_tasks: BackgroundTasks):
    raw_name = wf.sanitize_filename(filename)
    
    # [新增] 确保清除上次可能遗留的停止标志
    wf.clear_stop(raw_name)
    
    ctx_path = os.path.join(CONFIG["extract_dir"], f"{raw_name}_context.txt")
    res_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_result.txt")
    cache_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_cache.json")
    
    background_tasks.add_task(_run_translate_task, ctx_path, res_path, cache_path)
    return {"status": "started", "msg": "LLM 翻译任务已启动"}

# 2. [新增] 停止接口
@app.post("/api/workflow/stop/{filename}")
def stop_translate(filename: str):
    raw_name = wf.sanitize_filename(filename)
    wf.request_stop(raw_name) # 设置标志位
    return {"status": "success", "msg": "已发送停止信号"}

async def event_generator(raw_name):
    """SSE 生成器：监听 Cache 文件变化并推送"""
    cache_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_cache.json")
    last_mod_time = 0
    
    while True:
        if os.path.exists(cache_path):
            try:
                # 检查文件修改时间，有变化才读取
                current_mod_time = os.path.getmtime(cache_path)
                if current_mod_time > last_mod_time:
                    last_mod_time = current_mod_time
                    with open(cache_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        tasks = data.get("tasks", [])
                        # 推送 JSON 字符串，格式必须是 'data: ...\n\n'
                        yield f"data: {json.dumps(tasks)}\n\n"
                        
                        # 检查是否全部完成，如果是，发送结束信号
                        if tasks and all(t.get("status") == "success" for t in tasks):
                            yield "event: close\ndata: done\n\n"
                            break
            except Exception as e:
                print(f"SSE Error: {e}")
        
        # 每 1 秒检查一次文件（这是后端检查，比 HTTP 请求轻量得多）
        await asyncio.sleep(1)

@app.get("/api/stream/translation/{filename}")
async def stream_translation_progress(filename: str):
    raw_name = wf.sanitize_filename(filename)
    return StreamingResponse(event_generator(raw_name), media_type="text/event-stream")

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

# 3. 添加新的 API 路由 
# [新增] 保存用户纠错反馈
@app.post("/api/feedback/update")
def update_feedback(data: FeedbackUpdateModel):
    # 注意：这里接收的是 raw_name，因为我们在 HTML 里注入的就是 raw_name
    raw_name = data.filename 
    task_id = data.id
    user_hint = data.hint
    
    cache_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_cache.json")
    
    if not os.path.exists(cache_path):
        return {"status": "error", "msg": "Cache file not found"}
        
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
            
        found = False
        for task in cache_data.get("tasks", []):
            if task["id"] == task_id:
                # 核心逻辑：保存 hint，清空 trans，标记为 pending
                task['old_trans'] = task.get('trans', '') # 备份旧译文
                task['user_hint'] = user_hint
                task['status'] = 'pending' # 标记为待重译
                task['trans'] = ""         # 清空译文
                found = True
                break
        
        if found:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            return {"status": "success"}
        else:
            return {"status": "error", "msg": "Task ID not found"}
            
    except Exception as e:
        return {"status": "error", "msg": str(e)}

# [新增] 触发重译并更新报告
# 定义一个后台包装函数，跑完翻译后立即重新生成报告
def _run_rerun_task(context_path, result_path, cache_path, vis_dir):
    # 1. 运行 LLM (只跑 pending 的任务)
    wf.run_smart_analysis(context_path, result_path, cache_path=cache_path)
    # 2. 翻译结束后，立即重新生成 HTML (确保正则链接和样式应用)
    wf.generate_html_report(result_path, vis_dir)

@app.post("/api/feedback/rerun")
def rerun_feedback(data: FeedbackRerunModel, background_tasks: BackgroundTasks):
    raw_name = wf.sanitize_filename(data.filename)
    
    # 构造路径
    context_path = os.path.join(CONFIG["extract_dir"], f"{raw_name}_context.txt")
    result_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_result.txt")
    cache_path = os.path.join(CONFIG["llm_dir"], f"{raw_name}_llm_cache.json")
    vis_dir = os.path.join(CONFIG["vis_dir"], raw_name)
    
    # [修改] 之前是直接运行并等待，现在改为添加到后台任务
    # 这样前端可以立即收到响应，并开始 SSE 监听
    wf.clear_stop(raw_name) # 清除之前的停止标志
    background_tasks.add_task(_run_rerun_task, context_path, result_path, cache_path, vis_dir)
    
    return {"status": "started", "msg": "后台重译任务已启动"}

if __name__ == "__main__":
    print("🚀 启动 Web 服务: http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)