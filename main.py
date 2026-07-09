"""CLI entry point for the Google Drive Foundry agent.

Commands:
    python main.py auth          Run the Google OAuth consent flow and cache a token.
    python main.py ask "..."     Ask a single question and print the answer.
    python main.py chat          Interactive chat loop (default when no command given).
    python main.py create-agent  Create/persist the agent version for the Foundry portal.
"""

from __future__ import annotations

import argparse

from src.config import Settings
from src.foundry_agent import GoogleDriveAgent
from src.google_auth import load_credentials


def cmd_auth(settings: Settings) -> None:
    creds = load_credentials(
        settings.google_client_secrets_file,
        settings.google_token_file,
        oauth_port=settings.google_oauth_port,
        interactive=True,
    )
    print("\nGoogle authentication successful.")
    print(f"  Token cached at : {settings.google_token_file}")
    print(f"  Granted scopes  : {' '.join(creds.scopes or [])}")
    print("\nYou can now run:  python main.py chat")


def cmd_ask(settings: Settings, question: str) -> None:
    with GoogleDriveAgent(settings) as agent:
        print(f"\nQ: {question}")
        print(f"A: {agent.ask(question)}")


def cmd_create_agent(settings: Settings) -> None:
    """Create/persist the agent version (for use from the Foundry portal)."""
    agent = GoogleDriveAgent(settings)
    try:
        handle = agent.create_agent()
        print("\nAgent created and persisted in your Foundry project.")
        print(f"  Name    : {handle.name}")
        print(f"  Version : v{handle.version}")
        print("\nOpen it in the Foundry portal (Agents) and start chatting.")
    finally:
        agent.close()


def cmd_chat(settings: Settings) -> None:
    print("Google Drive agent ready. Ask about your documents. Type 'exit' to quit.\n")
    with GoogleDriveAgent(settings) as agent:
        while True:
            try:
                question = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not question:
                continue
            if question.lower() in {"exit", "quit", ":q"}:
                break
            print(f"agent> {agent.ask(question)}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundry agent connected to Google Drive via MCP."
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("auth", help="Run the Google OAuth flow and cache a token.")
    ask_parser = sub.add_parser("ask", help="Ask a single question.")
    ask_parser.add_argument("question", nargs="+", help="The question to ask.")
    sub.add_parser("chat", help="Interactive chat loop (default).")
    sub.add_parser(
        "create-agent",
        help="Create/persist the agent version for use from the Foundry portal.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()

    if args.command == "auth":
        cmd_auth(settings)
    elif args.command == "ask":
        cmd_ask(settings, " ".join(args.question))
    elif args.command == "create-agent":
        cmd_create_agent(settings)
    else:
        cmd_chat(settings)


if __name__ == "__main__":
    main()
