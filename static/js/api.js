import { API_BASE } from "./config.js";

export const ApiService = {
  async getPapers() {
    // 列表页通常也要防缓存
    const res = await fetch(`${API_BASE}/api/papers?t=${Date.now()}`);
    return await res.json();
  },

  async getLayout(filename) {
    // [修改] 增加时间戳，防止加载旧的布局文件
    const res = await fetch(
      `${API_BASE}/api/layout/${filename}?t=${Date.now()}`
    );
    return await res.json();
  },

  async saveLayout(filename, layoutData) {
    await fetch(`${API_BASE}/api/layout/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename, layout_data: layoutData }),
    });
  },

  async getPageImage(filename, pageIdx) {
    const res = await fetch(`${API_BASE}/api/pdf/${filename}/page/${pageIdx}`);
    return await res.json();
  },

  async triggerExtract(filename) {
    await fetch(`${API_BASE}/api/workflow/extract/${filename}`, {
      method: "POST",
    });
  },

  async getExtractData(filename) {
    // [核心修复] 增加 ?t=Timestamp，强制浏览器忽略缓存，请求最新 JSON
    const res = await fetch(
      `${API_BASE}/api/extract/${filename}?t=${Date.now()}`
    );
    if (!res.ok) throw new Error("Extract data not found");
    return await res.json();
  },

  async triggerTranslate(filename) {
    await fetch(`${API_BASE}/api/workflow/translate/${filename}`, {
      method: "POST",
    });
  },

  // 停止翻译接口
  async stopTranslation(filename) {
    const res = await fetch(`${API_BASE}/api/workflow/stop/${filename}`, {
      method: "POST",
    });
    return await res.json();
  },

  async generateReport(filename) {
    const res = await fetch(
      `${API_BASE}/api/workflow/generate_report/${filename}`,
      { method: "POST" }
    );
    return await res.json();
  },
};
