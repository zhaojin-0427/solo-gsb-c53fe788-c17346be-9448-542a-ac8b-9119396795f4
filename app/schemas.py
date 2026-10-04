"""请求 / 响应 Pydantic 模型。

载荷字段声明为 bytes：FastAPI/Pydantic 会自动做标准 Base64 解码，
响应中的 bytes 会被序列化为 Base64 字符串。
"""
from __future__ import annotations

import base64
import binascii
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

Direction = Literal["c2s", "s2c"]


def _b64_decode(v) -> bytes:
    """入参允许 Base64 字符串，统一解码为原始字节。"""
    if isinstance(v, str):
        try:
            return base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("payload 不是合法的标准 Base64") from exc
    if isinstance(v, (bytes, bytearray)):
        # 非 JSON 路径（如内部直接构造）传入的视为原始字节
        return bytes(v)
    raise ValueError("payload 必须是 Base64 字符串")


class PacketIn(BaseModel):
    cap: int = Field(..., ge=0, description="捕获序号：同一会话+方向内唯一、代表抓包先后")
    seq: int = Field(..., ge=0, le=0xFFFFFFFF, description="TCP 32 位序号")
    syn: bool = False
    fin: bool = False
    payload: bytes = Field(b"", description="Base64 编码的报文载荷")

    @field_validator("payload", mode="before")
    @classmethod
    def _decode_payload(cls, v):
        if v in (None, ""):
            return b""
        return _b64_decode(v)


class BatchPacketsIn(BaseModel):
    packets: list[PacketIn] = Field(..., min_length=1)


class PublishIn(BaseModel):
    expected_revision: int = Field(..., ge=0, description="预期当前修订号（首次发布传 0）")


class SessionOut(BaseModel):
    session_id: str
    revision: int
    created: bool


class UploadOut(BaseModel):
    session_id: str
    direction: Direction
    accepted: int
    duplicates: int
    revision: int


class DirectionSummary(BaseModel):
    complete: bool
    total_bytes: int
    span: int
    gaps: int
    divergences: int
    fin_relative_offset: Optional[int] = None


class RevisionOut(BaseModel):
    session_id: str
    revision: int
    created_at: str
    c2s: DirectionSummary
    s2c: DirectionSummary


class ErrorBody(BaseModel):
    error: str
    detail: str
    current_revision: Optional[int] = None
