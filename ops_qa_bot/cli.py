import argparse
import asyncio
from pathlib import Path

from .bot import OpsQABot, format_tool_call


async def run_repl(docs_root: Path, show_tools: bool) -> None:
    print(f"运维文档问答机器人（文档根目录：{docs_root}）")
    print("输入问题后回车提问，空行或 Ctrl+C 退出。\n")

    async with OpsQABot(docs_root=docs_root) as bot:
        while True:
            try:
                question = await asyncio.to_thread(input, "你> ")
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                return

            question = question.strip()
            if not question:
                print("再见。")
                return

            print()
            printed_claude_prefix = False
            try:
                async for event in bot.ask(question):
                    if event["type"] == "tool":
                        if show_tools:
                            print(f"  → {format_tool_call(event['name'], event['input'])}")
                    elif event["type"] == "text":
                        if not printed_claude_prefix:
                            print("Claude> ", end="", flush=True)
                            printed_claude_prefix = True
                        print(event["text"], end="", flush=True)
                    elif event["type"] == "done":
                        print()
                        cost = event.get("cost_usd")
                        if cost is not None:
                            print(f"  [本轮成本 ${cost:.4f}]")
                        print()
            except KeyboardInterrupt:
                print("\n（已中断本次回答）\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="内部运维文档问答机器人")
    parser.add_argument(
        "--docs",
        default=str(Path(__file__).resolve().parent.parent / "docs"),
        help="文档根目录路径（默认：项目自带的 docs/）",
    )
    parser.add_argument(
        "--hide-tools",
        action="store_true",
        help="隐藏 agent 的工具调用日志",
    )
    args = parser.parse_args()

    asyncio.run(run_repl(Path(args.docs).resolve(), show_tools=not args.hide_tools))


if __name__ == "__main__":
    main()
