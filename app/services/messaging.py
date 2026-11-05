"""Messaging: comments on records, and an internal message inbox."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import BusinessRuleViolation
from app.models.messaging import Comment, Message, MessageRecipient
from app.models.user import User
from app.repositories.base import NotFound


class MessagingService:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def _names(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        ids = {i for i in ids if i}
        if not ids:
            return {}
        rows = await self.db.execute(
            select(User.id, User.full_name).where(
                User.tenant_id == self.tenant_id, User.id.in_(ids)
            )
        )
        return {r[0]: r[1] for r in rows}

    # ================================================ comments =====

    async def list_comments(
        self,
        *,
        job_id: uuid.UUID,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> list[dict]:
        stmt = select(Comment).where(Comment.tenant_id == self.tenant_id, Comment.job_id == job_id)
        if entity_type is not None:
            stmt = stmt.where(Comment.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(Comment.entity_id == entity_id)
        rows = list(await self.db.scalars(stmt.order_by(Comment.created_at)))
        names = await self._names({c.author_id for c in rows})
        return [self._comment_dict(c, names) for c in rows]

    @staticmethod
    def _comment_dict(c: Comment, names: dict) -> dict:
        return {
            "id": c.id,
            "job_id": c.job_id,
            "entity_type": c.entity_type,
            "entity_id": c.entity_id,
            "parent_id": c.parent_id,
            "author_id": c.author_id,
            "author_name": names.get(c.author_id, "Unknown"),
            "body": c.body,
            "created_at": c.created_at,
        }

    async def add_comment(self, *, author_id: uuid.UUID | None, **data) -> dict:
        if data.get("parent_id"):
            parent = await self.db.scalar(
                select(Comment).where(
                    Comment.tenant_id == self.tenant_id, Comment.id == data["parent_id"]
                )
            )
            if parent is None:
                raise NotFound("Comment", data["parent_id"])
        comment = Comment(tenant_id=self.tenant_id, author_id=author_id, **data)
        self.db.add(comment)
        await self.db.commit()
        await self.db.refresh(comment)
        names = await self._names({author_id} if author_id else set())
        return self._comment_dict(comment, names)

    async def delete_comment(self, comment_id: uuid.UUID, *, actor_id: uuid.UUID) -> None:
        comment = await self.db.scalar(
            select(Comment).where(Comment.tenant_id == self.tenant_id, Comment.id == comment_id)
        )
        if comment is None:
            raise NotFound("Comment", comment_id)
        if comment.author_id and comment.author_id != actor_id:
            raise BusinessRuleViolation("You can only delete your own comments.")
        await self.db.delete(comment)
        await self.db.commit()

    # ================================================ messages =====

    async def send(
        self,
        *,
        sender_id: uuid.UUID,
        subject: str,
        body: str,
        recipient_ids: list[uuid.UUID],
        job_id: uuid.UUID | None = None,
    ) -> dict:
        valid = set((await self._names(set(recipient_ids))).keys())
        if not valid:
            raise BusinessRuleViolation("None of the recipients are valid users.")
        msg = Message(
            tenant_id=self.tenant_id,
            thread_id=uuid.uuid4(),
            subject=subject,
            body=body,
            sender_id=sender_id,
            job_id=job_id,
        )
        self.db.add(msg)
        await self.db.flush()
        msg.thread_id = msg.id  # the first message is its own thread root
        for uid in valid:
            self.db.add(MessageRecipient(tenant_id=self.tenant_id, message_id=msg.id, user_id=uid))
        await self.db.commit()
        return await self.get_message(msg.id)

    async def reply(self, *, thread_id: uuid.UUID, sender_id: uuid.UUID, body: str) -> dict:
        thread = list(
            await self.db.scalars(
                select(Message).where(
                    Message.tenant_id == self.tenant_id, Message.thread_id == thread_id
                )
            )
        )
        if not thread:
            raise NotFound("Thread", thread_id)
        root = min(thread, key=lambda m: m.created_at)
        # Participants = everyone who has sent or received in the thread, minus the
        # replier themselves.
        participants: set[uuid.UUID] = {m.sender_id for m in thread if m.sender_id}
        recs = await self.db.scalars(
            select(MessageRecipient.user_id).where(
                MessageRecipient.tenant_id == self.tenant_id,
                MessageRecipient.message_id.in_([m.id for m in thread]),
            )
        )
        participants |= set(recs)
        participants.discard(sender_id)
        msg = Message(
            tenant_id=self.tenant_id,
            thread_id=thread_id,
            subject=f"Re: {root.subject}" if not root.subject.startswith("Re:") else root.subject,
            body=body,
            sender_id=sender_id,
            job_id=root.job_id,
        )
        self.db.add(msg)
        await self.db.flush()
        for uid in participants:
            self.db.add(MessageRecipient(tenant_id=self.tenant_id, message_id=msg.id, user_id=uid))
        await self.db.commit()
        return await self.get_message(msg.id)

    async def get_message(self, message_id: uuid.UUID) -> dict:
        msg = await self.db.scalar(
            select(Message)
            .options(selectinload(Message.recipients))
            .where(Message.tenant_id == self.tenant_id, Message.id == message_id)
        )
        if msg is None:
            raise NotFound("Message", message_id)
        names = await self._names({msg.sender_id} | {r.user_id for r in msg.recipients})
        return self._message_dict(msg, names)

    @staticmethod
    def _message_dict(msg: Message, names: dict) -> dict:
        return {
            "id": msg.id,
            "thread_id": msg.thread_id,
            "subject": msg.subject,
            "body": msg.body,
            "sender_id": msg.sender_id,
            "sender_name": names.get(msg.sender_id, "Unknown"),
            "job_id": msg.job_id,
            "created_at": msg.created_at,
            "recipients": [
                {
                    "user_id": r.user_id,
                    "user_name": names.get(r.user_id, "Unknown"),
                    "read_at": r.read_at,
                }
                for r in msg.recipients
            ],
        }

    async def thread(self, thread_id: uuid.UUID, *, user_id: uuid.UUID) -> list[dict]:
        msgs = list(
            await self.db.scalars(
                select(Message)
                .options(selectinload(Message.recipients))
                .where(Message.tenant_id == self.tenant_id, Message.thread_id == thread_id)
                .order_by(Message.created_at)
            )
        )
        if not msgs:
            raise NotFound("Thread", thread_id)
        # Mark this user's recipient rows in the thread as read.
        now = datetime.now(UTC)
        for m in msgs:
            for r in m.recipients:
                if r.user_id == user_id and r.read_at is None:
                    r.read_at = now
        await self.db.commit()
        names = await self._names(
            {m.sender_id for m in msgs} | {r.user_id for m in msgs for r in m.recipients}
        )
        return [self._message_dict(m, names) for m in msgs]

    async def inbox(self, *, user_id: uuid.UUID) -> list[dict]:
        """Thread summaries for threads the user is a recipient of."""
        thread_ids = set(
            await self.db.scalars(
                select(Message.thread_id)
                .join(MessageRecipient, MessageRecipient.message_id == Message.id)
                .where(
                    Message.tenant_id == self.tenant_id,
                    MessageRecipient.user_id == user_id,
                )
            )
        )
        return await self._summaries(thread_ids, user_id)

    async def sent(self, *, user_id: uuid.UUID) -> list[dict]:
        thread_ids = set(
            await self.db.scalars(
                select(Message.thread_id).where(
                    Message.tenant_id == self.tenant_id, Message.sender_id == user_id
                )
            )
        )
        return await self._summaries(thread_ids, user_id)

    async def _summaries(self, thread_ids: set[uuid.UUID], user_id: uuid.UUID) -> list[dict]:
        if not thread_ids:
            return []
        msgs = list(
            await self.db.scalars(
                select(Message)
                .options(selectinload(Message.recipients))
                .where(Message.tenant_id == self.tenant_id, Message.thread_id.in_(thread_ids))
                .order_by(Message.created_at)
            )
        )
        names = await self._names(
            {m.sender_id for m in msgs} | {r.user_id for m in msgs for r in m.recipients}
        )
        by_thread: dict[uuid.UUID, list[Message]] = {}
        for m in msgs:
            by_thread.setdefault(m.thread_id, []).append(m)
        out = []
        for tid, group in by_thread.items():
            last = group[-1]
            unread = sum(
                1 for m in group for r in m.recipients if r.user_id == user_id and r.read_at is None
            )
            participants = sorted(
                {names.get(m.sender_id, "Unknown") for m in group}
                | {names.get(r.user_id, "Unknown") for m in group for r in m.recipients}
            )
            out.append(
                {
                    "thread_id": tid,
                    "subject": group[0].subject,
                    "snippet": last.body[:120],
                    "last_at": last.created_at,
                    "last_sender": names.get(last.sender_id, "Unknown"),
                    "message_count": len(group),
                    "unread": unread,
                    "participants": participants,
                }
            )
        out.sort(key=lambda s: s["last_at"], reverse=True)
        return out

    async def inbox_summary(self, *, user_id: uuid.UUID) -> dict:
        threads = await self.inbox(user_id=user_id)
        return {
            "threads": len(threads),
            "unread": sum(1 for t in threads if t["unread"] > 0),
        }
