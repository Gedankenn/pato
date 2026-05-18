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


class AppointmentCancel(BaseModel):
    appointment_id: int


class AppointmentResponse(BaseModel):
    id: int
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


class ToolCallResult(BaseModel):
    function: str
    arguments: dict
    result: str


class MessageResponse(BaseModel):
    reply: str
    thread_id: str
    action: Optional[ToolCallResult] = None
