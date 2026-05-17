"""聊天路由 — 流式对话 + TASK_SPLIT 拦截与任务写入

扩展方向：
- 增加对话搜索
- 增加消息撤回
- 增加图片消息支持
"""

import asyncio
import json
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db
from models import User, Conversation, Message, DailyTask
from schemas import ChatRequest, ConversationOut, AllMessagesOut
from services.llm_service import stream_chat, extract_task_split, extract_task_done, extract_task_edit, extract_task_delete
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

        # 构建今日任务上下文
        today_tasks = db.query(DailyTask).filter(
            DailyTask.user_id == user.id,
            DailyTask.task_date == effective_date,
        ).all()
        task_context = None
        if today_tasks:
            lines = [f"目标：{today_tasks[0].goal}"]
            for t in today_tasks:
                status = "✓" if t.is_completed else "○"
                lines.append(f"#{t.id} {status} {t.content}")
            task_context = "用户今日任务：\n" + "\n".join(lines) + "\n注意：已有任务就别重复拆，聊进度。用户说完成了就带TASK_DONE，要改就带TASK_EDIT，要删就带TASK_DELETE。"

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
            stream_iter = stream_chat(messages, task_context=task_context)
            while True:
                try:
                    chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=5)
                except asyncio.TimeoutError:
                    # SSE keep-alive to prevent client timeouts
                    yield ":\n\n"
                    continue
                except StopAsyncIteration:
                    break

                accumulated_text += chunk
                yield f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"

            # 流结束后处理任务标记
            cleaned_text = accumulated_text

            # TASK_SPLIT — 新增任务
            cleaned_text, task_data = extract_task_split(cleaned_text)
            if task_data:
                _save_tasks_from_llm(db, user_id, effective_date, task_data)

            # TASK_DONE — 标记完成
            cleaned_text, done_keywords = extract_task_done(cleaned_text)
            if done_keywords:
                _mark_tasks_done(db, user_id, effective_date, done_keywords)

            # TASK_EDIT — 修改任务内容
            cleaned_text, edit_data = extract_task_edit(cleaned_text)
            if edit_data:
                _edit_task(db, user_id, effective_date, edit_data["from"], edit_data["to"])

            # TASK_DELETE — 删除任务
            cleaned_text, delete_keywords = extract_task_delete(cleaned_text)
            if delete_keywords:
                _delete_tasks(db, user_id, effective_date, delete_keywords)

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


def _find_task_by_keyword(db: Session, user_id: int, task_date: date, keyword: str) -> DailyTask | None:
    """模糊匹配任务：关键词包含在任务内容中"""
    tasks = db.query(DailyTask).filter(
        DailyTask.user_id == user_id,
        DailyTask.task_date == task_date,
    ).all()
    for t in tasks:
        if keyword in t.content or t.content in keyword:
            return t
    return None


def _mark_tasks_done(db: Session, user_id: int, task_date: date, keywords: list[str]):
    """根据关键词标记任务完成"""
    from datetime import datetime
    try:
        count = 0
        for kw in keywords:
            task = _find_task_by_keyword(db, user_id, task_date, kw)
            if task and not task.is_completed:
                task.is_completed = True
                task.completed_at = datetime.utcnow()
                count += 1
        if count > 0:
            db.commit()
            logger.info(f"用户 {user_id} 通过对话完成 {count} 个任务")
    except Exception as e:
        logger.error(f"标记任务完成失败: {e}")
        db.rollback()


def _edit_task(db: Session, user_id: int, task_date: date, from_keyword: str, to_content: str):
    """根据关键词修改任务内容"""
    try:
        task = _find_task_by_keyword(db, user_id, task_date, from_keyword)
        if task:
            task.content = to_content
            db.commit()
            logger.info(f"用户 {user_id} 修改任务: {from_keyword} -> {to_content}")
    except Exception as e:
        logger.error(f"修改任务失败: {e}")
        db.rollback()


def _delete_tasks(db: Session, user_id: int, task_date: date, keywords: list[str]):
    """根据关键词删除任务"""
    try:
        count = 0
        for kw in keywords:
            task = _find_task_by_keyword(db, user_id, task_date, kw)
            if task:
                db.delete(task)
                count += 1
        if count > 0:
            db.commit()
            logger.info(f"用户 {user_id} 删除 {count} 个任务")
    except Exception as e:
        logger.error(f"删除任务失败: {e}")
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


@router.get("/all_messages/{openid}")
async def get_all_messages(
    openid: str,
    limit: int = 50,
    before_id: int = 0,
    db: Session = Depends(get_db),
):
    """获取用户所有消息（跨对话）— 按时间升序，支持分页"""
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        return []

    conversation_ids = [c.id for c in db.query(Conversation).filter(
        Conversation.user_id == user.id
    ).all()]

    if not conversation_ids:
        return []

    query = db.query(Message, Conversation.date.label("conversation_date")).join(
        Conversation, Message.conversation_id == Conversation.id
    ).filter(
        Message.conversation_id.in_(conversation_ids)
    )

    if before_id > 0:
        query = query.filter(Message.id < before_id)

    rows = query.order_by(Message.created_at.desc()).limit(limit).all()
    rows.reverse()

    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
            "conversation_date": conv_date.isoformat(),
        }
        for m, conv_date in rows
    ]
