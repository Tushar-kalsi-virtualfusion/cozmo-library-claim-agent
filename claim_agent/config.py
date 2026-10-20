import os
import secrets
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    data_dir: Path = Path("data")
    pair_token: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    provider: str = "gemini"
    api_key: str = ""
    anthropic_workspace_id: str = ""
    vision_model: str = "gemini-3.8-flash"
    live_model: str = "gemini-3.8-live"
    workers: int = 2
    queue_size: int = 12
    finish_timeout: float = 240
    threshold_inr: Decimal = Decimal("10000")
    max_frames: int = 360
    max_sweeps: int = 2
    local_first: bool = True
    cloud_max_calls: int = 3
    cloud_cooldown_s: float = 30
    paid_pricing: bool = False
    cloud_voice: bool = False
    assistant_model: str = ""
    assistant_effort: str = "low"
    assistant_max_calls: int = 60
    assistant_timeout_s: float = 30
    assistant_max_tokens: int = 2048

    speech_provider: str = "local"
    elevenlabs_api_key: str = ""
    speech_input_model: str = "scribe_v2"
    speech_output_model: str = "eleven_v4_turbo"
    speech_voice: str = "iP95p4xoKVk53GoZ742B"
    speech_max_calls: int = 240
    speech_timeout_s: float = 40

    @property
    def realtime_ready(self):
        return self.provider == "openai" and self.cloud_voice and bool(self.api_key and self.live_model)

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        provider=os.getenv("CLAIM_PROVIDER", "openai" if os.getenv("OPENAI_API_KEY") else "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "gemini")
        defaults={"openai":("OPENAI_API_KEY","gpt-6-astra","gpt-realtime-2.1"),
                  "gemini":("GOOGLE_API_KEY","gemini-3.8-flash","gemini-3.8-live"),
                  "anthropic":("ANTHROPIC_API_KEY","claude-opus-5-5","")}
        if provider not in defaults: raise ValueError("CLAIM_PROVIDER must be openai, gemini or anthropic")
        key_name,vision,live=defaults[provider]
        live = "" if provider == "anthropic" else (os.getenv("CLAIM_LIVE_MODEL") or live)
        return cls(provider=provider,data_dir=Path(os.getenv("CLAIM_DATA_DIR", "data")),
                   pair_token=os.getenv("CLAIM_PAIR_TOKEN") or secrets.token_urlsafe(18),
                   api_key=os.getenv(key_name, ""),
                   anthropic_workspace_id=os.getenv("ANTHROPIC_WORKSPACE_ID", ""),
                   vision_model=os.getenv("CLAIM_VISION_MODEL",vision),
                   live_model=live,
                   workers=max(1,min(4,int(os.getenv("CLAIM_WORKERS", "2")))),
                   queue_size=max(1,min(30,int(os.getenv("CLAIM_QUEUE_SIZE", "12")))),
                   finish_timeout=max(1,min(270,float(os.getenv("CLAIM_FINISH_TIMEOUT", "240")))),
                   threshold_inr=Decimal(os.getenv("CLAIM_APPRAISAL_THRESHOLD_INR", "10000")),
                   local_first=os.getenv("CLAIM_LOCAL_FIRST", "true").lower() == "true",
                   cloud_max_calls=max(0,min(20,int(os.getenv("CLAIM_CLOUD_MAX_CALLS", "3")))),
                   cloud_cooldown_s=max(0,float(os.getenv("CLAIM_CLOUD_COOLDOWN_S", "30"))),
                   paid_pricing=os.getenv("CLAIM_PAID_PRICING", "false").lower() == "true",
                   cloud_voice=os.getenv("CLAIM_CLOUD_VOICE", "false").lower() == "true",
                   speech_provider="openai" if provider=="openai" else os.getenv("CLAIM_SPEECH_PROVIDER", "elevenlabs" if os.getenv("ELEVENLABS_API_KEY") else "local"),
                   elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
                   speech_input_model=live if provider=="openai" else os.getenv("CLAIM_SPEECH_INPUT_MODEL", "scribe_v2"),
                   speech_output_model=live if provider=="openai" else os.getenv("CLAIM_SPEECH_OUTPUT_MODEL", "eleven_v4_turbo"),
                   speech_voice="marin" if provider=="openai" else os.getenv("CLAIM_SPEECH_VOICE", "iP95p4xoKVk53GoZ742B"),
                   speech_max_calls=max(0,min(500,int(os.getenv("CLAIM_SPEECH_MAX_CALLS", "240")))),
                   speech_timeout_s=max(1,min(40,float(os.getenv("CLAIM_SPEECH_TIMEOUT_S", "40")))),
                   assistant_model=live if provider=="openai" else os.getenv("CLAIM_ASSISTANT_MODEL", "claude-sonnet-5-5" if provider=="anthropic" else ""),
                   assistant_effort=os.getenv("CLAIM_ASSISTANT_EFFORT", "low"),
                   assistant_max_calls=max(0,min(120,int(os.getenv("CLAIM_ASSISTANT_MAX_CALLS", "60")))),
                   assistant_timeout_s=max(1,min(60,float(os.getenv("CLAIM_ASSISTANT_TIMEOUT_S", "30")))),
                   assistant_max_tokens=max(128,min(4096,int(os.getenv("CLAIM_ASSISTANT_MAX_TOKENS", "2048")))))
