"""Terminal chat interface.

Usage:
    python -m emporio                      # new conversation
    python -m emporio --session <id>       # resume a saved conversation
    python -m emporio --debug              # show tool calls (what the agent consulted)
    python -m emporio --list               # list saved conversations
"""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from . import config

console = Console()

GREETING = (
    "Oi! Eu sou a Lia, assistente virtual da Empório da Música 🎸\n"
    "Posso te ajudar com preços e disponibilidade de instrumentos, status do seu pedido, "
    "formas de pagamento, trocas, entregas e o que mais precisar. Como posso ajudar?"
)
HELP = "Comandos: /debug (liga/desliga rastro de ferramentas) · /novo (nova conversa) · /sair"


def _print_lia(text: str) -> None:
    # WhatsApp-style *bold* -> markdown **bold** for terminal rendering.
    console.print(Panel(Markdown(text.replace("*", "**").replace("****", "**")), title="Lia", title_align="left", border_style="cyan"))


def _print_tool(call) -> None:
    args = json.dumps(call.input, ensure_ascii=False)
    status = "[red]erro[/red]" if call.is_error else "[green]ok[/green]"
    preview = call.output if len(call.output) < 240 else call.output[:240] + "…"
    console.print(f"  [dim]🔧 {call.name}({args}) → {status}[/dim]")
    console.print(f"  [dim]   {preview}[/dim]")


def _list_sessions() -> None:
    folder = config.RUNTIME_DIR / "conversations"
    files = sorted(folder.glob("*.json")) if folder.exists() else []
    if not files:
        console.print("Nenhuma conversa salva.")
        return
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        first = next((t["text"] for t in data.get("transcript", []) if t["role"] == "user"), "")
        console.print(f"{data['session_id']}  [dim]{data.get('updated_at', '')}[/dim]  {first[:60]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat com a Lia, assistente da Empório da Música")
    parser.add_argument("--session", help="ID de uma conversa salva para continuar")
    parser.add_argument("--debug", action="store_true", help="mostra as ferramentas consultadas")
    parser.add_argument("--list", action="store_true", help="lista conversas salvas")
    args = parser.parse_args(argv)

    if args.list:
        _list_sessions()
        return 0

    from .agent import EmporioAgent  # imported late so --list works without credentials

    try:
        agent = EmporioAgent(session_id=args.session)
    except Exception as exc:  # most commonly: missing API key
        console.print(f"[red]Não foi possível iniciar o agente:[/red] {exc}")
        console.print("Configure ANTHROPIC_API_KEY no arquivo .env (veja .env.example).")
        return 1

    debug = args.debug
    console.print(f"[dim]Sessão {agent.session_id} · modelo {agent.model} · {HELP}[/dim]")

    if agent.transcript:
        console.print("[dim]— continuando conversa salva —[/dim]")
        for entry in agent.transcript[-6:]:
            if entry["role"] == "user":
                console.print(f"[bold]Você:[/bold] {entry['text']}")
            elif entry["role"] == "assistant":
                _print_lia(entry["text"])
    else:
        _print_lia(GREETING)

    while True:
        try:
            user_text = console.input("[bold]Você:[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not user_text:
            continue
        if user_text.lower() in ("/sair", "/exit", "sair"):
            break
        if user_text.lower() == "/debug":
            debug = not debug
            console.print(f"[dim]debug {'ligado' if debug else 'desligado'}[/dim]")
            continue
        if user_text.lower() == "/novo":
            agent = EmporioAgent()
            console.print(f"[dim]Nova sessão {agent.session_id}[/dim]")
            _print_lia(GREETING)
            continue

        try:
            with console.status("Lia está digitando…", spinner="dots"):
                result = agent.reply(user_text)
        except TypeError as exc:
            if "authentication" not in str(exc):
                raise
            console.print("[red]Credencial da Anthropic não encontrada.[/red]")
            console.print("Crie o arquivo .env com ANTHROPIC_API_KEY=sk-ant-... (veja .env.example) e rode de novo.")
            return 1
        if debug:
            for call in result.tool_calls:
                _print_tool(call)
            console.print(
                f"  [dim]tokens: in={result.input_tokens} (cache={result.cache_read_tokens}) out={result.output_tokens}[/dim]"
            )
        _print_lia(result.reply)

    console.print(f"[dim]Conversa salva. Para continuar: python -m emporio --session {agent.session_id}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
