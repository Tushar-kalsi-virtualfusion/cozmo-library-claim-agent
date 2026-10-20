"""Bounded ElevenLabs speech, private credentials and durable billing receipts.

Raw owner audio is sent once and never persisted. Completed audio is cached only
in memory; an evicted/interrupted request ID cannot make another billable call.
"""
import asyncio
import base64
import binascii
import hashlib
import io
import json
import re
import time
import wave
from collections import OrderedDict
from uuid import UUID

import httpx
import numpy as np
from fastapi import HTTPException
from pydantic import Field

from .models import Model


class SpeechRequest(Model):
    request_id: UUID
    text: str = Field(min_length=1, max_length=1200)
    language: str = Field(default='', max_length=80)
    guidance_event_id: str | None = Field(default=None, min_length=1, max_length=256)


class TranscribeRequest(Model):
    request_id: UUID
    audio_base64: str = Field(min_length=1, max_length=1_400_000)
    language: str = Field(default='', max_length=80)


def language_code(value):
    value=value.strip().replace('_','-').split('-')[0].lower()
    return value if re.fullmatch(r'[a-z]{2,3}',value) else None


def validate_audio(encoded):
    try:
        raw=base64.b64decode(encoded,validate=True)
        if len(raw)>1_000_000: raise ValueError()
        with wave.open(io.BytesIO(raw),'rb') as audio:
            if (audio.getnchannels(),audio.getsampwidth(),audio.getframerate(),audio.getcomptype())!=(1,2,16000,'NONE'):
                raise ValueError()
            count=audio.getnframes()
            if not 4000<=count<=480000: raise ValueError()
            pcm=audio.readframes(count)
            if len(pcm)!=count*2: raise ValueError()
        samples=np.frombuffer(pcm,dtype='<i2').astype(np.float64)
        if np.sqrt(np.mean(samples*samples))<25:
            raise ValueError('No audible speech; please speak again')
        return raw, count/16000
    except (binascii.Error,wave.Error,EOFError,ValueError) as error:
        raise ValueError('Send 0.25–30 seconds of audible mono PCM16 WAV at 16000 Hz') from error


def pcm_wav(pcm):
    if not 4800<=len(pcm)<=5_760_000 or len(pcm)%2:
        raise ValueError('Incomplete speech audio')
    if not np.any(np.frombuffer(pcm,dtype='<i2')):
        raise ValueError('Silent speech audio')
    output=io.BytesIO()
    with wave.open(output,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000)
        audio.writeframes(pcm)
    return output.getvalue()


class CloudSpeech:
    def __init__(self,target,settings,save=None,*,client=None):
        self.target,self.settings,self.save=target,settings,save
        self.lock=asyncio.Lock()
        self.cache=OrderedDict()
        self.client=client or httpx.AsyncClient(timeout=settings.speech_timeout_s,follow_redirects=False)

    def configuration(self):
        if self.settings.provider == 'openai':
            ready = self.settings.realtime_ready
            return {'provider':'openai', 'transport':'realtime', 'ready':ready,
                    'input_model':self.settings.live_model, 'output_model':self.settings.live_model,
                    'voice':'marin', 'error':'' if ready else 'OpenAI Realtime voice is unavailable; check server access and cloud voice configuration'}
        selected=self.settings.speech_provider
        ready=selected=='elevenlabs' and bool(self.settings.elevenlabs_api_key and self.settings.speech_voice)
        return {'provider':selected,'ready':ready if selected!='local' else True,
                'input_model':self.settings.speech_input_model,'output_model':self.settings.speech_output_model,
                'voice':self.settings.speech_voice,'error':'' if ready or selected=='local' else 'ElevenLabs speech is not configured on the server'}

    def persist(self):
        if self.save:self.save()

    async def transcribe(self,body):
        raw,seconds=validate_audio(body.audio_base64)
        return await self.perform(body,'transcribe',raw=raw,seconds=seconds)

    async def speak(self,body):
        if not body.text.strip():raise ValueError('Speech text is empty')
        return await self.perform(body,'speech')

    def require_current_guidance(self, body):
        event_id=getattr(body,'guidance_event_id',None)
        if event_id is None:return
        guide=getattr(self.target,'guide',None)
        if guide is None or not guide.is_current(event_id):
            raise HTTPException(409,{'code':'guidance_stale',
                'message':'The view changed; this guidance audio was discarded'})

    async def perform(self,body,kind,*,raw=None,seconds=None):
        # An interrupted owner turn must not queue a second billable turn behind it.
        # Identical in-flight IDs may wait for the one result, with a whole-call bound.
        if self.lock.locked() and str(body.request_id) not in self.target.state.metrics.get('speech_requests',{}):
            raise HTTPException(409,'Previous speech is still finishing; wait a moment before speaking again')
        try:
            async with asyncio.timeout(self.settings.speech_timeout_s):
                return await self._perform(body,kind,raw=raw,seconds=seconds)
        except TimeoutError as error:
            raise HTTPException(504,'Speech deadline reached; start a new turn') from error

    async def _perform(self,body,kind,*,raw=None,seconds=None):
        key=str(body.request_id)
        signature=hashlib.sha256((kind+json.dumps(body.model_dump(mode='json'),sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
        async with self.lock:
            if hasattr(self.target,'check_active'):self.target.check_active()
            receipts=self.target.state.metrics.setdefault('speech_requests',{})
            if key in receipts:
                receipt=receipts[key]
                if receipt['signature']!=signature:raise HTTPException(409,'Speech request ID has different content')
                if key in self.cache:
                    self.require_current_guidance(body)
                    return self.cache[key]
                raise HTTPException(receipt.get('status_code',503),receipt.get('error','Speech request expired or was interrupted; start a new turn'))
            self.require_current_guidance(body)
            if self.settings.provider == 'openai':
                raise HTTPException(503,'This server uses OpenAI Realtime streaming; update the app and reconnect voice')
            if not self.configuration()['ready'] or self.settings.speech_provider!='elevenlabs':
                raise HTTPException(503,'ElevenLabs speech is not configured on the server')
            if len(receipts)>=self.settings.speech_max_calls:raise HTTPException(429,'Speech request budget reached for this session')
            model=self.settings.speech_input_model if kind=='transcribe' else self.settings.speech_output_model
            receipt={'signature':signature,'status':'pending','kind':kind,'model':model}
            receipts[key]=receipt;self.persist()
            row={'provider':'elevenlabs','model':model,'request_id':key,'request_kind':kind,'usage':None}
            self.target.state.metrics.setdefault('speech_usage',[]).append(row)
            start=time.monotonic()
            try:
                headers={'xi-api-key':self.settings.elevenlabs_api_key}
                code=language_code(body.language)
                async with asyncio.timeout(self.settings.speech_timeout_s):
                    if kind=='transcribe':
                        fields={'model_id':model,'tag_audio_events':'false','diarize':'false','timestamps_granularity':'none'}
                        if code:fields['language_code']=code
                        url='https://api.elevenlabs.io/v1/speech-to-text'
                        options={'data':fields,'files':{'file':('utterance.wav',raw,'audio/wav')}}
                        limit=100_000
                    else:
                        data={'text':body.text,'model_id':model,
                              'voice_settings':{'stability':.5,'similarity_boost':.75,'use_speaker_boost':True}}
                        if code:data['language_code']=code
                        url='https://api.elevenlabs.io/v1/text-to-speech/'+self.settings.speech_voice
                        options={'params':{'output_format':'pcm_24000'},'json':data}
                        limit=5_760_000
                    async with self.client.stream('POST',url,headers=headers,**options) as response:
                        if response.status_code!=200:
                            category={401:'key or permissions',403:'permissions',402:'credits',429:'credits or rate limit'}.get(response.status_code,'service availability')
                            raise HTTPException(502,f'ElevenLabs could not complete speech; check {category} (HTTP {response.status_code})')
                        row['response_id']=response.headers.get('request-id') or response.headers.get('x-request-id')
                        # Accepted requests are costed before reading/validating output.
                        row['usage']={'audio_seconds':seconds} if kind=='transcribe' else {'characters':len(body.text)}
                        row['usage_basis']='Measured submitted audio duration or Unicode text length; public API rate estimate, not an invoice'
                        credits=response.headers.get('character-cost')
                        if credits and re.fullmatch(r'\d+(\.\d+)?',credits):row['reported_credit_cost']=float(credits)
                        content=bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            if len(content)+len(chunk)>limit:raise ValueError('Speech response exceeds limit')
                            content.extend(chunk)
                    if kind=='transcribe':
                        data=json.loads(content)
                        if not isinstance(data,dict):raise ValueError('Invalid transcription object')
                        text=data.get('text');language=data.get('language_code','')
                        if not isinstance(text,str) or len(text)>4000 or not isinstance(language,str) or len(language)>80:
                            raise ValueError('Invalid transcription')
                        result={'text':text.strip(),'language':language}
                    else:
                        result={'audio_base64':base64.b64encode(pcm_wav(bytes(content))).decode(),'audio_mime':'audio/wav'}
                # Camera evidence and corrections can overtake synthesis. Keep
                # accepted provider usage, but never return obsolete guide audio.
                self.require_current_guidance(body)
                receipt['status']='completed'
                self.cache[key]=result
                while len(self.cache)>8 or sum(len(x.get('audio_base64','')) for x in self.cache.values())>16_000_000:
                    self.cache.popitem(last=False)
                return result
            except asyncio.CancelledError:
                receipt.update(status='interrupted',status_code=503,error='Speech request interrupted; start a new turn')
                row['error_type']='cancelled';raise
            except HTTPException as error:
                receipt.update(status='failed',status_code=error.status_code,error=error.detail)
                row['error_type']='guidance_stale' if isinstance(error.detail,dict) and error.detail.get('code')=='guidance_stale' else 'provider_rejected'
                raise
            except (httpx.HTTPError,TimeoutError,ValueError,TypeError) as error:
                receipt.update(status='failed',status_code=502,error='ElevenLabs speech did not complete; reconnect voice and try a new turn')
                row['error_type']=type(error).__name__
                raise HTTPException(502,receipt['error']) from error
            finally:
                row['elapsed_s']=time.monotonic()-start;self.persist()

    async def close(self):
        await self.client.aclose()
        self.cache.clear()
