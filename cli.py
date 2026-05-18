import os
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

API_URL = os.environ.get("PATO_API_URL", "http://localhost:8000")
console = Console()


def main():
    console.print(Panel.fit("🦆 PatoAgenda AI — Agendamentos Inteligentes", style="bold cyan"))
    console.print("Type your message. Use [bold]/quit[/bold] to exit.\n")

    thread_id = None

    while True:
        user_input = console.input("[bold green]You:[/bold green] ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("/quit", "/exit", "/q"):
            break
        if user_input.lower() == "/appointments":
            show_all_appointments()
            continue

        try:
            with httpx.Client(base_url=API_URL, timeout=30) as http:
                resp = http.post("/chat", json={
                    "message": user_input,
                    "thread_id": thread_id,
                })
                resp.raise_for_status()
                data = resp.json()

            thread_id = data["thread_id"]
            reply = data["reply"]
            panel = Panel(Markdown(reply), title="🤖 PatoAgenda AI", border_style="cyan")
            console.print(panel)
            console.print()

        except httpx.ConnectError:
            console.print("[red]Could not connect to the backend. Is it running?[/red]")
            console.print("[yellow]Start it with: uvicorn app.main:app --reload[/yellow]\n")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]\n")


def show_all_appointments():
    try:
        with httpx.Client(base_url=API_URL, timeout=10) as http:
            resp = http.get("/appointments")
            resp.raise_for_status()
            appointments = resp.json()
    except Exception as e:
        console.print(f"[red]Could not fetch appointments: {e}[/red]\n")
        return

    if not appointments:
        console.print("[yellow]No appointments found.[/yellow]\n")
        return

    console.print("[bold]All Appointments:[/bold]")
    for a in appointments:
        status_color = {
            "scheduled": "green",
            "rescheduled": "yellow",
            "cancelled": "red",
        }.get(a["status"], "white")
        console.print(
            f"  [bold]#{a['id']}[/bold] {a['title']} | "
            f"{a['start_time']} → {a['end_time']} | "
            f"[{status_color}]{a['status']}[/{status_color}]"
        )
    console.print()


if __name__ == "__main__":
    main()
