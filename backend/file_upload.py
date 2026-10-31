"""Bounded, single-file multipart transport without changing collector runtimes."""

from email import policy
from email.parser import BytesParser
from fastapi import HTTPException


async def read_body(request, limit):
    # Fan-out configuration requests reuse the same Starlette Request. Cache
    # only a fully validated body, just as Request.body() does, without losing
    # the streaming size limit on the initial read.
    if hasattr(request, "_body"):
        if len(request._body) > limit:
            raise HTTPException(413, "上传文件超过大小限制")
        return request._body
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > limit:
            raise HTTPException(413, "上传文件超过大小限制")
        chunks.extend(chunk)
    request._body = bytes(chunks)
    return request._body


def multipart(content_type, raw, limit):
    if len(raw) > limit + 65536:
        raise ValueError("上传文件超过大小限制")
    if "\r" in content_type or "\n" in content_type or len(content_type) > 512:
        raise ValueError("上传格式无效")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
    )
    if message.get_content_type() != "multipart/form-data" or not message.is_multipart() or message.defects:
        raise ValueError("上传格式无效")
    fields, file = {}, None
    for index, part in enumerate(message.iter_parts()):
        if index >= 8 or part.is_multipart() or part.defects:
            raise ValueError("上传字段过多或格式无效")
        if part.get_content_disposition() != "form-data" or part.get(
            "Content-Transfer-Encoding", "binary"
        ).lower() not in ("binary", "8bit"):
            raise ValueError("上传编码无效")
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True)
        if part.get_filename() is not None:
            if name != "file" or file is not None or not 0 < len(data) <= limit:
                raise ValueError("请选择一个有效文件，且不能超过大小限制")
            filename = part.get_filename()
            if not filename or len(filename) > 200 or any(c in filename for c in "\r\n\x00"):
                raise ValueError("文件名称无效")
            file = (filename, data)
        else:
            if not name or name in fields or len(data) > 2048:
                raise ValueError("上传字段无效")
            fields[name] = data.decode("utf-8")
    if file is None:
        raise ValueError("请选择上传文件")
    return file[0], file[1], fields
