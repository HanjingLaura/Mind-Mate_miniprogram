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
from services.llm_service import stream_chat, extract_task_split, extract_task_add, extract_task_done, extract_task_edit, extract_task_delete
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
            completed = sum(1 for t in today_tasks if t.is_completed)
            total = len(today_tasks)
            lines = [f"目标：{today_tasks[0].goal}（共{total}个，已完成{completed}个）"]
            for t in today_tasks:
                status = "✓" if t.is_completed else "○"
                lines.append(f"#{t.id} {status} {t.content}")
            task_context = "用户今日任务：\n" + "\n".join(lines) + "\n注意：已有任务就别重复拆，追加用TASK_ADD。用户说完成就带|||TASK_DONE:[\"关键词\"]|||标记，要改就带|||TASK_EDIT:{\"from\":\"原\",\"to\":\"新\"}|||标记，要删就带|||TASK_DELETE:[\"关键词\"]|||标记。"

    except Exception as e:
        logger.error(f"聊天准备阶段失败: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="聊天初始化失败")

    # 同步模式（云托管用）：直接返回完整 JSON
    if req.sync:
        return await _chat_send_sync(messages, task_context, db, user.id, conv.id, effective_date)

    # 流式响应（本地开发用）
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
            task_changes = []

            # TASK_SPLIT — 新增任务
            cleaned_text, task_data = extract_task_split(cleaned_text)
            if task_data:
                changes = _save_tasks_from_llm(db, user_id, effective_date, task_data)
                task_changes.extend(changes)

            # TASK_ADD — 追加单任务
            cleaned_text, add_data = extract_task_add(cleaned_text)
            if add_data:
                change = _add_single_task(db, user_id, effective_date, add_data["content"])
                if change:
                    task_changes.append(change)

            # TASK_DONE — 标记完成
            cleaned_text, done_keywords = extract_task_done(cleaned_text)
            if done_keywords:
                changes = _mark_tasks_done(db, user_id, effective_date, done_keywords)
                task_changes.extend(changes)

            # TASK_EDIT — 修改任务内容
            cleaned_text, edit_data = extract_task_edit(cleaned_text)
            if edit_data:
                change = _edit_task(db, user_id, effective_date, edit_data["from"], edit_data["to"])
                if change:
                    task_changes.append(change)

            # TASK_DELETE — 删除任务
            cleaned_text, delete_keywords = extract_task_delete(cleaned_text)
            if delete_keywords:
                changes = _delete_tasks(db, user_id, effective_date, delete_keywords)
                task_changes.extend(changes)

            # 发送任务变更事件
            if task_changes:
                yield f"data: {json.dumps({'task_changes': task_changes}, ensure_ascii=False)}\n\n"

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


def _save_tasks_from_llm(db: Session, user_id: int, task_date: date, task_data: dict) -> list[dict]:
    """将 LLM 拆解的任务静默写入 DailyTask 表（去重），返回变更记录"""
    changes = []
    try:
        goal = task_data.get("goal", "")
        tasks = task_data.get("tasks", [])

        if not tasks:
            return changes

        existing = db.query(DailyTask).filter(
            DailyTask.user_id == user_id,
            DailyTask.task_date == task_date,
        ).all()
        existing_contents = {t.content.strip() for t in existing}

        added_contents = []
        for task_content in tasks:
            content = task_content.strip()
            if not content or content in existing_contents:
                continue
            task = DailyTask(
                user_id=user_id,
                task_date=task_date,
                goal=goal,
                content=content,
                is_completed=False,
            )
            db.add(task)
            existing_contents.add(content)
            added_contents.append(content)

        if added_contents:
            db.commit()
            # 查询刚插入的任务获取 ID
            new_tasks = db.query(DailyTask).filter(
                DailyTask.user_id == user_id,
                DailyTask.task_date == task_date,
                DailyTask.content.in_(added_contents),
            ).all()
            for t in new_tasks:
                changes.append({"action": "add", "task_id": t.id, "content": t.content})
            logger.info(f"用户 {user_id} 新增 {len(added_contents)} 个任务: {goal}")

    except Exception as e:
        logger.error(f"任务写入失败: {e}")
        db.rollback()

    return changes


def _add_single_task(db: Session, user_id: int, task_date: date, content: str) -> dict | None:
    """追加单条任务到现有目标，返回变更记录"""
    try:
        content = content.strip()
        if not content:
            return None

        existing = db.query(DailyTask).filter(
            DailyTask.user_id == user_id,
            DailyTask.task_date == task_date,
        ).all()

        # 去重
        if any(t.content.strip() == content for t in existing):
            logger.info(f"用户 {user_id} 追加任务重复，跳过: {content}")
            return None

        # 继承已有 goal
        goal = existing[0].goal if existing else "今日目标"

        task = DailyTask(
            user_id=user_id,
            task_date=task_date,
            goal=goal,
            content=content,
            is_completed=False,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        logger.info(f"用户 {user_id} 追加任务: {content}")
        return {"action": "add", "task_id": task.id, "content": content}

    except Exception as e:
        logger.error(f"追加任务失败: {e}")
        db.rollback()
        return None


def _find_task_by_keyword(db: Session, user_id: int, task_date: date, keyword: str, prefer_incomplete: bool = True) -> DailyTask | None:
    """模糊匹配任务：评分制，优先未完成，关键词越长匹配越准确"""
    tasks = db.query(DailyTask).filter(
        DailyTask.user_id == user_id,
        DailyTask.task_date == task_date,
    ).all()

    candidates = []
    for t in tasks:
        # 关键词包含在任务内容中（主要匹配方式）
        if keyword in t.content:
            score = len(keyword) / len(t.content)
            if prefer_incomplete and not t.is_completed:
                score += 0.5
            candidates.append((t, score))
        # 任务内容包含在关键词中（回退匹配）
        elif t.content in keyword:
            score = len(t.content) / len(keyword)
            if prefer_incomplete and not t.is_completed:
                score += 0.5
            candidates.append((t, score))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


def _mark_tasks_done(db: Session, user_id: int, task_date: date, keywords: list[str]) -> list[dict]:
    """根据关键词标记任务完成，返回变更记录"""
    from datetime import datetime
    changes = []
    try:
        count = 0
        for kw in keywords:
            task = _find_task_by_keyword(db, user_id, task_date, kw)
            if task and not task.is_completed:
                task.is_completed = True
                task.completed_at = datetime.utcnow()
                changes.append({"action": "done", "task_id": task.id, "content": task.content})
                count += 1
        if count > 0:
            db.commit()
            logger.info(f"用户 {user_id} 通过对话完成 {count} 个任务")
    except Exception as e:
        logger.error(f"标记任务完成失败: {e}")
        db.rollback()

    return changes


def _edit_task(db: Session, user_id: int, task_date: date, from_keyword: str, to_content: str) -> dict | None:
    """根据关键词修改任务内容，返回变更记录"""
    try:
        task = _find_task_by_keyword(db, user_id, task_date, from_keyword)
        if task:
            old_content = task.content
            task.content = to_content
            db.commit()
            logger.info(f"用户 {user_id} 修改任务: {from_keyword} -> {to_content}")
            return {"action": "edit", "task_id": task.id, "content": to_content, "old_content": old_content}
    except Exception as e:
        logger.error(f"修改任务失败: {e}")
        db.rollback()

    return None


def _delete_tasks(db: Session, user_id: int, task_date: date, keywords: list[str]) -> list[dict]:
    """根据关键词删除任务，返回变更记录"""
    changes = []
    try:
        count = 0
        for kw in keywords:
            task = _find_task_by_keyword(db, user_id, task_date, kw)
            if task:
                changes.append({"action": "delete", "task_id": task.id, "content": task.content})
                db.delete(task)
                count += 1
        if count > 0:
            db.commit()
            logger.info(f"用户 {user_id} 删除 {count} 个任务")
    except Exception as e:
        logger.error(f"删除任务失败: {e}")
        db.rollback()

    return changes


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


async def _chat_send_sync(messages, task_context, db, user_id, conv_id, effective_date):
    """同步模式：跑完 LLM 流，返回完整 JSON（云托管用）"""
    accumulated_text = ""
    try:
        stream_iter = stream_chat(messages, task_context=task_context)
        async for chunk in stream_iter:
            accumulated_text += chunk

        # 处理任务标记
        cleaned_text = accumulated_text
        task_changes = []

        cleaned_text, task_data = extract_task_split(cleaned_text)
        if task_data:
            changes = _save_tasks_from_llm(db, user_id, effective_date, task_data)
            task_changes.extend(changes)

        cleaned_text, add_data = extract_task_add(cleaned_text)
        if add_data:
            change = _add_single_task(db, user_id, effective_date, add_data["content"])
            if change:
                task_changes.append(change)

        cleaned_text, done_keywords = extract_task_done(cleaned_text)
        if done_keywords:
            changes = _mark_tasks_done(db, user_id, effective_date, done_keywords)
            task_changes.extend(changes)

        cleaned_text, edit_data = extract_task_edit(cleaned_text)
        if edit_data:
            change = _edit_task(db, user_id, effective_date, edit_data["from"], edit_data["to"])
            if change:
                task_changes.append(change)

        cleaned_text, delete_keywords = extract_task_delete(cleaned_text)
        if delete_keywords:
            changes = _delete_tasks(db, user_id, effective_date, delete_keywords)
            task_changes.extend(changes)

        # 保存助手回复
        assistant_msg = Message(
            conversation_id=conv_id,
            role="assistant",
            content=cleaned_text,
        )
        db.add(assistant_msg)
        db.commit()

        return {"content": cleaned_text, "task_changes": task_changes}

    except Exception as e:
        logger.error(f"同步回复失败: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="回复生成失败")
