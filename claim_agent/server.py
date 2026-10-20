import asyncio
import base64
import binascii
import hmac
import json
from copy import deepcopy
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, StrictBool, model_validator

from .config import Settings
from .cloud_speech import CloudSpeech, SpeechRequest, TranscribeRequest
from .sweep_guide import SweepGuide, GuideRequest
from .comparison import register_comparison_routes
from .live import LiveBridge
from .openai_live import OpenAILiveBridge
from .openai_provider import OpenAIProvider
from .anthropic_provider import AnthropicProvider, configuration_error
from .claude_assistant import AssistantRequest, AssistantResponse, ClaudeAssistant
from .models import FrameMetadata, Locale, Model, Sweep, SweepState
from .onboarding import OnboardingSession
from .packet import write_bundle
from .pipeline import SweepRunner
from .pricing import PriceProvider
from .providers import GeminiProvider
from .storage import EvidenceStore
from .vision import VisionProvider


class CreateSweep(Model):
    device: str = Field(min_length=1,max_length=120)
    synthetic: bool = False
    country: str | None = None
    currency: str | None = None
    capture_mode: str = Field(default="lidar", pattern="^(camera|lidar)$")
    onboarding_id: str | None = Field(default=None, pattern="^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def locale_or_conversation(self):
        if not self.onboarding_id:
            Locale(country=self.country, currency=self.currency)
        elif self.synthetic or self.country is not None or self.currency is not None:
            raise ValueError("Spoken setup supplies the confirmed market and capture mode")
        return self


class CreateOnboarding(Model):
    device: str = Field(min_length=1, max_length=120)
    capture_supported: StrictBool
    camera_supported: StrictBool | None = None


class FrameUpload(Model):
    metadata: FrameMetadata
    jpeg_base64: str = Field(max_length=14_000_000)
    depth_base64: str = Field(default="",max_length=6_000_000)
    confidence_base64: str = Field(default="",max_length=2_000_000)


class Finish(Model):
    duration_s: float = Field(ge=0,le=1800)
    capture_issues: list[str] = Field(default_factory=list,max_length=100)


class LocalVoiceTool(Model):
    name: str = Field(min_length=1,max_length=80)
    arguments: dict


def create_app(settings: Settings | None = None) -> FastAPI:
    settings=settings or Settings.from_env()
    store=EvidenceStore(settings.data_dir)
    runners={}
    onboarding={}
    active_comparisons=set()

    def make_provider():
        if settings.provider=="anthropic":
            return AnthropicProvider(settings.api_key,settings.vision_model,workspace_id=settings.anthropic_workspace_id)
        factory=OpenAIProvider if settings.provider=="openai" else GeminiProvider
        return factory(settings.api_key,settings.vision_model)

    def make_runner(state):
        provider=make_provider()
        vision=VisionProvider(provider) if settings.api_key else None
        prices=PriceProvider(provider,settings.threshold_inr) if settings.api_key else None
        runner=SweepRunner(state,store,settings,vision,prices)
        runner.provider=provider
        def save_assistant():
            runner.snapshot()
            store.save(runner.state)
        runner.assistant=ClaudeAssistant(runner,settings,save_assistant)
        runner.speech=CloudSpeech(runner,settings,save_assistant)
        runner.guide=SweepGuide(runner,settings,save_assistant)
        return runner

    @asynccontextmanager
    async def lifespan(app):
        yield
        for session in onboarding.values():
            await session.close()
            await session.speech.close()
        for runner in runners.values():
            if runner.live:
                await runner.live.close()
            await runner.close()
            await runner.speech.close()
            await runner.provider.close()

    app=FastAPI(title="Library Contents Claim Agent",lifespan=lifespan)
    app.state.runners=runners
    app.state.onboarding=onboarding
    app.state.settings=settings
    app.state.store=store

    @app.middleware("http")
    async def boundary(request,call_next):
        origin=request.headers.get("origin")
        if origin and urlsplit(origin).netloc!=request.url.netloc:
            return JSONResponse({"detail":"Foreign browser origin rejected"},status_code=403)
        if request.method in {"POST","PUT","PATCH"}:
            body=bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>22_000_000:
                    return JSONResponse({"detail":"Request exceeds 22 MB"},status_code=413)
            request._body=bytes(body)
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Cache-Control"]="no-store"
        response.headers["Referrer-Policy"]="no-referrer"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request,error):
        return JSONResponse({"detail":str(error)},status_code=422)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request,error):
        return JSONResponse({"detail":str(error)},status_code=409)

    def authorized(request):
        header=request.headers.get("authorization", "")
        token=header[7:] if header.startswith("Bearer ") else request.cookies.get("claim_pair", "")
        return bool(token) and hmac.compare_digest(token,settings.pair_token)

    async def auth(request:Request):
        if not authorized(request):
            raise HTTPException(401,"Pair this device first")

    async def runner_for(id):
        if id not in runners:
            try:
                state=store.load(id)
            except (FileNotFoundError,ValueError):
                raise HTTPException(404,"Sweep not found")
            if state.completion in {"capturing","processing"}:
                state.completion="partial"
                state.review_reasons.append("Server restarted during sweep; pending processing was interrupted. Start a new sweep for continuous capture.")
                store.save(state)
            runners[id]=make_runner(state)
            runners[id].resume_pricing()
        return runners[id]

    @app.get("/api/health")
    async def health():
        error=configuration_error(settings.api_key,settings.anthropic_workspace_id) if settings.provider=="anthropic" else ""
        realtime = settings.provider == "openai"
        speech_ready = settings.realtime_ready if realtime else (settings.speech_provider=="elevenlabs" and bool(settings.elevenlabs_api_key))
        return {"ok":True,"model_ready":bool(settings.api_key) and not error,"vision_model":settings.vision_model,
                "provider_configuration_error":error,
                "live_model":settings.live_model,"native_capture_required":True,"provider":settings.provider,
                "local_first":settings.local_first,"cloud_max_calls":settings.cloud_max_calls,
                "paid_pricing":settings.paid_pricing,"cloud_voice":settings.cloud_voice,
                "voice_transport":"realtime" if realtime else "utterance",
                "realtime_ready":settings.realtime_ready,
                "assistant_provider":settings.provider if settings.provider in {"anthropic","openai"} else "",
                "assistant_model":settings.live_model if realtime else (settings.assistant_model or settings.vision_model) if settings.provider=="anthropic" else "",
                "assistant_supported":settings.provider in {"anthropic","openai"},
                "assistant_ready":settings.realtime_ready if realtime else settings.provider=="anthropic" and bool(settings.api_key) and not error,
                "assistant_max_calls":settings.assistant_max_calls,
                "speech_provider":"openai" if realtime else settings.speech_provider,"speech_ready":speech_ready,
                "speech_input_model":settings.live_model if realtime else settings.speech_input_model,
                "speech_output_model":settings.live_model if realtime else settings.speech_output_model}

    @app.post("/api/pair")
    async def pair(request:Request):
        data=await request.json()
        token=data.get("token","") if isinstance(data,dict) else ""
        if not isinstance(token,str) or not hmac.compare_digest(token,settings.pair_token):
            raise HTTPException(401,"Pairing code does not match")
        response=JSONResponse({"paired":True})
        response.set_cookie("claim_pair",token,httponly=True,samesite="strict",secure=request.url.scheme=="https")
        return response

    @app.get("/api/sweeps",dependencies=[Depends(auth)])
    async def list_sweeps():
        values=[]
        for path in sorted(store.root.glob("*/state.json"),key=lambda p:p.stat().st_mtime,reverse=True)[:20]:
            state=store.load(path.parent.name)
            values.append({"id":state.sweep.id,"captured_at":state.sweep.captured_at,"completion":state.completion,
                           "country":state.sweep.country,"synthetic":state.sweep.synthetic,"book_count":len(state.books)})
        return values

    @app.post("/api/sweeps",dependencies=[Depends(auth)])
    async def create(body:CreateSweep):
        session=None
        if body.onboarding_id:
            # The handoff key is part of the first atomic sweep snapshot, so a
            # lost HTTP reply or server restart can only recover this same sweep.
            for path in store.root.glob("*/state.json"):
                saved=store.load(path.parent.name)
                if saved.metrics.get("onboarding_id")==body.onboarding_id:
                    if saved.sweep.device!=body.device:
                        raise HTTPException(409,"Conversation belongs to a different device")
                    return (await runner_for(saved.sweep.id)).snapshot()
            session=onboarding.get(body.onboarding_id)
            if not session or not session.remaining:
                # Recovery lookup above proved no sweep was created for this key.
                raise HTTPException(409,{"code":"handoff_unavailable","message":"This conversation expired before a sweep was created. Start a new conversation."})
            session.check_handoff()
            if session.assistant.lock.locked() or session.speech.lock.locked():
                raise HTTPException(409,"Wait for the current assistant turn before starting a sweep")
        if len(active_comparisons)+sum(r.state.completion in {"capturing","processing"} for r in runners.values())>=settings.max_sweeps:
            raise HTTPException(409,"Finish the active sweep before starting another")
        preferences=session.preferences if session else body
        state=SweepState(sweep=Sweep(id=uuid4().hex,device=body.device,country=preferences.country,currency=preferences.currency,synthetic=body.synthetic))
        state.sweep.capture_mode="camera" if preferences.capture_mode=="camera" else "lidar"
        state.metrics["capture_models"]={"provider":settings.provider,"vision_model":settings.vision_model,"live_model":settings.live_model}
        if state.sweep.capture_mode=="camera":
            state.review_reasons.append("Camera-only analysis: physical dimensions and room area are unavailable; overlapping views can create duplicate tracks, so counts require review")
        if session:
            # No await between validation and consumption; concurrent handoffs cannot create two sweeps.
            state.metrics["onboarding_id"]=session.id
            state.sweep.language=preferences.language
            state.sweep.comparison_requested=preferences.capture_mode=="comparison"
            state.events=deepcopy(list(session.state.events))
            state.metrics["onboarding_live_usage"]=deepcopy(session.state.metrics.get("live_usage",[]))
            state.metrics["onboarding_assistant_usage"]=deepcopy(session.state.metrics.get("assistant_usage",[]))
            state.metrics["onboarding_speech_usage"]=deepcopy(session.state.metrics.get("speech_usage",[]))
            state.metrics["spoken_setup"]=preferences.model_dump()
        store.save(state)
        if session:
            session.consumed=True
        runner=make_runner(state)
        runners[state.sweep.id]=runner
        runner.start()
        if session:
            await session.close()
            await session.speech.close()
            state.metrics["onboarding_live_usage"] = deepcopy(session.state.metrics.get("live_usage",[]))
            runner.save_publish()
        return runner.snapshot()

    @app.post("/api/onboarding",dependencies=[Depends(auth)])
    async def setup(body:CreateOnboarding):
        for id,session in list(onboarding.items()):
            if session.consumed or not session.remaining:
                onboarding.pop(id)
                await session.close()
                await session.speech.close()
        if len(onboarding)>=8:
            raise HTTPException(409,"Conversation limit reached; allow an existing conversation to expire")
        session=OnboardingSession(body.device,body.capture_supported,body.camera_supported)
        session.provider=make_provider()
        session.assistant=ClaudeAssistant(session,settings)
        session.speech=CloudSpeech(session,settings)
        onboarding[session.id]=session
        return {"id":session.id,"expires_in_s":session.ttl}

    @app.get("/api/sweeps/{id}",dependencies=[Depends(auth)])
    async def get(id:str):
        return (await runner_for(id)).snapshot()

    async def local_voice_target(kind, id):
        if kind == "sweeps":
            return await runner_for(id)
        session=onboarding.get(id)
        if not session:
            raise HTTPException(404,"Conversation not found")
        session.check_active()
        return session

    @app.get("/api/onboarding/{id}/local-voice",dependencies=[Depends(auth)])
    @app.get("/api/sweeps/{id}/local-voice",dependencies=[Depends(auth)])
    async def local_voice_context(request:Request,id:str):
        target=await local_voice_target(request.url.path.split("/")[2],id)
        # Reuse the existing validation/tool contract without starting a bridge,
        # creating a provider request or attaching cloud audio to the session.
        bridge=LiveBridge(target,None,"")
        return {"instructions":bridge.instruction(),"speech":target.speech.configuration(),
                "context":target.voice_context(),"tools":bridge.tool_definitions(),"greeting":bridge.greeting()}

    @app.post("/api/onboarding/{id}/local-voice",dependencies=[Depends(auth)])
    @app.post("/api/sweeps/{id}/local-voice",dependencies=[Depends(auth)])
    async def local_voice_tool(request:Request,id:str,body:LocalVoiceTool):
        if len(json.dumps(body.arguments))>16000:
            raise ValueError("Voice tool arguments exceed limit")
        target=await local_voice_target(request.url.path.split("/")[2],id)
        bridge=LiveBridge(target,None,"")
        if body.name not in {tool["name"] for tool in bridge.tool_definitions()}:
            raise ValueError("Unsupported tool")
        target.assistant.clear_tool_result()
        context=await bridge.invoke_tool(body.name,body.arguments)
        target.assistant.record_tool_result(body.name,body.arguments,context)
        target.state.events.append({"type":"local_voice_tool","name":body.name,"source":"on_device"})
        if isinstance(target,SweepRunner):
            store.save(target.state)
        return {"ok":True,"context":context}

    @app.post("/api/onboarding/{id}/local-voice/assistant",dependencies=[Depends(auth)],response_model=AssistantResponse)
    @app.post("/api/sweeps/{id}/local-voice/assistant",dependencies=[Depends(auth)],response_model=AssistantResponse)
    async def local_voice_assistant(request:Request,id:str,body:AssistantRequest):
        target=await local_voice_target(request.url.path.split("/")[2],id)
        return await connected_speech(request,target.assistant.respond(body))

    async def connected_speech(request, operation):
        async def disconnected():
            # FastAPI has consumed the validated body. Await the next ASGI message;
            # nonblocking is_disconnected() can miss disconnects behind middleware.
            while True:
                if (await request.receive()).get("type") == "http.disconnect":
                    return
        work=asyncio.create_task(operation)
        connection=asyncio.create_task(disconnected())
        try:
            done,_=await asyncio.wait({work,connection},return_when=asyncio.FIRST_COMPLETED)
            if work in done:return await work
            work.cancel()
            await asyncio.gather(work,return_exceptions=True)
            raise HTTPException(499,"Speech client disconnected")
        finally:
            connection.cancel()
            if not work.done():work.cancel()
            await asyncio.gather(work,connection,return_exceptions=True)

    @app.post("/api/onboarding/{id}/local-voice/transcribe",dependencies=[Depends(auth)])
    @app.post("/api/sweeps/{id}/local-voice/transcribe",dependencies=[Depends(auth)])
    async def transcribe(request:Request,id:str,body:TranscribeRequest):
        target=await local_voice_target(request.url.path.split("/")[2],id)
        return await connected_speech(request,target.speech.transcribe(body))

    @app.post("/api/onboarding/{id}/local-voice/speech",dependencies=[Depends(auth)])
    @app.post("/api/sweeps/{id}/local-voice/speech",dependencies=[Depends(auth)])
    async def speech(request:Request,id:str,body:SpeechRequest):
        target=await local_voice_target(request.url.path.split("/")[2],id)
        return await connected_speech(request,target.speech.speak(body))

    @app.post("/api/sweeps/{id}/local-voice/guidance",dependencies=[Depends(auth)])
    async def guidance(request:Request,id:str,body:GuideRequest):
        target=await runner_for(id)
        return await connected_speech(request,target.guide.respond(body))

    @app.post("/api/sweeps/{id}/frames",dependencies=[Depends(auth)])
    async def frames(id:str,body:FrameUpload):
        try:
            values=[base64.b64decode(value,validate=True) for value in [body.jpeg_base64,body.depth_base64,body.confidence_base64]]
        except binascii.Error:
            raise ValueError("Malformed base64 evidence")
        admission = await (await runner_for(id)).upload(body.metadata,*values)
        if admission.get("accepted") is False:
            # Non-2xx also protects older phone builds that acknowledge every 200.
            return JSONResponse({"detail":{"message":"Recognition queue is full; retry this saved photo", **admission}},
                                status_code=429,headers={"Retry-After":str(admission["retry_after_s"])})
        return admission

    @app.post("/api/sweeps/{id}/geometry",dependencies=[Depends(auth)])
    async def geometry(id:str,body:dict):
        await (await runner_for(id)).set_geometry(body)
        return {"saved":True}

    @app.post("/api/sweeps/{id}/corrections",dependencies=[Depends(auth)])
    async def correction(id:str,body:dict):
        return await (await runner_for(id)).correct(body)

    @app.post("/api/sweeps/{id}/finish",dependencies=[Depends(auth)])
    async def finish(id:str,body:Finish):
        runner=await runner_for(id)
        async with runner.lock:
            for issue in body.capture_issues:
                reason="Native capture: "+issue[:1000]
                if reason not in runner.state.review_reasons:
                    runner.state.review_reasons.append(reason)
            store.save(runner.state)
        return await runner.finish(body.duration_s)

    @app.post("/api/sweeps/{id}/second-locale",dependencies=[Depends(auth)])
    async def second(id:str,body:Locale):
        return await (await runner_for(id)).second_locale(body)

    @app.get("/api/sweeps/{id}/packet.zip",dependencies=[Depends(auth)])
    async def packet(id:str):
        runner=await runner_for(id)
        async with runner.lock:
            path=write_bundle(runner.state,store)
        return FileResponse(path,filename=f"claim_{id}.zip",media_type="application/zip")

    @app.get("/api/sweeps/{id}/claim_packet.json",dependencies=[Depends(auth)])
    async def packet_json(id:str):
        return (await runner_for(id)).snapshot()

    @app.get("/api/sweeps/{id}/report.html",dependencies=[Depends(auth)])
    async def report(id:str):
        runner=await runner_for(id)
        async with runner.lock:
            write_bundle(runner.state,store)
        return FileResponse(store.directory(id)/"report.html",media_type="text/html")

    @app.get("/api/sweeps/{id}/{kind}/{name}",dependencies=[Depends(auth)])
    async def evidence(id:str,kind:str,name:str):
        runner=await runner_for(id)
        ref=f"{kind}/{name}"
        store.read_evidence(runner.state,ref)
        return FileResponse(store.evidence_path(id,ref),media_type="image/jpeg" if kind=="frames" else "application/octet-stream")

    async def serve_live(ws,runner,setup=False):
        await ws.accept()
        events=asyncio.Queue(maxsize=256)
        runner.listeners.add(events)
        owner=None
        async def send():
            while True:
                await ws.send_json(await events.get())
        sender=asyncio.create_task(send())
        if setup:
            if runner.preferences:
                events.put_nowait({"type":"setup_confirmed","preferences":runner.preferences.model_dump()})
            if runner.capture_requested:
                events.put_nowait({"type":"capture_requested"})
        else:
            events.put_nowait({"type":"inventory","packet":runner.snapshot().model_dump(mode="json")})
        try:
            while True:
                if setup:
                    raw=await asyncio.wait_for(ws.receive_text(),timeout=runner.remaining)
                    runner.check_active()
                else:
                    raw=await ws.receive_text()
                if len(raw)>100_000:
                    await ws.close(code=1009)
                    break
                data=json.loads(raw)
                if not isinstance(data,dict): raise ValueError("Expected event object")
                if data.get("type")=="start_voice":
                    if settings.provider=="anthropic":
                        events.put_nowait({"type":"voice_error","text":"Claude is configured for server vision. Use on-device voice; this socket continues to deliver events."})
                    elif not settings.cloud_voice:
                        events.put_nowait({"type":"voice_error","text":"Cloud voice is disabled by cost policy. Use on-device speech; this socket continues to deliver events."})
                    elif runner.live is None:
                        bridge=OpenAILiveBridge if settings.provider=="openai" else LiveBridge
                        owner=bridge(runner,runner.provider,settings.live_model)
                        runner.live=owner
                        owner.start()
                    else:
                        events.put_nowait({"type":"voice_error","text":"Voice already connected on another client"})
                elif data.get("type")=="audio" and owner:
                    pcm=base64.b64decode(data.get("pcm_base64",""),validate=True)
                    if len(pcm)>32000 or len(pcm)%2:
                        raise ValueError("Expected bounded PCM16 mono audio at 16000 Hz")
                    owner.offer(("audio",pcm))
                elif data.get("type")=="playback" and owner:
                    owner.acknowledge(data)
                elif data.get("type")=="interrupt_voice" and owner and settings.provider=="openai":
                    owner.offer(("interrupt",None))
                elif data.get("type")=="capture_feedback" and owner and not setup:
                    # Capture guidance is client data, never an instruction or a fact about inventory.
                    text = data.get("text", "")
                    if text in {"Please add some light so I can see the objects clearly.",
                                "Please turn away from the glare so I can see the objects clearly.",
                                "Please hold the phone steady for a clear photo.",
                                "Please step back a little and show the whole object in the camera."}:
                        owner.offer_context({"guidance":text,"source":"phone quality check"})
                elif data.get("type")=="stop_voice" and owner:
                    await owner.close()
                    runner.live=None
                    owner=None
        except (WebSocketDisconnect,ValueError,binascii.Error):
            pass
        except (TimeoutError,RuntimeError):
            await ws.close(code=4408)
        finally:
            runner.listeners.discard(events)
            sender.cancel()
            await asyncio.gather(sender,return_exceptions=True)
            if owner:
                await owner.close()
                if runner.live is owner:
                    runner.live=None
            if not setup:
                store.save(runner.state)

    def authorized_socket(ws):
        origin=ws.headers.get("origin")
        return authorized(ws) and (not origin or urlsplit(origin).netloc==ws.url.netloc)

    @app.websocket("/api/onboarding/{id}/live")
    async def setup_live(ws:WebSocket,id:str):
        if not authorized_socket(ws):
            await ws.close(code=4401)
            return
        session=onboarding.get(id)
        if not session or not session.remaining or session.consumed:
            await ws.close(code=4404)
            return
        await serve_live(ws,session,setup=True)

    @app.websocket("/api/sweeps/{id}/live")
    async def live(ws:WebSocket,id:str):
        if not authorized_socket(ws):
            await ws.close(code=4401)
            return
        try:
            runner=await runner_for(id)
        except HTTPException:
            await ws.close(code=4404)
            return
        await serve_live(ws,runner)

    register_comparison_routes(app,auth,runner_for,make_runner,runners,store,settings,active_comparisons)
    static=Path(__file__).parent/"static"
    if static.exists():
        app.mount("/",StaticFiles(directory=static,html=True),name="review")
    return app
