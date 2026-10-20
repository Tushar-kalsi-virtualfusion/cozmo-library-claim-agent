import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from claim_agent.config import Settings
from claim_agent.live import LiveBridge
from claim_agent.models import Book, Price
from claim_agent.pipeline import SweepRunner
from claim_agent.storage import EvidenceStore
from claim_agent.sweep_guide import GuideRequest, SweepGuide
from tests.test_geometry import frame


class RealtimeGuideTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(Path(self.tmp.name))
        self.settings=Settings(provider='openai',data_dir=Path(self.tmp.name))
        self.runner=SweepRunner(self.store.create('test','India','INR',synthetic=True),self.store,self.settings,None,None)
        self.addAsyncCleanup(self.runner.close)
        self.runner.assistant=SimpleNamespace(lock=asyncio.Lock(),respond=AsyncMock(side_effect=AssertionError('Separate wording call')))
        self.save=lambda:self.store.save(self.runner.state)
        self.guide=SweepGuide(self.runner,self.settings,self.save); self.runner.guide=self.guide
        self.bridge=LiveBridge(self.runner,None,'gpt-realtime'); self.runner.live=self.bridge

    def add_frame(self,id='f1',status='processed'):
        meta=frame().model_copy(update={'id':id})
        self.runner.state.frames[id]=meta; self.runner.state.frame_status[id]=status
        self.store.put_evidence(self.runner.state,f'frames/{id}.jpg',b'frame')
        self.store.put_json(self.runner.state,f'observations/meta_{id}.json',meta.model_dump())

    def add_book(self,id='b1',frame_id='f1'):
        book=Book(id=id,title='Dune',status='identified',frame_ref=f'frames/{frame_id}.jpg')
        self.runner.state.books.append(book); self.save(); return book

    async def test_openai_guidance_leases_facts_without_separate_wording(self):
        self.assertIsNone(self.bridge.next_guidance())
        self.add_frame(); self.add_book(); self.guide.record('processed','f1',new_ids=['b1'])
        lease=self.bridge.next_guidance()
        self.assertEqual(lease['guide_cue']['stage'],'processed')
        self.assertTrue(lease['guide_cue']['can_move_on'])
        self.assertTrue(self.guide.is_current(lease['event_id']))
        self.assertIsNone(self.bridge.next_guidance())
        self.assertEqual(await self.guide.respond(GuideRequest(request_id=uuid4())),{'reply':'','event_id':''})
        self.runner.assistant.respond.assert_not_awaited()
        self.assertNotIn('guide_cue',self.runner.voice_context())

    async def test_restart_does_not_replay_ambiguous_realtime_lease(self):
        self.add_frame(); self.add_book(); self.guide.record('processed','f1',new_ids=['b1'])
        lease=self.bridge.next_guidance()
        self.runner.state=self.store.load(self.runner.state.sweep.id)
        self.runner.guide=SweepGuide(self.runner,self.settings,self.save)
        self.assertFalse(self.runner.guide.is_current(lease['event_id']))
        self.assertIsNone(self.bridge.next_guidance())

    async def test_interruption_consumes_lease_and_new_view_invalidates_it(self):
        self.add_frame(); self.add_book(); self.guide.record('processed','f1',new_ids=['b1'])
        lease=self.bridge.next_guidance()
        self.add_frame('f2','queued'); self.guide.record('uploaded','f2')
        self.assertFalse(self.guide.is_current(lease['event_id']))
        self.guide.finish_realtime(lease['event_id'],outcome='interrupted')
        self.assertFalse(self.guide.is_current(lease['event_id']))
        self.assertEqual(self.guide.ledger['realtime_leases'][lease['event_id']]['status'],'interrupted')

    async def test_realtime_private_validation_covers_thirteenth_record(self):
        self.add_frame()
        for index in range(13): self.add_book(f'b{index}')
        self.guide.record('processed','f1',new_ids=[b.id for b in self.runner.state.books])
        lease=self.bridge.next_guidance()
        self.assertEqual(len(lease['guide_cue']['records']),12)
        self.runner.state.books[-1].status='unidentified'
        self.assertFalse(self.guide.is_current(lease['event_id']))

    async def test_only_sourced_quotes_become_live_valuation_cues(self):
        self.add_frame(); book=self.add_book()
        book.replacement_cost=Price(amount='999',verified=False)
        self.assertIsNone(self.bridge.next_guidance())
        self.store.put_json(self.runner.state,'sources/p.json',{'price':'100'})
        book.replacement_cost=Price(amount='100',verified=True,currency='INR',country='India',
            source='Shop',url='https://example.com/book',source_ref='sources/p.json',
            retrieved_at='2026-10-20T00:00:00+00:00',condition_assumed='new equivalent',
            match='equivalent',match_basis='Same title; edition unknown')
        lease=self.bridge.next_guidance(); facts=lease['guide_cue']
        self.assertEqual(facts['stage'],'valuation'); self.assertFalse(facts['can_move_on'])
        self.assertEqual(facts['records'][0]['prices'][0]['amount'],'100')
        self.assertEqual(facts['records'][0]['prices'][0]['match'],'equivalent')
        book.excluded=True
        self.assertFalse(self.guide.is_current(lease['event_id']))

    async def test_record_notifies_live_context_without_consuming_cue(self):
        self.add_frame(); self.add_book(); self.guide.record('processed','f1',new_ids=['b1'])
        kind,payload=self.bridge.outgoing.get_nowait()
        self.assertEqual(kind,'context'); self.assertIn('guide_updated',payload)
        self.assertEqual(self.guide.ledger['admissions'],0)

    async def test_completed_realtime_lease_is_once_and_has_current_summary(self):
        self.add_frame(); self.add_book()
        self.runner.state.completion='partial'
        from claim_agent.packet import write_bundle
        write_bundle(self.runner.state,self.store)
        self.guide.record('complete',self.runner.state.sweep.id)
        lease=self.bridge.next_guidance()
        self.assertEqual(lease['guide_cue']['totals']['books_identified'],1)
        self.guide.finish_realtime(lease['event_id'],outcome='delivered')
        self.assertTrue(self.guide.is_current(lease['event_id']))
        self.assertIsNone(self.bridge.next_guidance())
        self.runner.state.books[0].excluded=True
        self.assertFalse(self.guide.is_current(lease['event_id']))

    async def test_usage_only_revision_cannot_invalidate_completed_summary(self):
        self.add_frame(); self.add_book(); self.runner.state.completion='partial'
        from claim_agent.packet import write_bundle
        write_bundle(self.runner.state,self.store); self.guide.record('complete',self.runner.state.sweep.id)
        lease=self.bridge.next_guidance()
        self.runner.state.metrics['live_usage']=[{'provider':'openai','usage':None}]
        self.runner.save_publish()
        self.assertTrue(self.guide.is_current(lease['event_id']))

    async def test_realtime_context_notifications_do_not_serialize_inventory(self):
        self.bridge.offer_context({'guidance':'steady','inventory':{'books':[{'title':'x'*10000}]*200}})
        kind,payload=self.bridge.outgoing.get_nowait()
        self.assertEqual(kind,'context'); self.assertLess(len(payload),300)
        self.assertTrue(json.loads(payload)['guide_updated'])

    async def test_lease_payload_is_short_even_with_long_source_descriptions(self):
        self.add_frame()
        for index in range(12):
            book=self.add_book(f'b{index}')
            ref=f'sources/p{index}.json'; self.store.put_json(self.runner.state,ref,{'price':'100'})
            book.replacement_cost=Price(amount='100',verified=True,currency='INR',country='India',
                source='Shop'*1000,url='https://example.com/book',source_ref=ref,
                retrieved_at='2026-10-20T00:00:00+00:00',condition_assumed='new equivalent'*1000,
                match='equivalent',match_basis='Edition unknown'*1000,product_name='Dune'*1000)
            book.used_value=book.replacement_cost.model_copy(deep=True)
        lease=self.bridge.next_guidance()
        self.assertLessEqual(len(json.dumps(lease['guide_cue'],ensure_ascii=False)),12000)
        self.assertTrue(lease['guide_cue']['records_truncated'])

    async def test_unspoken_price_cue_refreshes_benign_pending_status_change(self):
        self.add_frame(); book=self.add_book()
        self.store.put_json(self.runner.state,'sources/p.json',{'price':'100'})
        book.replacement_cost=Price(amount='100',verified=True,currency='INR',country='India',
            source='Shop',url='https://example.com/book',source_ref='sources/p.json',
            retrieved_at='2026-10-20T00:00:00+00:00',condition_assumed='new equivalent',match='equivalent')
        book.pricing_status='searching'
        self.guide.record('processed','f1',new_ids=['b1'],issues=['Glare'])
        first=self.bridge.next_guidance(); self.assertEqual(first['guide_cue']['stage'],'quality')
        self.guide.finish_realtime(first['event_id'])
        book.pricing_status='completed'
        import time
        with patch('claim_agent.sweep_guide.time.time',return_value=time.time()+9):
            second=self.bridge.next_guidance()
        self.assertIsNotNone(second)
        self.assertEqual(second['guide_cue']['stage'],'valuation')
        self.assertEqual(second['guide_cue']['records'][0]['pricing_status'],'completed')

    async def test_realtime_budget_reserves_completion_and_debounces_notifications(self):
        self.add_frame(); self.add_book(); self.guide.record('processed','f1',new_ids=['b1'])
        self.assertIsNotNone(self.bridge.next_guidance())
        self.add_frame('f2','queued'); self.guide.record('uploaded','f2')
        self.assertIsNone(self.bridge.next_guidance()); self.assertGreater(self.guide.realtime_delay(),0)
        self.guide.ledger['last_attempt_at']=0; self.guide.ledger['admissions']=47
        self.assertIsNone(self.bridge.next_guidance())
        self.runner.state.completion='partial'
        from claim_agent.packet import write_bundle
        write_bundle(self.runner.state,self.store); self.guide.record('complete',self.runner.state.sweep.id)
        self.assertEqual(self.bridge.next_guidance()['guide_cue']['stage'],'complete')
        self.assertEqual(self.guide.ledger['admissions'],48)
        self.assertIsNone(self.bridge.next_guidance())
