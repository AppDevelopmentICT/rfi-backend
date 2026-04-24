import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from app.config import API_AUTH_SECRET
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
