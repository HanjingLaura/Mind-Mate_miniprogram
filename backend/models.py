"""数据库模型定义

扩展方向：
- 新增 Achievement 模型做成就系统
- 新增 Streak 模型记录连续打卡
- DailyTask 增加 priority / category 字段
"""

from datetime import datetime, time
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    Time,
    Date,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    """用户模型 — 包含自定义作息时间和 VIP 状态"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    openid = Column(String(128), unique=True, index=True, nullable=False, comment="微信 openid")
    nickname = Column(String(64), default="", comment="用户昵称")
    avatar_url = Column(String(512), default="", comment="头像 URL")

    # 自定义三段式监督时间
    morning_time = Column(Time, default=time(10, 0), comment="晨间唤醒时间")
    afternoon_time = Column(Time, default=time(16, 0), comment="午后借口狙击时间")
    evening_time = Column(Time, default=time(21, 0), comment="晚间终局清算时间")

    # VIP 状态
    is_vip = Column(Boolean, default=False, comment="是否为 VIP 用户")
    vip_expire_at = Column(DateTime, nullable=True, comment="VIP 过期时间")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    daily_tasks = relationship("DailyTask", back_populates="user", cascade="all, delete-orphan")
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    pay_orders = relationship("PayOrder", back_populates="user", cascade="all, delete-orphan")


class DailyTask(Base):
    """每日任务模型 — 由 LLM 从聊天中拆解生成"""
    __tablename__ = "daily_tasks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    task_date = Column(Date, nullable=False, comment="任务所属日期")
    goal = Column(String(256), default="", comment="当日总目标")
    content = Column(Text, default="", comment="子任务内容")
    is_completed = Column(Boolean, default=False, comment="是否完成")
    completed_at = Column(DateTime, nullable=True, comment="完成时间")
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="daily_tasks")


class Conversation(Base):
    """对话模型 — 按日期归档"""
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_conversation_user_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(Date, nullable=False, comment="对话日期")
    title = Column(String(128), default="", comment="对话标题（如目标名）")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    """消息模型 — 单条聊天记录"""
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "request_id", name="uq_message_request"),
    )

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(String(16), nullable=False, comment="user / assistant")
    content = Column(Text, default="")
    request_id = Column(String(128), nullable=True, comment="客户端请求幂等键，仅用户消息使用")
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")


class SubscribeAuth(Base):
    """订阅消息授权记录 — 一条记录代表一条可消费的通知额度"""
    __tablename__ = "subscribe_auths"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    template_id = Column(String(128), nullable=False, comment="模板 ID")
    scene = Column(String(64), default="general", comment="授权场景")
    auth_time = Column(DateTime, default=datetime.utcnow, comment="授权时间")
    used = Column(Boolean, default=False, comment="是否已消费")
    used_at = Column(DateTime, nullable=True, comment="消费时间")


class ScheduledReminder(Base):
    """用户通过聊天明确创建的单次定时提醒。"""
    __tablename__ = "scheduled_reminders"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_scheduled_reminder_request"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # 同一条聊天消息只允许创建一个提醒，避免小程序重试导致重复入库。
    source_message_id = Column(
        Integer,
        ForeignKey("messages.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    idempotency_key = Column(String(128), nullable=False)
    content = Column(Text, nullable=False, comment="提醒内容")
    scheduled_at = Column(DateTime, nullable=False, index=True, comment="UTC 时间")
    status = Column(
        String(32),
        default="pending",
        nullable=False,
        index=True,
        comment="pending/processing/sent/expired; channel tracks in_app/wechat/pending/failed/unknown",
    )
    channel = Column(String(32), default="", comment="wechat/in_app")
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)


class PayOrder(Base):
    """微信支付订单 — 用于付费漏斗和回调对账"""
    __tablename__ = "pay_orders"

    id = Column(Integer, primary_key=True, index=True)
    out_trade_no = Column(String(64), unique=True, index=True, nullable=False, comment="商户订单号")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    openid = Column(String(128), index=True, nullable=False)
    amount_cents = Column(Integer, nullable=False, comment="金额，单位分")
    status = Column(String(32), default="created", index=True, comment="created/paid/failed/cancelled")
    prepay_id = Column(String(128), default="", comment="微信预支付交易会话 ID")
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="pay_orders")


class SupervisionLog(Base):
    """监督触发日志 — 防止重复催促并分析触达效果"""
    __tablename__ = "supervision_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    supervision_type = Column(String(64), nullable=False, index=True)
    task_date = Column(Date, nullable=False, index=True)
    sent_at = Column(DateTime, default=datetime.utcnow)
    channel = Column(String(32), default="in_app", comment="wechat/in_app")
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)


class Feedback(Base):
    """轻量反馈 — 用于判断监督文案是否有效或冒犯"""
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    target_type = Column(String(64), nullable=False, comment="chat/supervision/vip_modal/review")
    target_id = Column(String(64), default="")
    rating = Column(String(64), nullable=False, comment="useful/too_soft/too_hard/useless")
    comment = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class AnalyticsEvent(Base):
    """产品事件埋点 — 支撑付费和留存漏斗分析"""
    __tablename__ = "analytics_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    openid = Column(String(128), default="", index=True)
    event_name = Column(String(128), nullable=False, index=True)
    properties = Column(Text, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)


class AgentRun(Base):
    """一次可追踪的 Agent 闭环执行。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "run_key", name="uq_agent_run_user_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    run_key = Column(String(192), nullable=False)
    agent_name = Column(String(64), nullable=False, index=True)
    stage = Column(String(32), nullable=False, comment="understand/plan/act/verify/reflect")
    trigger = Column(String(64), nullable=False, comment="chat/scheduled/task_change")
    status = Column(String(32), nullable=False, default="running", index=True)
    provider = Column(String(32), default="", comment="bailian/zhipu/fallback")
    input_snapshot = Column(Text, default="{}")
    output_snapshot = Column(Text, default="{}")
    error_message = Column(Text, default="")
    lease_token = Column(String(64), default="", nullable=False, comment="当前执行租约 fencing token")
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
