from pydantic import BaseModel
from typing import Optional


class AppointmentCreate(BaseModel):
    title: str
    description: str = ""
    start_time: str
    end_time: str


class AppointmentReschedule(BaseModel):
    appointment_id: int
    new_start_time: str
    new_end_time: str


class AppointmentResponse(BaseModel):
    id: int
    barbershop_id: int
    title: str
    description: str
    start_time: str
    end_time: str
    status: str
    created_at: str
    updated_at: str


class MessageRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None
    barbershop_id: Optional[int] = None


class MessageResponse(BaseModel):
    reply: str
    thread_id: str


# ── Auth ────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    token: str
    barbershop_id: int
    name: str
    email: str
    whatsapp_number: Optional[str] = None
