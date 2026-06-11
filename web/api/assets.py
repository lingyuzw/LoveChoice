from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request

from api.dependencies import require_local_service_control
from media.assets import normalize_channel
from media.sticker_processing import save_sticker_image
from media.sticker_vision import StickerVisionAnalyzer


def selected_stickers(request: Request, sticker_ids: list[str] | None, include_filtered: bool, filters: dict) -> list[dict]:
    library = request.app.state.sticker_library
    if sticker_ids:
        wanted = {str(item or "").strip() for item in sticker_ids if str(item or "").strip()}
        return [item for item in library.load() if item.get("id") in wanted]
    if include_filtered:
        return library.list(
            status=str(filters.get("status") or ""),
            emotion=str(filters.get("emotion") or ""),
            query=str(filters.get("q") or filters.get("query") or ""),
        )
    return []


async def analyze_sticker(request: Request, sticker: dict) -> dict:
    analyzer = StickerVisionAnalyzer(request.app.state.settings)
    analysis = await analyzer.analyze(Path(sticker.get("send_path") or sticker.get("path") or ""), mime="image/png")
    return request.app.state.sticker_library.update(
        sticker["id"],
        {**analysis, "review_status": "pending", "enabled": False, "error": ""},
    )


def create_assets_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/assets/avatar")
    async def upload_avatar(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            return {"asset": request.app.state.avatar_store.save_data_url(str((payload or {}).get("data_url") or ""))}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/assets/chat-image")
    async def upload_chat_image(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            asset = request.app.state.chat_image_store.save_data_url(
                str((payload or {}).get("data_url") or ""),
                max_mb=float(getattr(request.app.state.settings, "vision_max_image_mb", 8.0)),
            )
            return {"asset": asset}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/stickers")
    async def stickers(request: Request, status: str = "", emotion: str = "", q: str = ""):
        require_local_service_control(request)
        return {"stickers": request.app.state.sticker_store.list(status=status, emotion=emotion, query=q)}

    @router.post("/api/stickers")
    async def upload_sticker(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        payload = payload or {}
        try:
            sticker = request.app.state.sticker_store.add_data_url(
                str(payload.get("data_url") or ""),
                tag=str(payload.get("tag") or "默认"),
                name=str(payload.get("name") or ""),
                channels=payload.get("channels") or payload.get("channel") or "all",
            )
            return {"sticker": sticker, "stickers": request.app.state.sticker_store.list()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/stickers/batch")
    async def upload_sticker_batch(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        payload = payload or {}
        files = payload.get("files")
        if not isinstance(files, list) or not files:
            raise HTTPException(status_code=400, detail="files is required")
        channels = payload.get("channels") or payload.get("channel") or "all"
        analyzer = StickerVisionAnalyzer(request.app.state.settings)
        results = []
        for index, file_item in enumerate(files[:80]):
            if not isinstance(file_item, dict):
                continue
            name = str(file_item.get("name") or f"sticker_{index + 1}")
            data_url = str(file_item.get("data_url") or "")
            try:
                preview = request.app.state.sticker_library.add_upload(data_url=data_url, name=name, channels=channels)
                if preview.get("duplicate"):
                    results.append({"ok": True, "duplicate": True, "analyzed": False, "sticker": preview})
                    continue

                image_path = preview.get("send_path") or preview.get("path")
                try:
                    analysis = await analyzer.analyze(Path(image_path), mime="image/png")
                except Exception as exc:
                    warning = f"自动识别失败：{exc}"
                    sticker = request.app.state.sticker_library.update(
                        preview["id"],
                        {"review_status": "pending", "enabled": False, "error": warning},
                    )
                    results.append({"ok": True, "analyzed": False, "warning": warning, "vision_model": getattr(request.app.state.settings, "sticker_vision_model", ""), "sticker": sticker})
                    continue

                sticker = request.app.state.sticker_library.update(
                    preview["id"],
                    {**analysis, "review_status": "pending", "enabled": False, "error": ""},
                )
                results.append({"ok": True, "analyzed": True, "vision_model": getattr(request.app.state.settings, "sticker_vision_model", ""), "sticker": sticker})
            except ValueError as exc:
                results.append({"ok": False, "name": name, "error": str(exc)})
            except Exception as exc:
                results.append({"ok": False, "name": name, "error": str(exc)})
        return {"ok": True, "results": results, "stickers": request.app.state.sticker_store.list()}

    @router.patch("/api/stickers/{sticker_id}")
    async def update_sticker(sticker_id: str, request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            sticker = request.app.state.sticker_library.update(sticker_id, payload or {})
            return {"sticker": sticker, "stickers": request.app.state.sticker_store.list()}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Sticker not found") from exc

    @router.post("/api/stickers/{sticker_id}/approve")
    async def approve_sticker(sticker_id: str, request: Request):
        require_local_service_control(request)
        try:
            sticker = request.app.state.sticker_library.approve(sticker_id)
            return {"sticker": sticker, "stickers": request.app.state.sticker_store.list()}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Sticker not found") from exc

    @router.post("/api/stickers/{sticker_id}/reanalyze")
    async def reanalyze_sticker(sticker_id: str, request: Request):
        require_local_service_control(request)
        sticker = next((item for item in request.app.state.sticker_library.load() if item.get("id") == sticker_id), None)
        if not sticker:
            raise HTTPException(status_code=404, detail="Sticker not found")
        try:
            analyzer = StickerVisionAnalyzer(request.app.state.settings)
            analysis = await analyzer.analyze(Path(sticker.get("send_path") or sticker.get("path") or ""), mime="image/png")
            updated = request.app.state.sticker_library.update(sticker_id, {**analysis, "review_status": "pending", "enabled": False, "error": ""})
            return {"sticker": updated, "stickers": request.app.state.sticker_store.list()}
        except Exception as exc:
            updated = request.app.state.sticker_library.update(sticker_id, {"review_status": "pending", "enabled": False, "error": f"自动识别失败：{exc}"})
            return {"sticker": updated, "stickers": request.app.state.sticker_store.list()}

    @router.post("/api/stickers/vision-test")
    async def test_sticker_vision(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        payload = payload or {}
        sticker_id = str(payload.get("sticker_id") or "").strip()
        data_url = str(payload.get("data_url") or "")
        try:
            if sticker_id:
                sticker = next((item for item in request.app.state.sticker_library.load() if item.get("id") == sticker_id), None)
                if not sticker:
                    raise HTTPException(status_code=404, detail="Sticker not found")
                image_path = Path(sticker.get("send_path") or sticker.get("path") or "")
            else:
                if not data_url:
                    raise HTTPException(status_code=400, detail="sticker_id or data_url is required")
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    processed = save_sticker_image(
                        data_url=data_url,
                        original_dir=root / "originals",
                        processed_dir=root / "processed",
                        send_dir=root / "send",
                        thumbnail_dir=root / "thumbs",
                        name_hint="vision_test",
                    )
                    analyzer = StickerVisionAnalyzer(request.app.state.settings)
                    result = await analyzer.test(processed.send_path, mime="image/png")
                    return {
                        **result,
                        "vision_model": getattr(request.app.state.settings, "sticker_vision_model", ""),
                        "vision_url": getattr(request.app.state.settings, "sticker_vision_url", ""),
                    }
            analyzer = StickerVisionAnalyzer(request.app.state.settings)
            result = await analyzer.test(image_path, mime="image/png")
            return {
                **result,
                "vision_model": getattr(request.app.state.settings, "sticker_vision_model", ""),
                "vision_url": getattr(request.app.state.settings, "sticker_vision_url", ""),
            }
        except HTTPException:
            raise
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "vision_model": getattr(request.app.state.settings, "sticker_vision_model", ""),
                "vision_url": getattr(request.app.state.settings, "sticker_vision_url", ""),
            }

    @router.post("/api/stickers/bulk")
    async def bulk_sticker_action(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        payload = payload or {}
        action = str(payload.get("action") or "").strip().lower()
        sticker_ids = payload.get("ids") if isinstance(payload.get("ids"), list) else []
        include_filtered = bool(payload.get("include_filtered"))
        filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
        targets = selected_stickers(request, sticker_ids, include_filtered, filters)
        if not targets:
            raise HTTPException(status_code=400, detail="no stickers selected")

        results = []
        if action == "reanalyze":
            for sticker in targets[:200]:
                try:
                    updated = await analyze_sticker(request, sticker)
                    results.append({"ok": True, "id": sticker.get("id"), "sticker": updated})
                except Exception as exc:
                    updated = None
                    try:
                        updated = request.app.state.sticker_library.update(
                            str(sticker.get("id")),
                            {"review_status": "pending", "enabled": False, "error": f"自动识别失败：{exc}"},
                        )
                    except Exception:
                        pass
                    results.append({"ok": False, "id": sticker.get("id"), "error": str(exc), "sticker": updated})
        elif action == "approve":
            for sticker in targets[:500]:
                try:
                    updated = request.app.state.sticker_library.approve(str(sticker.get("id")))
                    results.append({"ok": True, "id": sticker.get("id"), "sticker": updated})
                except Exception as exc:
                    results.append({"ok": False, "id": sticker.get("id"), "error": str(exc)})
        elif action == "delete":
            for sticker in targets[:500]:
                sticker_id = str(sticker.get("id") or "")
                try:
                    results.append({"ok": request.app.state.sticker_library.delete(sticker_id), "id": sticker_id})
                except Exception as exc:
                    results.append({"ok": False, "id": sticker_id, "error": str(exc)})
        else:
            raise HTTPException(status_code=400, detail="unsupported bulk action")

        return {
            "ok": True,
            "action": action,
            "count": len(results),
            "success": sum(1 for item in results if item.get("ok")),
            "failed": sum(1 for item in results if not item.get("ok")),
            "results": results,
            "stickers": request.app.state.sticker_store.list(
                status=str(filters.get("status") or ""),
                emotion=str(filters.get("emotion") or ""),
                query=str(filters.get("q") or filters.get("query") or ""),
            ),
        }

    @router.post("/api/stickers/test")
    async def test_sticker(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        payload = payload or {}
        channel = normalize_channel(str(payload.get("channel") or "web"))
        user_text = str(payload.get("text") or payload.get("user_text") or "").strip()
        reply_text = str(payload.get("reply_text") or "").strip()
        if not user_text and not reply_text:
            raise HTTPException(status_code=400, detail="text is required")
        intent = request.app.state.sticker_policy.simulate(
            request.app.state.settings,
            session_id=f"sticker_test:{channel}",
            user_text=user_text,
            reply_text=reply_text,
            source=channel,
        )
        sticker = None
        if intent.get("send"):
            sticker = request.app.state.sticker_store.choose(
                str(intent.get("tag") or ""),
                avoid_id=str(intent.get("avoid_id") or ""),
                channel=channel,
            )
            if not sticker:
                intent = {**intent, "send": False, "reason": "no_channel_sticker"}
        return {
            "ok": True,
            "channel": channel,
            "intent": intent,
            "sticker": sticker,
            "stickers_count": len(request.app.state.sticker_store.list()),
        }

    @router.delete("/api/stickers/{sticker_id}")
    async def delete_sticker(sticker_id: str, request: Request):
        require_local_service_control(request)
        return {"ok": request.app.state.sticker_store.delete(sticker_id), "stickers": request.app.state.sticker_store.list()}

    return router
