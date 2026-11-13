"""HTTP surface of the "Ask the platform" assistant."""

import os

import anthropic
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src.assistant.chat import (
    Assistant,
    AssistantUnavailableError,
    ChatContext,
    ChatTurn,
)
from src.assistant.tools import AssistantTools
from src.webhooks import find_assessment

router = APIRouter(prefix="/api/assistant", tags=["assistant"])
ASSISTANT = Assistant(tools=AssistantTools(find_assessment=find_assessment))


class ChatRequest(BaseModel):
    """One question, optionally continuing a conversation."""

    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=100)
    context: ChatContext = Field(default_factory=ChatContext)


class AssistantStatus(BaseModel):
    """Whether credentials look configured, and which model answers."""

    configured: bool
    model: str


@router.get("/status", response_model=AssistantStatus)
def assistant_status() -> AssistantStatus:
    """Report whether Claude API credentials are set in the environment.

    Only environment variables are checked; a credential profile from
    ``ant auth login`` also works and is reported as not configured here.
    """
    configured = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    return AssistantStatus(configured=configured, model=ASSISTANT.model)


@router.post("/chat", response_model=ChatTurn)
async def chat(request: ChatRequest) -> ChatTurn:
    """Answer one question from the platform's evidence (read-only)."""
    try:
        return await run_in_threadpool(
            ASSISTANT.ask, request.message, request.context, request.conversation_id
        )
    except AssistantUnavailableError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except anthropic.RateLimitError as error:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, detail="Claude is rate limiting; try again shortly."
        ) from error
    except anthropic.APIConnectionError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Could not reach Claude.") from error
    except anthropic.APIStatusError as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"Claude returned an error ({error.status_code})."
        ) from error
