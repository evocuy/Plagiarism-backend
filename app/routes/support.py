"""Student support ticket HTTP endpoints."""

from typing import Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.schemas import User
from app.routes.auth import get_current_user, require_admin
from app.services import support_service

router = APIRouter()


class TicketCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    category: str = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=4000)


class MessageCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=4000)


class TicketStatusUpdate(BaseModel):
    status: Literal["new", "in_progress", "waiting_student", "resolved"]


@router.get("/tickets")
def list_tickets(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return support_service.list_tickets(db, current_user)


@router.post("/tickets", status_code=status.HTTP_201_CREATED)
def create_ticket(
    request: TicketCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return support_service.create_ticket(db, current_user, request.category, request.subject, request.message)


@router.get("/tickets/{ticket_id}")
def get_ticket(
    ticket_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return support_service.get_ticket(db, current_user, ticket_id)


@router.post("/tickets/{ticket_id}/messages")
def add_message(
    ticket_id: int,
    request: MessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return support_service.add_message(db, current_user, ticket_id, request.message)


@router.patch("/tickets/{ticket_id}")
def update_ticket(
    ticket_id: int,
    request: TicketStatusUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return support_service.update_ticket(db, admin, ticket_id, request.status)
