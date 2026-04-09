from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user
from backend.core.database import get_db
from backend.core.models import (
    Conversation,
    PlatformGatewayConnection,
    PlatformGatewayEventLog,
    PlatformGatewayRouteRule,
    User,
)

router = APIRouter(prefix="/gateway", tags=["gateway"])


class CreateGatewayConnectionRequest(BaseModel):
    platform_type: str = Field(pattern="^(dingtalk|feishu|webhook|wechat_work)$")
    display_name: str = Field(min_length=1, max_length=128)
    app_key: str | None = None
    app_secret_ref: str | None = None
    bot_id: str | None = None
    callback_url: str | None = None
    enabled: bool = True
    config_json: dict = Field(default_factory=dict)


class UpdateGatewayConnectionRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    app_key: str | None = None
    app_secret_ref: str | None = None
    bot_id: str | None = None
    callback_url: str | None = None
    enabled: bool | None = None
    config_json: dict | None = None


class CreateGatewayRuleRequest(BaseModel):
    connection_id: str
    rule_name: str = Field(min_length=1, max_length=128)
    source_scope: str = Field(pattern="^(private|group|all)$")
    source_id: str | None = None
    keyword: str | None = None
    conversation_id: str | None = None
    default_model: str | None = None
    execution_backend: str | None = None
    enabled: bool = True


class UpdateGatewayRuleRequest(BaseModel):
    rule_name: str | None = Field(default=None, min_length=1, max_length=128)
    source_scope: str | None = Field(default=None, pattern="^(private|group|all)$")
    source_id: str | None = None
    keyword: str | None = None
    conversation_id: str | None = None
    default_model: str | None = None
    execution_backend: str | None = None
    enabled: bool | None = None


def _serialize_connection(item: PlatformGatewayConnection) -> dict:
    return {
        "id": item.id,
        "owner_user_id": item.owner_user_id,
        "platform_type": item.platform_type,
        "display_name": item.display_name,
        "app_key": item.app_key,
        "app_secret_ref": item.app_secret_ref,
        "bot_id": item.bot_id,
        "callback_url": item.callback_url,
        "enabled": item.enabled,
        "config_json": item.config_json or {},
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _serialize_rule(item: PlatformGatewayRouteRule) -> dict:
    return {
        "id": item.id,
        "connection_id": item.connection_id,
        "owner_user_id": item.owner_user_id,
        "rule_name": item.rule_name,
        "source_scope": item.source_scope,
        "source_id": item.source_id,
        "keyword": item.keyword,
        "conversation_id": item.conversation_id,
        "default_model": item.default_model,
        "execution_backend": item.execution_backend,
        "enabled": item.enabled,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _serialize_log(item: PlatformGatewayEventLog) -> dict:
    return {
        "id": item.id,
        "connection_id": item.connection_id,
        "rule_id": item.rule_id,
        "owner_user_id": item.owner_user_id,
        "platform_type": item.platform_type,
        "event_type": item.event_type,
        "source_id": item.source_id,
        "message_text": item.message_text,
        "status": item.status,
        "detail": item.detail or {},
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.get("/connections")
def list_connections(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    items = db.scalars(
        select(PlatformGatewayConnection)
        .where(PlatformGatewayConnection.owner_user_id == current_user.id)
        .order_by(desc(PlatformGatewayConnection.created_at))
    ).all()
    return {"items": [_serialize_connection(item) for item in items]}


@router.post("/connections")
def create_connection(
    payload: CreateGatewayConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    item = PlatformGatewayConnection(owner_user_id=current_user.id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"item": _serialize_connection(item)}


@router.patch("/connections/{connection_id}")
def update_connection(
    connection_id: str,
    payload: UpdateGatewayConnectionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    item = db.get(PlatformGatewayConnection, connection_id)
    if not item or item.owner_user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"item": _serialize_connection(item)}


@router.delete("/connections/{connection_id}")
def delete_connection(connection_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    item = db.get(PlatformGatewayConnection, connection_id)
    if not item or item.owner_user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    db.delete(item)
    db.commit()
    return {"deleted": True, "id": connection_id}


@router.get("/rules")
def list_rules(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    items = db.scalars(
        select(PlatformGatewayRouteRule)
        .where(PlatformGatewayRouteRule.owner_user_id == current_user.id)
        .order_by(desc(PlatformGatewayRouteRule.created_at))
    ).all()
    return {"items": [_serialize_rule(item) for item in items]}


@router.post("/rules")
def create_rule(
    payload: CreateGatewayRuleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    connection = db.get(PlatformGatewayConnection, payload.connection_id)
    if not connection or connection.owner_user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    if payload.conversation_id:
        conversation = db.get(Conversation, payload.conversation_id)
        if not conversation or conversation.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    item = PlatformGatewayRouteRule(owner_user_id=current_user.id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"item": _serialize_rule(item)}


@router.patch("/rules/{rule_id}")
def update_rule(
    rule_id: str,
    payload: UpdateGatewayRuleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    item = db.get(PlatformGatewayRouteRule, rule_id)
    if not item or item.owner_user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    updates = payload.model_dump(exclude_unset=True)
    if "conversation_id" in updates and updates["conversation_id"]:
        conversation = db.get(Conversation, updates["conversation_id"])
        if not conversation or conversation.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    for key, value in updates.items():
        setattr(item, key, value)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"item": _serialize_rule(item)}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    item = db.get(PlatformGatewayRouteRule, rule_id)
    if not item or item.owner_user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    db.delete(item)
    db.commit()
    return {"deleted": True, "id": rule_id}


@router.get("/events")
def list_events(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    items = db.scalars(
        select(PlatformGatewayEventLog)
        .where(PlatformGatewayEventLog.owner_user_id == current_user.id)
        .order_by(desc(PlatformGatewayEventLog.created_at))
        .limit(100)
    ).all()
    return {"items": [_serialize_log(item) for item in items]}


@router.get("/options")
def list_options(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    conversations = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == current_user.id)
        .order_by(desc(Conversation.updated_at))
    ).all()
    return {
        "platform_types": ["dingtalk", "feishu", "webhook", "wechat_work"],
        "conversations": [
            {"id": item.id, "title": item.title, "model": item.model_name, "execution_backend": item.execution_backend}
            for item in conversations
        ],
    }
