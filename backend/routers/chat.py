"""聊天路由 — 流式对话 + TASK_SPLIT 拦截与任务写入

扩展方向：
- 增加对话搜索
- 增加消息撤回
- 增加图片消息支持
"""

import asyncio
import base64
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import User, Conversation, Message, DailyTask, SubscribeAuth, AgentRun
from schemas import ChatRequest, ConversationOut, AllMessagesOut
from services.execution_memory import build_execution_memory, format_execution_memory_context
from services.auth_service import get_trusted_openid, require_openid_match
from services.llm_service import stream_chat, extract_task_split, extract_task_add, extract_task_done, extract_task_edit, extract_task_delete
from services.message_content import decode_message_content, encode_message_content, strip_task_protocol
from services.reminder_service import parse_reminder_request, create_scheduled_reminder
from services.scheduler_service import get_effective_date
from services.agent_orchestrator import advance_agent_stage, begin_agent_run, finish_agent_run, verify_agent_run
from services.time_service import utc_naive_to_beijing
from config import settings

router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = logging.getLogger(__name__)


_EXPLICIT_TASK_RE = re.compile(
    r"(帮我(?:拆(?:成)?|制定|安排|添加|加)(?:一下)?任务|加到任务|列(?:个|一份)?计划|"
    r"我?(?:今天|明天|这周|本周)?我?(?:要|得|必须|准备|计划|打算)"
    r"(?:去|开始)?(?:完成|做|学|背|练|写|看|复习|整理))"
)
_KNOWLEDGE_QUERY_RE = re.compile(
    r"(是什么|什么意思|定义|解释(?:一下)?|介绍(?:一下)?|科普(?:一下)?|了解(?:一下)?|"
    r"区别|为什么|怎么理解|是干嘛的|有哪些|如何看待)"
)
_EXPLICIT_ADD_RE = re.compile(
    r"(再加|还要|也要|加一个|追加|添加到任务|列入任务|放进任务)"
)
_NEGATED_MUTATION_RE = re.compile(r"(不要|别|不用|取消|不需要).{0,8}(加|建|拆|安排|任务)")
_EXPLICIT_EDIT_RE = re.compile(r"(把|将).{1,80}(改成|改为|换成|换为|修改为|重命名为)")
_EXPLICIT_DELETE_RE = re.compile(r"(删掉|删除|移除|去掉|不要做了|不想做了|取消任务)")
_SHORT_CONFIRM_RE = re.compile(r"^(?:好|好的|可以|行|加吧|安排|就这么办)[呀吧啊。！! ]*$")
_TASK_PROPOSAL_RE = re.compile(r"(要不要|是否|需不需要|可以帮你).{0,30}(加|建|拆|任务|计划)")


def _is_confirming_task_proposal(user_text: str, previous_assistant_text: str = "") -> bool:
    return bool(
        _SHORT_CONFIRM_RE.search((user_text or "").strip())
        and _TASK_PROPOSAL_RE.search((previous_assistant_text or "").strip())
    )


def _allows_task_creation(user_text: str, previous_assistant_text: str = "") -> bool:
    """Only let model directives mutate tasks after an explicit execution intent."""
    text = (user_text or "").strip()
    if not text:
        return False
    if _NEGATED_MUTATION_RE.search(text):
        return False
    if _KNOWLEDGE_QUERY_RE.search(text) and not re.search(r"(加到任务|拆成任务|制定计划|安排任务)", text):
        return False
    return bool(
        _EXPLICIT_TASK_RE.search(text)
        or _is_confirming_task_proposal(text, previous_assistant_text)
    )


def _allows_task_add(user_text: str, previous_assistant_text: str = "") -> bool:
    text = (user_text or "").strip()
    return bool(
        text
        and not _NEGATED_MUTATION_RE.search(text)
        and (
            _EXPLICIT_ADD_RE.search(text)
            or _is_confirming_task_proposal(text, previous_assistant_text)
        )
    )


def _allows_task_edit(user_text: str) -> bool:
    text = (user_text or "").strip()
    return bool(text and _EXPLICIT_EDIT_RE.search(text))


def _allows_task_delete(user_text: str) -> bool:
    text = (user_text or "").strip()
    return bool(text and _EXPLICIT_DELETE_RE.search(text))


_UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
_IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


def _llm_image_url(image_url: str) -> str:
    """Turn locally uploaded files into data URLs so the vision model can read them."""
    url = (image_url or "").strip()
    if not url.startswith("/uploads/"):
        return url
    path = _UPLOAD_DIR / Path(url).name
    if not path.exists():
        return url
    mime = _IMAGE_MIME.get(path.suffix.lower(), "image/jpeg")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _public_message_payload(message: Message, conversation_date: date | None = None) -> dict:
    payload = decode_message_content(
        message.content,
        strip_protocol=message.role == "assistant",
    )
    result = {
        "id": message.id,
        "role": message.role,
        "content": payload["text"],
        "message_type": payload["type"],
        "image_url": payload["image_url"],
        "image_cloud_id": payload["image_cloud_id"],
        "created_at": utc_naive_to_beijing(message.created_at).isoformat(),
    }
    if conversation_date is not None:
        result["conversation_date"] = conversation_date.isoformat()
    return result


def _has_visible_message_content(payload: dict) -> bool:
    """Return whether a public chat payload has anything the UI can render."""
    return bool(
        str(payload.get("content") or "").strip()
        or payload.get("image_url")
        or payload.get("image_cloud_id")
    )


def _ensure_visible_assistant_reply(text: str, task_changes: list[dict]) -> str:
    """Never persist a protocol-only assistant response as an empty bubble."""
    cleaned = strip_task_protocol(text)
    if cleaned:
        return cleaned
    if task_changes:
        return "好，已经按你的要求更新任务了。"
    return "刚才没有生成有效回复，你再说一次。"


def _stream_visible_prefix(text: str) -> str:
    """Keep internal task protocols out of provisional draft events."""
    marker = re.search(r"\|{0,3}TASK_", text, flags=re.IGNORECASE)
    return text[:marker.start()] if marker else text[:max(0, len(text) - 16)]


def _save_assistant_message(db: Session, conversation_id: int, content: str) -> Message:
    message = Message(conversation_id=conversation_id, role="assistant", content=content)
    db.add(message)
    db.flush()
    return message


def _format_reminder_confirmation(db: Session, user: User, reminder) -> tuple[str, dict]:
    remind_at = utc_naive_to_beijing(reminder.scheduled_at)
    template_id = settings.WECHAT_SUBSCRIBE_TEMPLATE_ID
    unused_count = 0
    if template_id:
        unused_count = db.query(SubscribeAuth).filter(
            SubscribeAuth.user_id == user.id,
            SubscribeAuth.template_id == template_id,
            SubscribeAuth.used == False,
        ).count()
    label = remind_at.strftime("%m月%d日 %H:%M").lstrip("0").replace("月0", "月")
    if unused_count > 0:
        content = (
            f"已设置：{label} 提醒你{reminder.content}。"
            "到点会写进小程序聊天；微信提醒额度届时仍可用的话，也会发到微信“服务通知”。"
        )
    else:
        content = (
            f"已设置：{label} 提醒你{reminder.content}。"
            "到点会写进小程序聊天；微信外提醒还没授权，点下方“立即开启”后才能发送。"
        )
    return content, {
        "action": "set",
        "reminder_id": reminder.id,
        "remind_at": remind_at.isoformat(),
        "content": reminder.content,
        "needs_subscribe_auth": unused_count <= 0,
    }


def _reminder_error_reply(error: str) -> str:
    if error == "negated":
        return "明白，我没有创建新的提醒。"
    if error == "multiple_times":
        return "一条消息里有多个时间，我先没有创建，免得提醒错。请一次告诉我一个提醒时间。"
    if error == "past_time":
        return "这个时间已经过了，所以我没有创建提醒。告诉我一个未来时间，比如“明天 16:02 提醒我练阅读”。"
    if error == "invalid_time":
        return "这个提醒时间我没看懂，所以没有创建。可以这样说：“今天 21:07 提醒我背单词”。"
    return "我还缺一个明确时间，所以没有创建提醒。可以这样说：“16:02 提醒我练阅读”。"


def _direct_chat_response(content: str, reminder_change: dict | None = None):
    """Return the same deterministic response shape for sync and local SSE."""
    async def generate():
        yield f"data: {json.dumps({'content': content}, ensure_ascii=False)}\n\n"
        if reminder_change:
            yield f"data: {json.dumps({'reminder_change': reminder_change}, ensure_ascii=False)}\n\n"
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


def _replay_completed_chat(req: ChatRequest, run: AgentRun):
    """Replay a committed request instead of calling the model twice."""
    try:
        output = json.loads(run.output_snapshot or "{}")
    except (TypeError, ValueError):
        output = {}
    content = output.get("content")
    if not content:
        raise HTTPException(status_code=409, detail="请求已完成但结果正在整理，请刷新对话")
    result = {
        "content": content,
        "task_changes": output.get("task_changes", []),
        "reminder_change": output.get("reminder_change"),
    }
    return result if req.sync else _direct_chat_response(content, result["reminder_change"])


def _ensure_user(db: Session, openid: str) -> User:
    """确保用户存在，不存在则自动创建"""
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        user = User(openid=openid)
        db.add(user)
        try:
            db.commit()
            db.refresh(user)
        except IntegrityError:
            db.rollback()
            user = db.query(User).filter(User.openid == openid).first()
            if user is None:
                raise
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
        try:
            db.commit()
            db.refresh(conv)
        except IntegrityError:
            db.rollback()
            conv = db.query(Conversation).filter(
                Conversation.user_id == user_id,
                Conversation.date == target_date,
            ).first()
            if conv is None:
                raise
    return conv


@router.post("/send")
async def chat_send(
    req: ChatRequest,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """发送消息并流式获取回复 — 核心聊天接口"""
    agent_run = None
    provider_name = "unknown"

    def capture_provider(name: str) -> None:
        nonlocal provider_name
        provider_name = name

    try:
        openid = require_openid_match(req.openid, trusted_openid)
        user = _ensure_user(db, openid)
        effective_date = get_effective_date()
        if req.date:
            try:
                effective_date = date.fromisoformat(req.date)
            except ValueError:
                pass

        conv = _ensure_conversation(db, user.id, effective_date)

        request_run_key = (
            f"chat-request:{conv.id}:{req.request_id.strip()}"
            if req.request_id.strip() else None
        )
        existing_run = None
        if request_run_key:
            existing_run = db.query(AgentRun).filter(
                AgentRun.user_id == user.id,
                AgentRun.run_key == request_run_key,
            ).first()
            if existing_run and existing_run.status == "completed":
                return _replay_completed_chat(req, existing_run)
            if existing_run and existing_run.status == "running":
                started_at = existing_run.started_at or datetime.utcnow()
                if datetime.utcnow() - started_at < timedelta(seconds=settings.AGENT_RUN_LEASE_SECONDS):
                    raise HTTPException(status_code=409, detail="同一请求正在处理中，请稍后重试")

        if request_run_key:
            user_msg = db.query(Message).filter(
                Message.conversation_id == conv.id,
                Message.role == "user",
                Message.request_id == req.request_id.strip(),
            ).first()
            if user_msg is None:
                stored_content = encode_message_content(req.content, req.image_url, req.image_cloud_id)
                user_msg = Message(
                    conversation_id=conv.id,
                    role="user",
                    content=stored_content,
                    request_id=req.request_id.strip(),
                )
                db.add(user_msg)
                try:
                    db.commit()
                    db.refresh(user_msg)
                except IntegrityError:
                    db.rollback()
                    user_msg = db.query(Message).filter(
                        Message.conversation_id == conv.id,
                        Message.role == "user",
                        Message.request_id == req.request_id.strip(),
                    ).first()
                    if user_msg is None:
                        raise

            agent_run, should_start = begin_agent_run(
                db,
                user_id=user.id,
                run_key=request_run_key,
                agent_name="coordinator",
                stage="understand",
                trigger="chat",
                input_snapshot={
                    "message_id": user_msg.id,
                    "content": req.content[:500],
                    "task_date": effective_date.isoformat(),
                },
            )
            if not should_start:
                if agent_run.status == "completed":
                    return _replay_completed_chat(req, agent_run)
                raise HTTPException(status_code=409, detail="同一请求正在处理中，请稍后重试")
        else:
            # No client idempotency key: preserve the existing one-shot behavior.
            stored_content = encode_message_content(req.content, req.image_url, req.image_cloud_id)
            user_msg = Message(conversation_id=conv.id, role="user", content=stored_content)
            db.add(user_msg)
            db.commit()
            db.refresh(user_msg)
            agent_run, _ = begin_agent_run(
                db,
                user_id=user.id,
                run_key=f"chat:{user_msg.id}",
                agent_name="coordinator",
                stage="understand",
                trigger="chat",
                input_snapshot={
                    "message_id": user_msg.id,
                    "content": req.content[:500],
                    "task_date": effective_date.isoformat(),
                },
            )

        # Exact-time reminders are a real backend capability, not an LLM
        # promise.  Intercept them before model generation and only confirm
        # after the reminder row has committed successfully.
        reminder_request = parse_reminder_request(req.content)
        if reminder_request.is_intent:
            advance_agent_stage(db, agent_run, "plan")
            reminder_change = None
            if reminder_request.scheduled_at:
                reminder = create_scheduled_reminder(
                    db,
                    user_id=user.id,
                    source_message_id=user_msg.id,
                    idempotency_key=req.request_id or f"message:{user_msg.id}",
                    content=reminder_request.content,
                    scheduled_at=reminder_request.scheduled_at,
                    commit=False,
                )
                reply, reminder_change = _format_reminder_confirmation(db, user, reminder)
            else:
                reply = _reminder_error_reply(reminder_request.error)

            assistant_msg = _save_assistant_message(db, conv.id, reply)
            advance_agent_stage(db, agent_run, "act")
            verification = verify_agent_run(db, agent_run, {
                "assistant_persisted": db.get(Message, assistant_msg.id) is not None,
            })
            finish_agent_run(
                db,
                agent_run,
                status="completed",
                stage="reflect",
                provider="deterministic",
                output_snapshot={"content": reply, "reminder_change": reminder_change, "verification": verification},
            )
            if req.sync:
                return {
                    "content": reply,
                    "task_changes": [],
                    "reminder_change": reminder_change,
                }
            return _direct_chat_response(reply, reminder_change)

        # 加载对话历史
        history_msgs = db.query(Message).filter(
            Message.conversation_id == conv.id
        ).order_by(Message.created_at).all()
        previous_assistant_text = next((
            decode_message_content(message.content)["text"]
            for message in reversed(history_msgs[:-1])
            if message.role == "assistant"
        ), "")

        messages = []
        for m in history_msgs:
            payload = decode_message_content(
                m.content,
                strip_protocol=m.role == "assistant",
            )
            if payload["type"] == "image" and m.id == user_msg.id and payload["image_url"]:
                content = [
                    {"type": "text", "text": payload["text"] or "请看这张图片并回答我。"},
                    {"type": "image_url", "image_url": {"url": _llm_image_url(payload["image_url"])}},
                ]
            elif payload["type"] == "image":
                content = (payload["text"] + "\n" if payload["text"] else "") + "[用户曾发送一张图片]"
            else:
                content = payload["text"]
            if m.role == "assistant" and not content:
                continue
            messages.append({"role": m.role, "content": content})

        # 构建今日任务与跨天执行记忆上下文
        db.expire_all()
        today_tasks = db.query(DailyTask).filter(
            DailyTask.user_id == user.id,
            DailyTask.task_date == effective_date,
        ).all()
        context_parts = []
        if today_tasks:
            completed = sum(1 for t in today_tasks if t.is_completed)
            total = len(today_tasks)
            lines = [f"目标：{today_tasks[0].goal}（共{total}个，已完成{completed}个）"]
            for t in today_tasks:
                status = "✓" if t.is_completed else "○"
                lines.append(f"#{t.id} {status} {t.content}")
            context_parts.append(
                "数据库最新任务状态（这是最高优先级事实，必须覆盖历史聊天里的旧说法）：\n"
                + "\n".join(lines)
                + "\n已打勾的任务就是已完成，绝不能再说用户没做；全部打勾时必须承认已经全部完成。"
                + "已有任务就别重复拆。任务完成只能由用户在面板手动勾选，绝不能输出 TASK_DONE；"
                + "只有用户明确要求追加才用 TASK_ADD，要改才用 TASK_EDIT，要删才用 TASK_DELETE。"
            )

        execution_memory = build_execution_memory(db, user.id, effective_date)
        context_parts.append(format_execution_memory_context(execution_memory))
        context_parts.append(
            "提醒能力规则：未来提醒只能由系统真实入库后确认。你绝不能自行承诺‘到点提醒’、"
            "也不能声称自己已经在某个时间提醒过用户。"
        )
        task_context = "\n\n".join(context_parts)
        advance_agent_stage(db, agent_run, "plan")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"聊天准备阶段失败: {e}")
        db.rollback()
        if agent_run:
            try:
                finish_agent_run(db, agent_run, status="failed", error_message=str(e))
            except Exception:
                db.rollback()
        raise HTTPException(status_code=500, detail="聊天初始化失败")

    # 同步模式（云托管用）：直接返回完整 JSON
    if req.sync:
        try:
            result = await _chat_send_sync(
                messages,
                task_context,
                db,
                user.id,
                conv.id,
                effective_date,
                req.content,
                previous_assistant_text,
                provider_callback=capture_provider,
            )
            advance_agent_stage(db, agent_run, "act")
            verification = verify_agent_run(db, agent_run, {
                "assistant_persisted": db.get(Message, result["assistant_message_id"]) is not None,
            })
            finish_agent_run(
                db,
                agent_run,
                status="completed",
                stage="reflect",
                provider=provider_name,
                output_snapshot={**result, "verification": verification},
            )
            return result
        except Exception as e:
            db.rollback()
            try:
                finish_agent_run(db, agent_run, status="failed", error_message=str(e))
            except Exception:
                db.rollback()
            raise

    # 流式响应（本地开发用）
    accumulated_text = ""
    conv_id = conv.id
    user_id = user.id

    async def generate():
        nonlocal accumulated_text
        stream_iter = None
        next_chunk = None
        emitted_text = ""
        try:
            stream_iter = stream_chat(messages, task_context=task_context, provider_callback=capture_provider)
            while True:
                if next_chunk is None:
                    next_chunk = asyncio.create_task(stream_iter.__anext__())
                try:
                    chunk = await asyncio.wait_for(asyncio.shield(next_chunk), timeout=5)
                except asyncio.TimeoutError:
                    # SSE keep-alive to prevent client timeouts
                    yield ":\n\n"
                    continue
                except StopAsyncIteration:
                    break

                next_chunk = None
                accumulated_text += chunk
                visible = _stream_visible_prefix(accumulated_text)
                if len(visible) > len(emitted_text):
                    delta = visible[len(emitted_text):]
                    emitted_text = visible
                    yield f"data: {json.dumps({'content': delta, 'draft': True}, ensure_ascii=False)}\n\n"

            # 流结束后处理任务标记
            cleaned_text = accumulated_text
            task_changes = []

            # TASK_SPLIT — 新增任务
            cleaned_text, task_data = extract_task_split(cleaned_text)
            if task_data and _allows_task_creation(req.content, previous_assistant_text):
                changes = _save_tasks_from_llm(db, user_id, effective_date, task_data)
                task_changes.extend(changes)

            # TASK_ADD — 追加单任务
            cleaned_text, add_data = extract_task_add(cleaned_text)
            if add_data and _allows_task_add(req.content, previous_assistant_text):
                change = _add_single_task(db, user_id, effective_date, add_data["content"])
                if change:
                    task_changes.append(change)

            # Do not let model language complete tasks. We still remove a
            # legacy marker if a model emits one, but completion is manual.
            cleaned_text, _ = extract_task_done(cleaned_text)

            # TASK_EDIT — 修改任务内容
            cleaned_text, edit_data = extract_task_edit(cleaned_text)
            if edit_data and _allows_task_edit(req.content):
                change = _edit_task(db, user_id, effective_date, edit_data["from"], edit_data["to"])
                if change:
                    task_changes.append(change)

            # TASK_DELETE — 删除任务
            cleaned_text, delete_keywords = extract_task_delete(cleaned_text)
            if delete_keywords and _allows_task_delete(req.content):
                changes = _delete_tasks(db, user_id, effective_date, delete_keywords)
                task_changes.extend(changes)

            cleaned_text = _ensure_visible_assistant_reply(cleaned_text, task_changes)
            advance_agent_stage(db, agent_run, "act")

            # 保存助手回复
            assistant_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=cleaned_text,
            )
            db.add(assistant_msg)
            db.flush()
            verification = verify_agent_run(db, agent_run, {
                "assistant_persisted": db.get(Message, assistant_msg.id) is not None,
            })
            finish_agent_run(
                db,
                agent_run,
                status="completed",
                stage="reflect",
                provider=provider_name,
                output_snapshot={"content": cleaned_text, "task_changes": task_changes, "verification": verification},
            )

            # 只有事务成功后才把成功内容和任务变更发给客户端，避免前端显示
            # 已完成但数据库实际回滚的假成功状态。
            # 草稿可以被客户端丢弃；只有提交和 verify 成功后才发送 committed 内容。
            yield f"data: {json.dumps({'content': cleaned_text, 'committed': True}, ensure_ascii=False)}\n\n"
            if task_changes:
                yield f"data: {json.dumps({'task_changes': task_changes}, ensure_ascii=False)}\n\n"

        except asyncio.CancelledError:
            db.rollback()
            if agent_run.status != "completed":
                try:
                    finish_agent_run(db, agent_run, status="cancelled", provider=provider_name, error_message="Client disconnected")
                except Exception:
                    db.rollback()
            raise
        except Exception as e:
            logger.error(f"流式响应失败: {e}")
            db.rollback()
            try:
                finish_agent_run(db, agent_run, status="failed", provider=provider_name, error_message=str(e))
            except Exception:
                db.rollback()
            yield f"data: {json.dumps({'error': '回复生成失败'}, ensure_ascii=False)}\n\n"
        finally:
            if next_chunk is not None and not next_chunk.done():
                next_chunk.cancel()
                await asyncio.gather(next_chunk, return_exceptions=True)
            if stream_iter is not None:
                await stream_iter.aclose()

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
        db.add(DailyTask(
            user_id=user_id,
            task_date=task_date,
            goal=goal,
            content=content,
            is_completed=False,
        ))
        existing_contents.add(content)
        added_contents.append(content)

    if added_contents:
        db.flush()
        new_tasks = db.query(DailyTask).filter(
            DailyTask.user_id == user_id,
            DailyTask.task_date == task_date,
            DailyTask.content.in_(added_contents),
        ).all()
        for t in new_tasks:
            changes.append({"action": "add", "task_id": t.id, "content": t.content})
        logger.info(f"用户 {user_id} 新增 {len(added_contents)} 个任务: {goal}")

    return changes


def _add_single_task(db: Session, user_id: int, task_date: date, content: str) -> dict | None:
    """追加单条任务到现有目标，返回变更记录"""
    content = content.strip()
    if not content:
        return None

    existing = db.query(DailyTask).filter(
        DailyTask.user_id == user_id,
        DailyTask.task_date == task_date,
    ).all()

    if any(t.content.strip() == content for t in existing):
        logger.info(f"用户 {user_id} 追加任务重复，跳过: {content}")
        return None

    goal = existing[0].goal if existing else "今日目标"

    task = DailyTask(user_id=user_id, task_date=task_date, goal=goal, content=content, is_completed=False)
    db.add(task)
    db.flush()
    logger.info(f"用户 {user_id} 追加任务: {content}")
    return {"action": "add", "task_id": task.id, "content": content}


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
    count = 0
    for kw in keywords:
        task = _find_task_by_keyword(db, user_id, task_date, kw)
        if task and not task.is_completed:
            task.is_completed = True
            task.completed_at = datetime.utcnow()
            changes.append({"action": "done", "task_id": task.id, "content": task.content})
            count += 1
    if count > 0:
        db.flush()
        logger.info(f"用户 {user_id} 通过对话完成 {count} 个任务")

    return changes


def _edit_task(db: Session, user_id: int, task_date: date, from_keyword: str, to_content: str) -> dict | None:
    """根据关键词修改任务内容，返回变更记录"""
    task = _find_task_by_keyword(db, user_id, task_date, from_keyword)
    if task:
        old_content = task.content
        task.content = to_content
        db.flush()
        logger.info(f"用户 {user_id} 修改任务: {from_keyword} -> {to_content}")
        return {"action": "edit", "task_id": task.id, "content": to_content, "old_content": old_content}

    return None


def _delete_tasks(db: Session, user_id: int, task_date: date, keywords: list[str]) -> list[dict]:
    """根据关键词删除任务，返回变更记录"""
    changes = []
    count = 0
    for kw in keywords:
        task = _find_task_by_keyword(db, user_id, task_date, kw)
        if task:
            changes.append({"action": "delete", "task_id": task.id, "content": task.content})
            db.delete(task)
            count += 1
    if count > 0:
        db.flush()
        logger.info(f"用户 {user_id} 删除 {count} 个任务")

    return changes


@router.get("/history/{openid}", response_model=list[ConversationOut])
async def get_history(
    openid: str,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """获取用户历史对话列表"""
    openid = require_openid_match(openid, trusted_openid)
    user = db.query(User).filter(User.openid == openid).first()
    if not user:
        return []

    conversations = db.query(Conversation).filter(
        Conversation.user_id == user.id
    ).order_by(Conversation.date.desc()).all()

    return conversations


@router.get("/messages/{conversation_id}")
async def get_messages(
    conversation_id: int,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """获取某次对话的所有消息"""
    if trusted_openid is None and not settings.ALLOW_INSECURE_DEV_OPENID:
        raise HTTPException(status_code=401, detail="请先登录")
    if trusted_openid is None:
        messages = db.query(Message).filter(
            Message.conversation_id == conversation_id
        ).order_by(Message.created_at).all()
    else:
        messages = db.query(Message).join(Conversation).join(User).filter(
            Message.conversation_id == conversation_id,
            User.openid == trusted_openid,
        ).order_by(Message.created_at).all()
        if not messages:
            owned_conversation = db.query(Conversation).join(User).filter(
                Conversation.id == conversation_id,
                User.openid == trusted_openid,
            ).first()
            if not owned_conversation:
                raise HTTPException(status_code=404, detail="对话不存在")

    payloads = [_public_message_payload(m) for m in messages]
    return [payload for payload in payloads if _has_visible_message_content(payload)]


@router.get("/all_messages/{openid}")
async def get_all_messages(
    openid: str,
    limit: int = 50,
    before_id: int = 0,
    db: Session = Depends(get_db),
    trusted_openid: str | None = Depends(get_trusted_openid),
):
    """获取用户所有消息（跨对话）— 按时间升序，支持分页"""
    openid = require_openid_match(openid, trusted_openid)
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

    payloads = [_public_message_payload(m, conv_date) for m, conv_date in rows]
    return [payload for payload in payloads if _has_visible_message_content(payload)]


async def _chat_send_sync(
    messages,
    task_context,
    db,
    user_id,
    conv_id,
    effective_date,
    user_text="",
    previous_assistant_text="",
    provider_callback=None,
):
    """同步模式：跑完 LLM 流，返回完整 JSON（云托管用）"""
    accumulated_text = ""
    try:
        stream_iter = stream_chat(messages, task_context=task_context, provider_callback=provider_callback)
        async for chunk in stream_iter:
            accumulated_text += chunk

        # 处理任务标记
        cleaned_text = accumulated_text
        task_changes = []

        cleaned_text, task_data = extract_task_split(cleaned_text)
        if task_data and _allows_task_creation(user_text, previous_assistant_text):
            changes = _save_tasks_from_llm(db, user_id, effective_date, task_data)
            task_changes.extend(changes)

        cleaned_text, add_data = extract_task_add(cleaned_text)
        if add_data and _allows_task_add(user_text, previous_assistant_text):
            change = _add_single_task(db, user_id, effective_date, add_data["content"])
            if change:
                task_changes.append(change)

        # Completion is intentionally manual. Strip any stale TASK_DONE marker
        # without changing a DailyTask row.
        cleaned_text, _ = extract_task_done(cleaned_text)

        cleaned_text, edit_data = extract_task_edit(cleaned_text)
        if edit_data and _allows_task_edit(user_text):
            change = _edit_task(db, user_id, effective_date, edit_data["from"], edit_data["to"])
            if change:
                task_changes.append(change)

        cleaned_text, delete_keywords = extract_task_delete(cleaned_text)
        if delete_keywords and _allows_task_delete(user_text):
            changes = _delete_tasks(db, user_id, effective_date, delete_keywords)
            task_changes.extend(changes)

        cleaned_text = _ensure_visible_assistant_reply(cleaned_text, task_changes)

        # 保存助手回复
        assistant_msg = Message(
            conversation_id=conv_id,
            role="assistant",
            content=cleaned_text,
        )
        db.add(assistant_msg)
        db.flush()

        return {"content": cleaned_text, "task_changes": task_changes, "assistant_message_id": assistant_msg.id}

    except Exception as e:
        logger.error(f"同步回复失败: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="回复生成失败")
