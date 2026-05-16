"""聊天路由 — 流式对话 + TASK_SPLIT 拦截与任务写入

扩展方向：
- 增加对话搜索
- 增加消息撤回
- 增加图片消息支持
"""

import json
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db
from models import User, Conversation, Message, DailyTask
from schemas import ChatRequest, ConversationOut
from services.llm_service import stream_chat, extract_task_split
from services.scheduler_service import get_effective_date

router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = logging.getLogger(__name__)


def _ensure_user(db: Session, openid: str) -> User:
    """确保用户存在，不存在则自动创建"""
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        user = User(openid=openid)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _ensure_conversation(db: Session, user_id: int, target_date: date) -> Conversation:
    """确保对话存在，不存在则创建"""
    conv = db.query(Conversation).filter(
        Conversation.user_id == user_id,
        Conversation.date == target_date,
    ).first()
    if not conv:
        conv = Conversation(user_id=user_id, date=target_date, title=f"目标追踪 {target_date}")
        db.add(conv)
        db.commit()
        db.refresh(conv)
    return conv


@router.post("/send")
async def chat_send(req: ChatRequest, db: Session = Depends(get_db)):
    """发送消息并流式获取回复 — 核心聊天接口"""
    try:
        user = _ensure_user(db, req.openid)
        effective_date = get_effective_date()
        if req.date:
            try:
                effective_date = date.fromisoformat(req.date)
            except ValueError:
                pass

        conv = _ensure_conversation(db, user.id, effective_date)

        # 保存用户消息
        user_msg = Message(conversation_id=conv.id, role="user", content=req.content)
        db.add(user_msg)
        db.commit()

        # 加载对话历史
        history_msgs = db.query(Message).filter(
            Message.conversation_id == conv.id
        ).order_by(Message.created_at).all()

        messages = [{"role": m.role, "content": m.content} for m in history_msgs]

    except Exception as e:
        logger.error(f"聊天准备阶段失败: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="聊天初始化失败")

    # 流式响应 + TASK_SPLIT 拦截
    accumulated_text = ""
    conv_id = conv.id
    user_id = user.id

    async def generate():
        nonlocal accumulated_text
        try:
            async for chunk in stream_chat(messages):
                accumulated_text += chunk
                yield f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"

            # 流结束后处理 TASK_SPLIT
            cleaned_text, task_data = extract_task_split(accumulated_text)
            if task_data:
                _save_tasks_from_llm(db, user_id, effective_date, task_data)

            # 保存助手回复
            assistant_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=cleaned_text,
            )
            db.add(assistant_msg)
            db.commit()

        except Exception as e:
            logger.error(f"流式响应失败: {e}")
            db.rollback()
            yield f"data: {json.dumps({'error': '回复生成失败'}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _save_tasks_from_llm(db: Session, user_id: int, task_date: date, task_data: dict):
    """将 LLM 拆解的任务静默写入 DailyTask 表"""
    try:
        goal = task_data.get("goal", "")
        tasks = task_data.get("tasks", [])

        if not tasks:
            return

        for task_content in tasks:
            if not task_content or not task_content.strip():
                continue
            task = DailyTask(
                user_id=user_id,
                task_date=task_date,
                goal=goal,
                content=task_content.strip(),
                is_completed=False,
            )
            db.add(task)

        db.commit()
        logger.info(f"用户 {user_id} 新增 {len(tasks)} 个任务: {goal}")

    except Exception as e:
        logger.error(f"任务写入失败: {e}")
        db.rollback()


@router.get("/history/{openid}", response_model=list[ConversationOut])
async def get_history(openid: str, db: Session = Depends(get_db)):
    """获取用户历史对话列表"""
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        return []

    conversations = db.query(Conversation).filter(
        Conversation.user_id == user.id
    ).order_by(Conversation.date.desc()).all()

    return conversations


@router.get("/messages/{conversation_id}")
async def get_messages(conversation_id: int, db: Session = Depends(get_db)):
    """获取某次对话的所有消息"""
    messages = db.query(Message).filter(
        Message.conversation_id == conversation_id
    ).order_by(Message.created_at).all()

    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in messages
    ]
