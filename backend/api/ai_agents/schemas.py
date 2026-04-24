from pydantic import BaseModel, field_validator
from typing import Any


class MessageIn(BaseModel):
    role: str    # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    agent_id: str
    messages: list[MessageIn]        # full conversation history
    provider: str | None = None      # override default provider
    model: str | None = None         # override default model
    context: dict[str, Any] = {}     # optional structured data (portfolio, DCF result, …)

    @field_validator("agent_id")
    @classmethod
    def valid_agent(cls, v: str) -> str:
        from ai.agents import list_agents
        ids = [a["id"] for a in list_agents()]
        if v not in ids:
            raise ValueError(f"agent_id must be one of {ids}")
        return v

    @field_validator("messages")
    @classmethod
    def at_least_one_message(cls, v: list[MessageIn]) -> list[MessageIn]:
        if not v:
            raise ValueError("messages must not be empty")
        return v


class AgentInfo(BaseModel):
    id: str
    name: str
    description: str
    default_provider: str
