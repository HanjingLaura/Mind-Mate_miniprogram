"""Pydantic v2 数据校验模型 — 前后端通信的严格类型屏障

扩展方向：
- 增加 UserUpdate schema 支持更多字段更新
- 增加 TaskBatchCreate 批量创建任务
"""

from datetime import date, time, datetime
from typing import Optional, Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ────────────── User ──────────────

class LoginRequest(BaseModel):
    """登录请求 — 小程序用 code，跨端 App 用 device_id"""
    code: str = Field("", description="wx.login 获取的 code")
    openid: str = Field("", description="直接传入 openid（开发模式）")
    device_id: str = Field("", max_length=128, description="跨端 App 设备身份")


class UserBase(BaseModel):
    nickname: Optional[str] = ""
    avatar_url: Optional[str] = ""


class UserSettingsUpdate(BaseModel):
    """用户自定义作息时间更新"""
    morning_time: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$", description="HH:MM 格式")
    afternoon_time: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$", description="HH:MM 格式")
    evening_time: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$", description="HH:MM 格式")


class UserOut(BaseModel):
    id: int
    openid: str
    nickname: str
    avatar_url: str
    morning_time: str
    afternoon_time: str
    evening_time: str
    is_vip: bool
    vip_expire_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("morning_time", "afternoon_time", "evening_time", mode="before")
    @classmethod
    def time_to_str(cls, v):
        if hasattr(v, "strftime"):
            return v.strftime("%H:%M")
        return str(v)


# ────────────── Chat ──────────────

class ChatRequest(BaseModel):
    """聊天请求"""
    openid: str = Field(..., min_length=1, description="用户 openid")
    content: str = Field("", max_length=2000, description="用户消息内容")
    image_url: str = Field("", max_length=2048, description="本次图片的临时 HTTPS 地址")
    image_cloud_id: str = Field("", max_length=1024, description="云存储 fileID，用于历史展示")
    date: Optional[str] = Field(None, description="对话日期 YYYY-MM-DD，默认今天")
    request_id: str = Field("", max_length=128, description="客户端请求幂等键")
    sync: bool = Field(False, description="云托管同步模式，返回完整JSON而非SSE流")

    @model_validator(mode="after")
    def require_text_or_image(self):
        if not self.content.strip() and not self.image_url.strip():
            raise ValueError("消息文字和图片不能同时为空")
        url = self.image_url.strip()
        if url and not (
            url.startswith("https://")
            or url.startswith("http://localhost")
            or url.startswith("http://127.0.0.1")
            or url.startswith("/uploads/")
        ):
            raise ValueError("图片地址必须使用 HTTPS 或本地上传路径")
        return self


class ChatMessageOut(BaseModel):
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AllMessagesOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime
    conversation_date: date

    model_config = {"from_attributes": True}


# ────────────── Task ──────────────

class DailyTaskOut(BaseModel):
    id: int
    task_date: date
    goal: str
    content: str
    is_completed: bool
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TaskCheckInRequest(BaseModel):
    """任务打卡"""
    task_id: int = Field(..., gt=0)
    openid: str = Field(..., min_length=1)
    completed: bool = Field(..., description="目标完成状态；重复请求必须保持幂等")


class TaskCreateRequest(BaseModel):
    """手动添加任务"""
    openid: str = Field(..., min_length=1)
    goal: str = Field("", max_length=256)
    content: str = Field(..., min_length=1, max_length=500)


class TaskEditRequest(BaseModel):
    """Manual task editing from the task board."""
    task_id: int = Field(..., gt=0)
    openid: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1, max_length=500)


class TaskDeleteRequest(BaseModel):
    """删除任务"""
    task_id: int = Field(..., gt=0)
    openid: str = Field(..., min_length=1)


# ────────────── Pay ──────────────

class PayOrderRequest(BaseModel):
    """支付下单请求"""
    openid: str = Field(..., min_length=1)


class PayCancelRequest(BaseModel):
    """支付取消记录"""
    openid: str = Field(..., min_length=1)
    out_trade_no: str = Field(..., min_length=1, max_length=64)


class PayOrderOut(BaseModel):
    """支付下单返回 — 前端用此参数拉起 wx.requestPayment"""
    time_stamp: str
    nonce_str: str
    package: str
    sign_type: str = "RSA"
    pay_sign: str
    out_trade_no: str


class PayStatusOut(BaseModel):
    """支付状态查询返回"""
    is_vip: bool
    vip_expire_at: Optional[datetime] = None
    latest_order_status: str = ""
    latest_out_trade_no: str = ""


# ────────────── Admin ──────────────

class AdminBypassRequest(BaseModel):
    """管理员内测升级请求"""
    user_id: int = Field(..., gt=0)
    secret_key: str = Field(..., min_length=1, description="内测暗号")


# ────────────── History ──────────────

class ConversationOut(BaseModel):
    id: int
    date: date
    title: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ────────────── Feedback / Analytics ──────────────

class FeedbackCreate(BaseModel):
    """用户轻反馈"""
    openid: str = Field(..., min_length=1)
    target_type: str = Field(..., min_length=1, max_length=64)
    target_id: str = Field("", max_length=64)
    rating: str = Field(..., min_length=1, max_length=64)
    comment: str = Field("", max_length=500)


class AnalyticsEventCreate(BaseModel):
    """产品事件埋点"""
    openid: str = Field("", max_length=128)
    event_name: str = Field(..., min_length=1, max_length=128)
    properties: dict[str, Any] = Field(default_factory=dict)
