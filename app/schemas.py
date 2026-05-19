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


# ── Services ─────────────────────────────────────────────────

class ServiceCreate(BaseModel):
    name: str
    duration_minutes: int = 60
    price: float = 0.0


class ServiceUpdate(BaseModel):
    name: Optional[str] = None
    duration_minutes: Optional[int] = None
    price: Optional[float] = None
    active: Optional[bool] = None


class ServiceResponse(BaseModel):
    id: int
    barbershop_id: int
    name: str
    duration_minutes: int
    price: float
    active: bool
    created_at: str


class AuthResponse(BaseModel):
    token: str
    barbershop_id: int
    name: str
    email: str
    whatsapp_number: Optional[str] = None
