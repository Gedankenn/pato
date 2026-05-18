import json
import os
import re
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

from fastapi import FastAPI, HTTPException
from app.schemas import (
    AppointmentCreate,
    AppointmentReschedule,
    AppointmentResponse,
    MessageRequest,
    MessageResponse,
)
from app import database as db
from app.llm import SYSTEM_PROMPT

app = FastAPI(title="Pato - Appointment Chatbot")


def get_openai():
    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY", "not-needed")
    kwargs = {"api_key": api_key, "timeout": 300}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


@app.on_event("startup")
def startup():
    db.init_db()


def format_appointment(a: dict) -> str:
    return (
        f"[#{a['id']}] {a['title']} - {a['start_time']} to {a['end_time']} "
        f"({a['status']})"
    )


def execute_action(action: str, params: dict) -> str:
    if action == "create_appointment":
        appt_id = db.create_appointment(
            title=params["title"],
            description=params.get("description", ""),
            start_time=params["start_time"],
            end_time=params["end_time"],
        )
        a = db.get_appointment(appt_id)
        return f"Created: {format_appointment(a)}"

    elif action == "list_appointments":
        appointments = db.list_appointments(status=params.get("status"))
        if not appointments:
            return "No appointments found."
        return "\n".join(format_appointment(a) for a in appointments)

    elif action == "get_appointment":
        a = db.get_appointment(params["appointment_id"])
        if not a:
            return f"Appointment #{params['appointment_id']} not found."
        return (
            f"Appointment #{a['id']}: {a['title']}\n"
            f"  Description: {a['description']}\n"
            f"  Start: {a['start_time']}\n"
            f"  End: {a['end_time']}\n"
            f"  Status: {a['status']}"
        )

    elif action == "reschedule_appointment":
        success = db.reschedule_appointment(
            params["appointment_id"],
            params["new_start_time"],
            params["new_end_time"],
        )
        if not success:
            return f"Appointment #{params['appointment_id']} not found."
        a = db.get_appointment(params["appointment_id"])
        return f"Rescheduled: {format_appointment(a)}"

    elif action == "cancel_appointment":
        success = db.cancel_appointment(params["appointment_id"])
        if not success:
            return f"Appointment #{params['appointment_id']} not found."
        a = db.get_appointment(params["appointment_id"])
        return f"Cancelled: {format_appointment(a)}"

    elif action == "reply":
        return None

    return f"Unknown action: {action}"


def extract_json(text: str) -> dict | None:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def call_llm(messages: list) -> str:
    response = get_openai().chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.1,
    )
    return response.choices[0].message.content or ""


@app.post("/chat", response_model=MessageResponse)
def chat(request: MessageRequest):
    thread_id = request.thread_id or f"thread_{datetime.utcnow().timestamp()}"

    history = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": request.message},
    ]

    for _ in range(5):
        raw = call_llm(history)
        parsed = extract_json(raw)

        if not parsed or not isinstance(parsed, dict):
            return MessageResponse(
                reply="I couldn't process that request. Could you rephrase?",
                thread_id=thread_id,
            )

        action = parsed.get("action", "reply")
        params = parsed.get("parameters", {})
        msg = parsed.get("message", "")

        result = execute_action(action, params)

        if action == "reply" or result is None:
            return MessageResponse(reply=msg, thread_id=thread_id)

        history.append({"role": "assistant", "content": raw})
        history.append({
            "role": "user",
            "content": f"The action was executed. Result:\n{result}",
        })

    return MessageResponse(
        reply="I've processed your request. Check the appointments list to see changes.",
        thread_id=thread_id,
    )


@app.get("/appointments", response_model=list[AppointmentResponse])
def list_appointments(status: str | None = None):
    return db.list_appointments(status=status)


@app.get("/appointments/{appointment_id}", response_model=AppointmentResponse)
def get_appointment(appointment_id: int):
    a = db.get_appointment(appointment_id)
    if not a:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return a


@app.post("/appointments", response_model=AppointmentResponse)
def create_appointment(body: AppointmentCreate):
    appt_id = db.create_appointment(
        title=body.title,
        description=body.description,
        start_time=body.start_time,
        end_time=body.end_time,
    )
    return db.get_appointment(appt_id)


@app.put("/appointments/{appointment_id}/reschedule", response_model=AppointmentResponse)
def reschedule_appointment(appointment_id: int, body: AppointmentReschedule):
    success = db.reschedule_appointment(
        appointment_id, body.new_start_time, body.new_end_time
    )
    if not success:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return db.get_appointment(appointment_id)


@app.delete("/appointments/{appointment_id}", response_model=AppointmentResponse)
def cancel_appointment(appointment_id: int):
    a = db.get_appointment(appointment_id)
    if not a:
        raise HTTPException(status_code=404, detail="Appointment not found")
    db.cancel_appointment(appointment_id)
    return db.get_appointment(appointment_id)
