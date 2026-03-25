"""
Direct Claude API integration for the QUO VOIP connector.

Allows you to ask Claude questions about your call transcriptions using
Anthropic's tool-use API — without needing a running MCP server.

Example
-------
>>> from claude_integration import QUOClaudeAssistant
>>> assistant = QUOClaudeAssistant()
>>> response = assistant.ask("Summarise my last 5 customer calls and flag any complaints")
>>> print(response)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from quo_voip import QUOConfig, TranscriptionService
from quo_voip.models import TranscriptionStatus
from quo_mcp.server import TOOLS, QUOMCPHandler

logger = logging.getLogger(__name__)


class QUOClaudeAssistant:
    """
    Conversational assistant that combines Claude with QUO VOIP data.

    The assistant exposes QUO VOIP transcription tools to Claude via the
    Anthropic tool-use API.  Claude decides which tools to call based on
    the user's query and the connector fetches the live data.

    Parameters
    ----------
    config : QUOConfig, optional
        QUO VOIP + Anthropic configuration.  Reads from env vars if omitted.
    system_prompt : str, optional
        Override the default system prompt.
    """

    DEFAULT_SYSTEM_PROMPT = (
        "You are a helpful assistant with access to a company's QUO VOIP call "
        "transcription data. Use the available tools to fetch transcription data "
        "when needed. Be concise, accurate, and highlight key insights such as "
        "customer sentiment, action items, and recurring themes."
    )

    def __init__(
        self,
        config: Optional[QUOConfig] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        if not ANTHROPIC_AVAILABLE:
            raise ImportError(
                "The 'anthropic' package is required.\n"
                "Install it with: pip install anthropic"
            )

        self.config = config or QUOConfig.from_env()
        self.system_prompt = system_prompt or self.DEFAULT_SYSTEM_PROMPT
        self._handler = QUOMCPHandler(self.config)
        self._client = anthropic.Anthropic(api_key=self.config.anthropic_api_key)
        self._tools = self._build_tool_specs()

    # ── Public interface ──────────────────────────────────────────────────────

    def ask(
        self,
        question: str,
        history: Optional[list[dict]] = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Ask a question about your QUO VOIP data.

        Parameters
        ----------
        question : str
            The user's question.
        history : list[dict], optional
            Prior conversation messages for multi-turn chat.
        max_tokens : int
            Maximum tokens for the response.

        Returns
        -------
        str
            Claude's final answer (after all tool calls are resolved).
        """
        messages = list(history or [])
        messages.append({"role": "user", "content": question})

        return self._agentic_loop(messages, max_tokens)

    def chat(self) -> None:
        """
        Start an interactive REPL session with the QUO VOIP assistant.
        Type 'exit' or 'quit' to stop.
        """
        print("QUO VOIP Assistant (powered by Claude)")
        print("Type 'exit' to quit.\n")
        history: list[dict] = []

        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            if user_input.lower() in ("exit", "quit", "q"):
                print("Goodbye!")
                break
            if not user_input:
                continue

            response = self.ask(user_input, history=history)
            print(f"\nAssistant: {response}\n")

            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": response})

    # ── Internals ─────────────────────────────────────────────────────────────

    def _agentic_loop(self, messages: list[dict], max_tokens: int) -> str:
        """Run the Claude tool-use loop until a final text response is produced."""
        while True:
            response = self._client.messages.create(
                model=self.config.claude_model,
                max_tokens=max_tokens,
                system=self.system_prompt,
                tools=self._tools,
                messages=messages,
            )

            logger.debug("Claude stop_reason=%s", response.stop_reason)

            if response.stop_reason == "end_turn":
                # Extract the final text block
                for block in response.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if response.stop_reason == "tool_use":
                # Append Claude's tool-call message
                messages.append({"role": "assistant", "content": response.content})

                # Execute each tool and collect results
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info("Claude calling tool: %s(%s)", block.name, block.input)
                        result_text = self._handler.handle(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        })

                messages.append({"role": "user", "content": tool_results})
                continue

            # Unexpected stop reason
            logger.warning("Unexpected stop_reason: %s", response.stop_reason)
            break

        return ""

    def _build_tool_specs(self) -> list[dict[str, Any]]:
        """Convert MCP tool definitions to Anthropic tool format."""
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["inputSchema"],
            }
            for t in TOOLS
        ]
