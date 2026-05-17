"""Pydantic v2 数据校验模型 — 前后端通信的严格类型屏障

扩展方向：
- 增加 UserUpdate schema 支持更多字段更新
- 增加 TaskBatchCreate 批量创建任务
"""

from datetime import date, time, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ────────────── User ──────────────

class LoginRequest(BaseModel):
    """登录请求"""
    code: str = Field("", description="wx.login 获取的 code")
    openid: str = Field("", description="直接传入 openid（开发模式）")


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
    content: str = Field(..., min_length=1, max_length=2000, description="用户消息内容")
    date: Optional[str] = Field(None, description="对话日期 YYYY-MM-DD，默认今天")
    sync: bool = Field(False, description="云托管同步模式，返回完整JSON而非SSE流")


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


class TaskCreateRequest(BaseModel):
    """手动添加任务"""
    openid: str = Field(..., min_length=1)
    goal: str = Field("", max_length=256)
    content: str = Field(..., min_length=1, max_length=500)


class TaskDeleteRequest(BaseModel):
    """删除任务"""
    task_id: int = Field(..., gt=0)
    openid: str = Field(..., min_length=1)


# ────────────── Pay ──────────────

class PayOrderRequest(BaseModel):
    """支付下单请求"""
    openid: str = Field(..., min_length=1)


class PayOrderOut(BaseModel):
    """支付下单返回 — 前端用此参数拉起 wx.requestPayment"""
    time_stamp: str
    nonce_str: str
    package: str
    sign_type: str = "RSA"
    pay_sign: str


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
