import json, sys
from .invoice_pdf import extract

try:
    content = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise ValueError("PDF 超过大小限制")
    result = {"parsed": extract(content)}
except Exception as e:
    result = {"error": str(e) if isinstance(e, ValueError) else "文件损坏、加密或版式不支持，请提供原始电子发票 PDF"}
sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
