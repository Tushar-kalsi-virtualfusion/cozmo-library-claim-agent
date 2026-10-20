import unittest
from types import SimpleNamespace

from claim_agent.live import live_events


class LiveTests(unittest.TestCase):
    def test_interruption_precedes_audio_and_discards_same_message_audio(self):
        msg=SimpleNamespace(server_content=SimpleNamespace(interrupted=True,model_turn=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=b"stale",mime_type="audio/pcm;rate=24000"))]),
                             input_transcription=None, output_transcription=None, turn_complete=False), tool_call=None)
        events=live_events(msg)
        self.assertEqual(events,[{"type":"interrupted"}])

    def test_audio_event_preserves_provider_rate(self):
        msg=SimpleNamespace(server_content=SimpleNamespace(interrupted=False,model_turn=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=b"\x01\x02",mime_type="audio/pcm;rate=24000"))]),
                             input_transcription=None, output_transcription=None, turn_complete=True),tool_call=None)
        events=live_events(msg)
        self.assertEqual(events[0]["sample_rate"],24000)
        self.assertEqual(events[-1]["type"],"turn_complete")
