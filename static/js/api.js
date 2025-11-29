import { API_BASE } from "./config.js";

export const ApiService = {
  async getPapers() {
    const res = await fetch(`${API_BASE}/api/papers`);
    return await res.json();
  },

  async getLayout(filename) {
    const res = await fetch(`${API_BASE}/api/layout/${filename}`);
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
    const res = await fetch(`${API_BASE}/api/extract/${filename}`);
    if (!res.ok) throw new Error("Extract data not found");
    return await res.json();
  },

  async triggerTranslate(filename) {
    await fetch(`${API_BASE}/api/workflow/translate/${filename}`, {
      method: "POST",
    });
  },

  async generateReport(filename) {
    const res = await fetch(
      `${API_BASE}/api/workflow/generate_report/${filename}`,
      { method: "POST" }
    );
    return await res.json();
  },
};
