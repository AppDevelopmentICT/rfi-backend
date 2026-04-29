import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import JSONResponse
from app.config import API_AUTH_SECRET, OLLAMA_API, OLLAMA_MODEL
from app.schemas.rfp_schema import GenerateTechnicalContentRequest
from app.services.rfp.generator import stream_technical_content, stream_adjust_content

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/rfp", tags=["RFP"])


@router.websocket("/ws/generate-technical")
async def ws_generate_technical(
    websocket: WebSocket,
    token: str = Query(default=""),
):
    """WebSocket endpoint for streaming RFP Chapter 3 generation + adjustment.

    Flow:
      1. Client connects with ?token=<bearer_token>
      2. Client sends JSON: {"product": "...", "rfp": true}
      3. Server streams chunks → complete
      4. Client can then send: {"product": "...", "rfp": true, "adjust": true, "content": "...", "additionalContext": "make it shorter"}
      5. Server streams adjusted content → complete
      6. Repeat step 4-5 as many times as needed
      7. Client disconnects when done
    """
    if token != API_AUTH_SECRET:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    logger.info("WebSocket client connected for RFP generation")

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                payload = json.loads(raw)
                request = GenerateTechnicalContentRequest(**payload)
            except (json.JSONDecodeError, Exception) as e:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Invalid request payload: {str(e)}",
                }))
                continue

            if not request.rfp:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "rfp must be true for RFP generation",
                }))
                continue

            # ── Adjust mode ───────────────────────────────────────────
            if request.adjust:
                if not request.content:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "content is required when adjust=true",
                    }))
                    continue

                logger.info(f"Adjusting RFP content for product: {request.product}")

                async for message in stream_adjust_content(
                    product=request.product,
                    content=request.content,
                    additional_context=request.additionalContext,
                ):
                    await websocket.send_text(json.dumps(message, ensure_ascii=False))
                    if message.get("type") == "error":
                        break

            # ── Generate mode ─────────────────────────────────────────
            else:
                logger.info(f"Generating RFP technical content for product: {request.product}")

                async for message in stream_technical_content(
                    product=request.product,
                    project_name=request.projectName,
                    project_description=request.projectDescription,
                    additional_context=request.additionalContext,
                ):
                    await websocket.send_text(json.dumps(message, ensure_ascii=False))
                    if message.get("type") == "error":
                        break

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"Server error: {str(e)}",
            }))
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


async def _classify_prompt_with_ollama(prompt: str) -> dict:
    """Use Ollama to classify if a prompt is vague or specific."""
    import httpx
    import re

    system_prompt = (
        "You are an assistant that classifies user prompts for RFP document editing. "
        "Determine if the prompt is 'vague' or 'specific'.\n\n"
        "Vague: ONLY single emotional words with zero context ('too bad', 'ugly', 'not good').\n\n"
        "Specific: ANY prompt that mentions a section/topic, language, length, format, or action. "
        "Examples: 'do it', 'migration', 'semuanya', 'make it shorter', 'make it in korean', "
        "'all of them', 'make it in points', 'too long' are ALL specific when context exists.\n\n"
        "CRITICAL RULES:\n"
        "1. If the prompt contains ANY technical term, section name, or action word → classify as 'specific'.\n"
        "2. If the prompt is a command like 'do it', 'apply', 'execute', 'yes', 'ok' → classify as 'specific'.\n"
        "3. Single words like 'semuanya', 'all', 'migration', 'security' in a follow-up context → 'specific'.\n"
        "4. ONLY classify as 'vague' if there is truly zero actionable information AND no prior context exists.\n"
        "5. When in doubt, classify as 'specific' and execute.\n\n"
        "If vague (rare), generate exactly ONE (1) short clarifying question. "
        "Use ENGLISH or INDONESIAN matching the user's language.\n\n"
        "Respond ONLY with JSON: {\"classification\": \"vague\"|\"specific\", \"questions\": []}"
    )

    user_prompt = f"Classify this prompt:\n\n\"{prompt}\""

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": user_prompt,
        "system": system_prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 256,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            logger.info(f"Calling Ollama at {OLLAMA_API}/api/generate with model {OLLAMA_MODEL}")
            response = await client.post(f"{OLLAMA_API}/api/generate", json=payload)
            response.raise_for_status()
            data = response.json()
            result_text = data.get("response", "")
            logger.info(f"Ollama response: {result_text[:200]}")

            json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            logger.warning("No JSON found in Ollama response")
            return {"classification": "vague", "questions": ["Can you specify what needs improvement?"]}
    except httpx.ConnectError as e:
        logger.error(f"Cannot connect to Ollama at {OLLAMA_API}: {e}")
        return {"classification": "vague", "questions": ["Can you specify what changes you'd like?"]}
    except httpx.HTTPStatusError as e:
        logger.error(f"Ollama HTTP error: {e.response.status_code} - {e.response.text}")
        return {"classification": "vague", "questions": ["Can you specify what changes you'd like?"]}
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Ollama JSON response: {e}")
        return {"classification": "vague", "questions": ["Can you specify what changes you'd like?"]}
    except Exception as e:
        logger.error(f"Unexpected error classifying prompt: {e}", exc_info=True)
        return {"classification": "vague", "questions": ["Can you specify what changes you'd like?"]}


@router.post("/classify-adjust-prompt")
async def classify_adjust_prompt(body: dict):
    """Classify a user's adjustment prompt as vague or specific.

    Returns:
        - classification: "vague" or "specific"
        - questions: list of clarifying questions (if vague)
    """
    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    result = await _classify_prompt_with_ollama(prompt)
    return JSONResponse(content=result)
