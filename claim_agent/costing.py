"""Server-only usage estimates. Published USD rates, not an invoice or item valuation.

Rates checked 2026-10-20 at https://developers.openai.com/api/docs/pricing
and https://developers.openai.com/api/docs/models/gpt-5.4-mini.md.
Standard global endpoint only; unknown models/usage are explicitly uncosted.
Claude Sonnet 5.5 rates checked 2026-10-19 at
https://platform.claude.com/docs/en/about-claude/pricing.
"""
from decimal import Decimal

VERSION = "openai-2026-10-20-anthropic-elevenlabs-2026-10-20"
ANTHROPIC = {"claude-sonnet-5-5": (2,.1,10,2.5,4), "claude-opus-5-5": (4,.2,20,5,8)}
TEXT = {"gpt-6-astra": (10, 1, 50),
        "gpt-5.4-mini": (.75, .075, 4.5), "gpt-5.4-mini-2026-03-17": (.75, .075, 4.5),
        "gpt-4o-mini-transcribe": (1.25, 1.25, 5)}
REALTIME = {"gpt-realtime-2.1": {"text": (4, .4, 24), "audio": (32, .4, 64), "image": (5, .5, 0)}}


def number(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("Missing or invalid token count")
    return value


def component(total, cached, output, rates):
    total, cached, output = map(number, (total, cached, output))
    if cached > total:
        raise ValueError("Cached tokens exceed input")
    a, b, c = map(lambda n: Decimal(str(n)), rates)
    return ((total-cached)*a + cached*b + output*c)/Decimal(1_000_000)


def pending_assistant_usage(metrics, usage=None):
    """Durable admissions may have incurred a charge before a process stopped.

    This is a separate reservation ledger, recomputed from receipts rather than
    appended to provider usage. Completed or already reported requests therefore
    cannot count both their reservation and their actual provider usage.
    """
    reported = {row.get('request_id') for row in (usage if usage is not None else metrics.get('provider_usage', []))}
    return [{'provider': 'anthropic', 'model': receipt.get('model'), 'request_id': key,
             'request_kind': 'assistant', 'error_type': 'pending_or_interrupted', 'usage': None}
            for key, receipt in metrics.get('assistant_requests', {}).items()
            if 'result' not in receipt and 'status_code' not in receipt and key not in reported]


def pending_speech_usage(metrics):
    """Count durable admissions with uncertain charges once, never as zero cost."""
    reported = {row.get('request_id') for row in metrics.get('speech_usage', [])}
    return [{'provider': 'elevenlabs', 'model': receipt.get('model'), 'request_id': key,
             'request_kind': receipt.get('kind', 'speech'), 'usage': None,
             'error_type': 'pending_or_interrupted'}
            for key, receipt in metrics.get('speech_requests', {}).items()
            if receipt.get('status', 'pending') in {'pending', 'interrupted'} and key not in reported]


def usage_cost(rows):
    subtotal = Decimal(0); known = 0; missing = 0; seen = set()
    for row in rows:
        key = (row.get("provider"),row.get("model"), row.get("response_id"))
        if key[2] and key in seen:
            continue
        if key[2]: seen.add(key)
        model, usage = row.get("model"), row.get("usage")
        try:
            if not isinstance(usage, dict): raise ValueError("Usage unavailable")
            if row.get("provider")=="elevenlabs":
                # Public ElevenAPI rates, checked 2026-10-20. Subscription invoices
                # differ; record submitted units and provider credits separately.
                if model in {"eleven_v4", "eleven_v4_turbo"}:
                    cost=Decimal(number(usage.get("characters")))*Decimal(".08" if model=="eleven_v4" else ".04")/1000
                elif model=="scribe_v2":
                    duration=Decimal(str(usage.get("audio_seconds")))
                    if not duration.is_finite() or duration<0:raise ValueError("Invalid audio duration")
                    cost=duration*Decimal(".22")/3600
                else:raise ValueError("Unconfigured ElevenLabs rate")
            elif model in ANTHROPIC and row.get("provider")=="anthropic":
                if usage.get("service_tier") not in {None,"standard"} or usage.get("inference_geo") not in {None,"global","not_available"}:
                    raise ValueError("Unconfigured Claude pricing tier")
                incoming,output=number(usage.get("input_tokens")),number(usage.get("output_tokens"))
                cached=number(usage.get("cache_read_input_tokens",0))
                writes=number(usage.get("cache_creation_input_tokens",0))
                durations=usage.get("cache_creation") or {}
                short=number(durations.get("ephemeral_5m_input_tokens",0))
                long=number(durations.get("ephemeral_1h_input_tokens",0))
                if short+long != writes: raise ValueError("Missing cache write duration breakdown")
                # Claude reports uncached input, cache reads and writes separately.
                cost=sum((Decimal(n)*Decimal(str(rate)) for n,rate in zip(
                    (incoming,cached,output,short,long),ANTHROPIC[model])),Decimal(0))/Decimal(1_000_000)
                server_tools=usage.get("server_tool_use") or {}
                if any(value for key,value in server_tools.items() if key!="web_search_requests"):
                    raise ValueError("Claude server tool pricing is not configured")
                cost+=Decimal(".01")*number(server_tools.get("web_search_requests",0))
            elif model in TEXT:
                rates = TEXT[model]
                details = usage.get("input_tokens_details") or {}
                incoming = number(usage.get("input_tokens"))
                cached = number(details.get("cached_tokens",0))
                if model == "gpt-6-astra" and incoming > 272000:
                    rates = (rates[0]*2,rates[1]*2,rates[2]*1.5)
                cost = component(incoming,cached,usage.get("output_tokens"),rates)
                if model == "gpt-6-astra":
                    writes = number(details.get("cache_write_tokens",0))
                    if writes+cached > incoming: raise ValueError("Cache breakdown exceeds input")
                    cost += Decimal(writes)*Decimal(str(rates[0]))*Decimal(".25")/Decimal(1_000_000)
            elif model in REALTIME:
                incoming, outgoing = usage.get("input_token_details") or {}, usage.get("output_token_details") or {}
                cached = incoming.get("cached_tokens_details") or {}
                if sum(number(incoming.get(k+"_tokens", 0)) for k in ("text","audio","image")) != number(usage.get("input_tokens")):
                    raise ValueError("Input modality breakdown missing")
                if sum(number(outgoing.get(k+"_tokens", 0)) for k in ("text","audio")) != number(usage.get("output_tokens")):
                    raise ValueError("Output modality breakdown missing")
                if sum(number(cached.get(k+"_tokens", 0)) for k in ("text","audio","image")) != number(incoming.get("cached_tokens", 0)):
                    raise ValueError("Cache modality breakdown missing")
                cost = sum((component(incoming.get(k+"_tokens", 0), cached.get(k+"_tokens", 0),
                                      outgoing.get(k+"_tokens", 0), rates) for k, rates in REALTIME[model].items()), Decimal(0))
            else:
                raise ValueError("Unconfigured rate")
            cost += Decimal(".01")*number(row.get("web_search_calls", 0))
            subtotal += cost; known += 1
        except (ValueError, TypeError, ArithmeticError):
            missing += 1
    return {"cost_usd":float(subtotal) if known or not missing else None,
            "cost_status":"partial" if known and missing else "unavailable" if missing else "estimated",
            "known_cost_usd":float(subtotal), "costed_requests":known, "uncosted_requests":missing,
            "cost_rate_card":VERSION, "cost_basis":"Reported API usage and measured ElevenLabs speech units at public USD rates; subscription billing may differ. Excludes tax, hosting, network and device costs. Partial totals exclude unknown charges."}
