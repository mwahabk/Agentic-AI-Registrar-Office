"""Agentic AI Registrar Office - interactive CLI.

    python main.py                         # prompts for role and ID
    python main.py --role student --id S1001
    python main.py --role admin -q "Which students are on the brink of dismissal?"
"""

import argparse
import asyncio

from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, TextBlock, ToolUseBlock

from registrar.agents import build_options
from registrar.db import DEFAULT_DB, connect
from registrar.service import ROLES, PermissionDenied, Registrar


def show(msg, verbose: bool) -> None:
    if isinstance(msg, AssistantMessage):
        # text next to a tool call is the coordinator's working note, not the answer
        quiet = msg.parent_tool_use_id is not None or any(isinstance(b, ToolUseBlock) for b in msg.content)
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                if block.name == "Agent":
                    print(f"  -> delegating to {block.input.get('subagent_type')}: {block.input.get('description', '')}")
                elif verbose:
                    print(f"     [tool] {block.name.split('__')[-1]} {block.input}")
            elif isinstance(block, TextBlock) and not quiet:
                print(f"\nRegistrar: {block.text}")
    elif isinstance(msg, ResultMessage) and msg.total_cost_usd:
        print(f"  (cost ${msg.total_cost_usd:.4f})")


async def run(options, first_question: str | None, verbose: bool) -> None:
    async with ClaudeSDKClient(options=options) as client:
        question = first_question
        while True:
            if question is None:
                try:
                    question = input("\nYou: ").strip()
                except EOFError:
                    break
            if question.lower() in {"quit", "exit"}:
                break
            if question:
                await client.query(question)
                async for msg in client.receive_response():
                    show(msg, verbose)
            if first_question:
                break  # one-shot mode
            question = None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", choices=ROLES)
    ap.add_argument("--id", help="Student ID (S1001...) or professor ID (P01...)")
    ap.add_argument("-q", "--question", help="Ask one question and exit")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("-v", "--verbose", action="store_true", help="Show every tool call")
    args = ap.parse_args()

    conn = connect(args.db)
    role = args.role or input(f"Role {ROLES}: ").strip().lower()
    uid = args.id if role == "admin" or args.id else input("Your ID: ").strip().upper()
    try:
        user = Registrar.login(conn, role, uid)
    except PermissionDenied as e:
        raise SystemExit(f"Login failed: {e}")
    print(f"Welcome, {user.name} ({user.role}). Type 'quit' to exit.")
    asyncio.run(run(build_options(conn, user), args.question, args.verbose))


if __name__ == "__main__":
    main()
