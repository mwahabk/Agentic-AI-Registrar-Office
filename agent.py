"""A starter Claude Agent SDK agent with custom in-process tools.

Run:  .venv/bin/python agent.py
Swap the example tools below for calls to your own APIs / database.
"""

import asyncio
from typing import Annotated

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

# --- Example "backend" (replace with your real data source) -----------------
PRODUCTS = {
    "SKU-1": {"name": "Coffee mug", "price": 12.0, "stock": 40},
    "SKU-2": {"name": "T-shirt", "price": 20.0, "stock": 0},
    "SKU-3": {"name": "Notebook", "price": 6.5, "stock": 120},
}
ORDERS: list[dict] = []


def text(s: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], "is_error": is_error}


# --- Custom tools --------------------------------------------------------------
@tool("search_products", "Search the product catalog by name (case-insensitive).",
      {"query": Annotated[str, "Text to match in product names; empty lists all"]})
async def search_products(args):
    q = args["query"].lower()
    hits = [f"{sku}: {p['name']} ${p['price']:.2f} (stock {p['stock']})"
            for sku, p in PRODUCTS.items() if q in p["name"].lower()]
    return text("\n".join(hits) or "No products found.")


@tool("place_order", "Place an order for a product. Fails if stock is insufficient.",
      {"sku": Annotated[str, "Product SKU, e.g. SKU-1"],
       "quantity": Annotated[int, "Number of units, at least 1"]})
async def place_order(args):
    product = PRODUCTS.get(args["sku"])
    qty = args["quantity"]
    if product is None:
        return text(f"Unknown SKU {args['sku']}", is_error=True)
    if qty < 1 or product["stock"] < qty:
        return text(f"Cannot order {qty}; only {product['stock']} in stock.", is_error=True)
    product["stock"] -= qty
    order = {"id": len(ORDERS) + 1, "sku": args["sku"], "qty": qty,
             "total": round(qty * product["price"], 2)}
    ORDERS.append(order)
    return text(f"Order #{order['id']} placed: {qty} x {product['name']} = ${order['total']:.2f}")


server = create_sdk_mcp_server(name="shop", tools=[search_products, place_order])

options = ClaudeAgentOptions(
    model="claude-opus-5-5",
    system_prompt=(
        "You are a helpful shop assistant. Use the tools to look up products and "
        "place orders. Confirm the item and quantity with the user before ordering."
    ),
    mcp_servers={"shop": server},
    tools=[],  # disable built-in Claude Code tools (Bash, Read, ...) - only ours
    allowed_tools=["mcp__shop__search_products", "mcp__shop__place_order"],
    max_turns=10,
)


async def main():
    async with ClaudeSDKClient(options=options) as client:
        print("Shop agent ready. Type 'quit' to exit.")
        while True:
            user = input("\nYou: ").strip()
            if user.lower() in {"quit", "exit"}:
                break
            if not user:
                continue
            await client.query(user)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"Agent: {block.text}")
                        elif isinstance(block, ToolUseBlock):
                            print(f"  [tool] {block.name} {block.input}")
                elif isinstance(msg, ResultMessage) and msg.total_cost_usd:
                    print(f"  (cost ${msg.total_cost_usd:.4f})")


if __name__ == "__main__":
    asyncio.run(main())
