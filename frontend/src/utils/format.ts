export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function formatDateTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function statusLabel(status: string): string {
  const map: Record<string, string> = {
    queued: "排队中",
    running: "处理中",
    parsing: "解析中",
    indexing: "索引中",
    completed: "已完成",
    partial: "部分失败",
    succeeded: "成功",
    failed: "失败",
    parsed: "已解析",
    partial_ocr: "OCR 部分失败",
    ocr_failed: "OCR 失败",
    ocr_unavailable: "OCR 不可用",
    ocr_disabled: "待 OCR",
    ocr_no_text: "OCR 未提取到文本",
    ocr_extracted: "OCR 已提取",
    text_extracted: "文本已提取",
    needs_ocr: "待 OCR",
    no_text: "无可提取文本",
    empty: "空白文档",
    pending: "待索引",
    indexed: "已索引",
    ready: "就绪",
  };
  return map[status] ?? status;
}
