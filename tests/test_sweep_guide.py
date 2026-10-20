import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

from claim_agent.claude_assistant import AssistantResponse, ClaudeAssistant
from claim_agent.config import Settings
from claim_agent.models import Book, Item
from claim_agent.pipeline import SweepRunner
from claim_agent.storage import EvidenceStore
from tests.test_geometry import frame


class SweepGuideTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(Path(self.tmp.name))
        self.settings=Settings(data_dir=Path(self.tmp.name),provider='anthropic',assistant_max_calls=60)
        self.runner=SweepRunner(self.store.create('test','India','INR',synthetic=True),self.store,self.settings,None,None)
        self.addAsyncCleanup(self.runner.close)
        self.calls=[]; self.hold=None; self.begun=asyncio.Event()
        async def structured_text(prompt,schema,**kwargs):
            self.calls.append(json.loads(prompt)); self.begun.set()
            if self.hold: await self.hold.wait()
            return AssistantResponse(reply='Saved on the server. Keep the sweep gentle.')
        self.runner.provider=SimpleNamespace(usage=[],structured_text=structured_text)
        self.save=lambda:self.store.save(self.runner.state)
        self.runner.assistant=ClaudeAssistant(self.runner,self.settings,self.save)
        from claim_agent.sweep_guide import SweepGuide
        self.guide=SweepGuide(self.runner,self.settings,self.save); self.runner.guide=self.guide

    def add_frame(self,id='f1',status='queued'):
        meta=frame().model_copy(update={'id':id})
        self.runner.state.frames[id]=meta; self.runner.state.frame_status[id]=status
        self.store.put_evidence(self.runner.state,f'frames/{id}.jpg',b'frame')
        self.store.put_json(self.runner.state,f'observations/meta_{id}.json',meta.model_dump())
        self.save()

    def add_book(self,id='b1',frame_id='f1',status='identified'):
        book=Book(id=id,title='Dune' if status=='identified' else '',frame_ref=f'frames/{frame_id}.jpg',status=status,
                  evidence_refs=[f'frames/{frame_id}.jpg'])
        self.runner.state.books.append(book); self.save(); return book

    async def request(self,id=None):
        from claim_agent.sweep_guide import GuideRequest
        return await self.guide.respond(GuideRequest(request_id=id or uuid4()))

    async def test_empty_poll_never_calls_assistant(self):
        self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(self.calls,[])

    async def test_processed_supersedes_upload_and_batches_only_saved_records(self):
        self.add_frame(); self.guide.record('uploaded','f1')
        self.runner.state.frame_status['f1']='processed'; self.add_book()
        self.guide.record('processed','f1',new_ids=['b1'])
        first=await self.request()
        cue=self.calls[0]['server_context']['guide_cue']
        self.assertEqual(cue['stage'],'processed')
        self.assertEqual(cue['new_record_ids'],['b1'])
        self.assertTrue(cue['can_move_on'])
        self.assertEqual(self.calls[0]['request']['allow_actions'],False)
        self.assertNotIn('Dune',self.calls[0]['request']['text'])
        self.assertTrue(first['event_id'])
        self.assertNotIn('guide_cue',self.runner.voice_context())
        self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(len(self.calls),1)

    async def test_image_duplicate_is_not_inventory_duplicate_without_saved_identity(self):
        self.add_frame(); self.add_frame('f2')
        self.guide.record('duplicate','f2',source_frame_id='f1')
        await self.request()
        cue=self.calls[-1]['server_context']['guide_cue']
        self.assertEqual(cue['stage'],'duplicate_image')
        self.assertEqual(cue['repeated_record_ids'],[])
        self.assertFalse(cue['can_move_on'])

    async def test_duplicate_tied_to_processed_saved_record_is_explicit(self):
        self.add_frame(status='processed'); self.add_book(); self.add_frame('f2')
        self.guide.record('duplicate','f2',source_frame_id='f1')
        await self.request()
        cue=self.calls[-1]['server_context']['guide_cue']
        self.assertEqual(cue['stage'],'duplicate_inventory')
        self.assertEqual(cue['repeated_record_ids'],['b1'])

    async def test_failed_unknown_or_quality_frames_never_allow_move_on(self):
        for status,issues in [('failed',[]),('local_pending',[]),('processed',['Glare obscures title'])]:
            with self.subTest(status=status):
                self.add_frame(id=status,status=status)
                self.add_book(id=status,frame_id=status,status='unidentified')
                self.guide.record('processed',status,issues=issues)
                with patch('claim_agent.sweep_guide.time.time',return_value=1000+len(self.calls)*20):
                    await self.request()
                self.assertFalse(self.calls[-1]['server_context']['guide_cue']['can_move_on'])

    async def test_same_request_and_repeated_evidence_cannot_spend_twice(self):
        self.add_frame(status='processed'); self.add_book()
        self.guide.record('processed','f1',new_ids=['b1'])
        id=uuid4(); first=await self.request(id)
        self.assertEqual(await self.request(id),first)
        self.guide.record('processed','f1',new_ids=['b1'])
        with patch('claim_agent.sweep_guide.time.time',return_value=9999999999):
            self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(len(self.calls),1)

    async def test_owner_turn_lock_returns_empty_without_queueing_or_spend(self):
        self.add_frame(); self.guide.record('uploaded','f1')
        async with self.runner.assistant.lock:
            self.assertEqual(await asyncio.wait_for(self.request(),.1),{'reply':'','event_id':''})
        self.assertEqual(self.calls,[])
        self.assertTrue((await self.request())['reply'])

    async def test_cancellation_is_durable_and_never_retries_cue(self):
        self.add_frame(); self.guide.record('uploaded','f1')
        self.hold=asyncio.Event(); id=uuid4()
        task=asyncio.create_task(self.request(id))
        await asyncio.wait_for(self.begun.wait(),1); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        with self.assertRaises(HTTPException): await self.request(id)
        self.assertNotIn('guide_cue',self.runner.voice_context())
        self.hold=None
        from claim_agent.sweep_guide import SweepGuide
        self.runner.state=self.store.load(self.runner.state.sweep.id)
        self.guide=SweepGuide(self.runner,self.settings,self.save); self.runner.guide=self.guide
        with patch('claim_agent.sweep_guide.time.time',return_value=9999999999):
            self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(len(self.calls),1)

    async def test_final_summary_is_once_and_uses_packet_completion(self):
        self.add_frame(status='processed'); self.add_book()
        self.runner.state.completion='partial'
        from claim_agent.packet import write_bundle
        write_bundle(self.runner.state,self.store)
        self.guide.record('complete',self.runner.state.sweep.id)
        await self.request()
        cue=self.calls[-1]['server_context']['guide_cue']
        self.assertEqual(cue['stage'],'complete')
        self.assertEqual(cue['completion'],'partial')
        self.assertEqual(cue['totals']['replacement_prices_missing'],1)
        self.guide.record('complete',self.runner.state.sweep.id)
        self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(len(self.calls),1)

    async def test_pipeline_upload_processing_and_duplicate_hooks_use_actual_records(self):
        from tests.test_hybrid import jpeg, meta
        from claim_agent.vision import Scan, Detection
        self.runner.settings.local_first=False
        async def inspect(photo,metadata):
            return Scan(detections=[Detection(kind='book',title='Dune',visible_text='Dune',confidence=.9,
                polygon=[[.1,.1],[.2,.1],[.2,.4],[.1,.4]])])
        self.runner.vision=SimpleNamespace(provider=self.runner.provider,inspect=inspect)
        photo=jpeg(); await self.runner.upload(meta(),photo,b'',b'')
        self.assertTrue(any(c['facts']['stage']=='uploaded' for c in self.guide.ledger['cues']))
        self.runner.start(); await asyncio.wait_for(self.runner.queue.join(),1)
        await self.request()
        self.assertEqual(self.calls[-1]['server_context']['guide_cue']['stage'],'processed')
        self.assertEqual(len(self.calls[-1]['server_context']['guide_cue']['new_record_ids']),1)
        await self.runner.upload(meta('f2'),photo,b'',b'')
        with patch('claim_agent.sweep_guide.time.time',return_value=time_now()+10):
            await self.request()
        self.assertEqual(self.calls[-1]['server_context']['guide_cue']['stage'],'duplicate_inventory')
        await self.runner.finish(1)
        self.assertTrue(any(c['facts']['stage']=='complete' for c in self.guide.ledger['cues']))

    async def test_batch_reports_two_saved_records_without_individual_photo_requests(self):
        for frame_id,book_id in [('f1','b1'),('f2','b2')]:
            self.add_frame(frame_id,'processed'); self.add_book(book_id,frame_id)
            self.guide.record('processed',frame_id,new_ids=[book_id])
        await self.request(); cue=self.calls[-1]['server_context']['guide_cue']
        self.assertEqual(set(cue['new_record_ids']),{'b1','b2'})
        self.assertEqual(cue['record_count'],2)
        self.assertEqual(cue['batched_views'],2)

    async def test_quality_and_original_print_question_have_priority(self):
        self.add_frame(status='processed'); self.add_book(); self.guide.record('processed','f1',new_ids=['b1'])
        self.add_frame('art','processed')
        self.runner.state.items.append(Item(id='art1',category='portrait',frame_ref='frames/art.jpg',status='needs_appraisal',
            review_reasons=['Ask whether this is an original or a print']))
        self.guide.record('processed','art',new_ids=['art1'])
        await self.request(); cue=self.calls[-1]['server_context']['guide_cue']
        self.assertEqual(cue['stage'],'appraisal_question')
        self.assertEqual(cue['owner_questions'],[{'ref_id':'art1','question':'original_or_print'}])
        self.assertFalse(cue['can_move_on'])

    async def test_rate_limit_preserves_new_cue_for_next_idle_poll(self):
        now=time_now()
        self.add_frame(); self.guide.record('uploaded','f1')
        with patch('claim_agent.sweep_guide.time.time',return_value=now): await self.request()
        self.add_frame('f2'); self.guide.record('uploaded','f2')
        with patch('claim_agent.sweep_guide.time.time',return_value=now+1):
            self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(len(self.calls),1)
        with patch('claim_agent.sweep_guide.time.time',return_value=now+9): await self.request()
        self.assertEqual(len(self.calls),2)

    async def test_cues_and_paid_admissions_are_bounded(self):
        now=time_now()
        for index in range(49):
            id=f'f{index}'
            with patch('claim_agent.sweep_guide.time.time',return_value=now+index*9):
                self.add_frame(id); self.guide.record('uploaded',id); await self.request()
        self.assertLessEqual(len(self.guide.ledger['cues']),48)
        self.assertEqual(len(self.calls),47)
        self.runner.state.completion='partial'
        from claim_agent.packet import write_bundle
        write_bundle(self.runner.state,self.store)
        with patch('claim_agent.sweep_guide.time.time',return_value=now+49*9):
            self.guide.record('complete',self.runner.state.sweep.id); await self.request()
        self.assertEqual(len(self.calls),48)
        self.assertEqual(self.guide.ledger['admissions'],48)

    async def test_duplicate_of_previously_clear_view_cannot_hide_new_appraisal(self):
        self.add_frame(status='processed'); book=self.add_book()
        self.guide.record('processed','f1',new_ids=['b1'])
        book.status='needs_appraisal'; book.revision+=1
        self.add_frame('f2'); self.guide.record('duplicate','f2',source_frame_id='f1')
        await self.request()
        self.assertFalse(self.calls[-1]['server_context']['guide_cue']['can_move_on'])

    async def test_stale_view_cannot_request_move_on_or_spend(self):
        self.add_frame(status='processed'); self.add_book()
        self.guide.record('processed','f1',new_ids=['b1'])
        with patch('claim_agent.sweep_guide.time.time',return_value=time_now()+21):
            self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(self.calls,[])

    async def test_new_upload_means_older_processed_view_cannot_say_move_on(self):
        self.add_frame(status='processed'); self.add_book()
        self.guide.record('processed','f1',new_ids=['b1'])
        self.add_frame('f2'); self.guide.record('uploaded','f2')
        await self.request()
        self.assertFalse(self.calls[-1]['server_context']['guide_cue']['can_move_on'])

    async def test_same_inventory_repeated_in_new_frames_is_not_repeated_spend(self):
        now=time_now()
        self.add_frame(status='processed'); self.add_book(); self.add_frame('f2')
        self.guide.record('duplicate','f2',source_frame_id='f1'); await self.request()
        self.add_frame('f3'); self.guide.record('duplicate','f3',source_frame_id='f1')
        with patch('claim_agent.sweep_guide.time.time',return_value=now+9):
            self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(len(self.calls),1)

    async def test_same_quality_reason_new_frame_is_not_repeated_spend(self):
        now=time_now()
        for id in ['f1','f2']:
            self.add_frame(id,'processed'); self.guide.record('processed',id,issues=['Glare obscures the shelf'])
            with patch('claim_agent.sweep_guide.time.time',return_value=now+(9 if id=='f2' else 0)):
                await self.request()
        self.assertEqual(len(self.calls),1)

    async def test_new_clear_evidence_supersedes_quality_for_same_saved_record(self):
        self.add_frame(status='processed'); book=self.add_book()
        self.guide.record('processed','f1',new_ids=['b1'],issues=['Glare'])
        self.add_frame('f2','processed'); book.evidence_refs.append('frames/f2.jpg'); self.save()
        self.guide.record('processed','f2')
        await self.request(); cue=self.calls[-1]['server_context']['guide_cue']
        self.assertEqual(cue['stage'],'processed'); self.assertTrue(cue['can_move_on'])
        self.assertFalse(any(c['status']=='pending' and c['facts']['stage']=='quality' for c in self.guide.ledger['cues']))

    async def test_processing_overtakes_inflight_uploaded_reply(self):
        self.add_frame(); self.guide.record('uploaded','f1')
        self.hold=asyncio.Event(); task=asyncio.create_task(self.request())
        await asyncio.wait_for(self.begun.wait(),1)
        self.runner.state.frame_status['f1']='processed'; self.add_book()
        self.guide.record('processed','f1',new_ids=['b1']); self.hold.set()
        self.assertEqual(await task,{'reply':'','event_id':''})
        self.assertNotIn('guide_cue',self.runner.voice_context())
        with patch('claim_agent.sweep_guide.time.time',return_value=time_now()+9): await self.request()
        self.assertEqual(self.calls[-1]['server_context']['guide_cue']['stage'],'processed')

    async def test_different_pose_duplicate_image_is_not_same_inventory(self):
        self.add_frame(status='processed'); self.add_book(); self.add_frame('f2')
        self.runner.state.frames['f2'].camera_transform[12]=2
        self.guide.record('duplicate','f2',source_frame_id='f1'); await self.request()
        cue=self.calls[-1]['server_context']['guide_cue']
        self.assertEqual(cue['stage'],'duplicate_image')
        self.assertEqual(cue['repeated_record_ids'],[])

    async def test_busy_poll_request_id_cannot_be_reused_to_spend_later(self):
        self.add_frame(); self.guide.record('uploaded','f1')
        id=uuid4()
        async with self.guide.lock:
            self.assertEqual(await self.request(id),{'reply':'','event_id':''})
        self.assertEqual(await self.request(id),{'reply':'','event_id':''})
        self.assertEqual(self.calls,[])

    async def test_same_original_print_question_on_new_view_is_asked_once(self):
        self.add_frame('art','processed')
        art=Item(id='art1',category='portrait',frame_ref='frames/art.jpg',status='needs_appraisal',
            review_reasons=['Ask whether this is an original or a print'])
        self.runner.state.items.append(art)
        self.guide.record('processed','art',new_ids=['art1']); await self.request()
        self.add_frame('art2','processed'); art.evidence_refs.append('frames/art2.jpg'); self.save()
        self.guide.record('processed','art2')
        with patch('claim_agent.sweep_guide.time.time',return_value=time_now()+9):
            self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(len(self.calls),1)

    async def test_owner_correction_invalidates_pending_narration(self):
        self.add_frame(status='processed'); book=self.add_book()
        self.guide.record('processed','f1',new_ids=['b1'])
        book.excluded=True; book.revision+=1; self.save()
        self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(self.calls,[])

    async def test_completion_refreshes_counts_reviews_and_totals_before_admission(self):
        self.add_frame(status='processed')
        self.runner.state.items.append(Item(id='i1',category='chair',frame_ref='frames/f1.jpg'))
        self.runner.state.completion='partial'
        from claim_agent.packet import write_bundle
        write_bundle(self.runner.state,self.store)
        self.guide.record('complete',self.runner.state.sweep.id)
        await self.runner.correct({'action':'exclude','ref_id':'i1'})
        self.assertTrue((await self.request())['reply'])
        cue=self.calls[-1]['server_context']['guide_cue']; packet=self.runner.snapshot()
        self.assertEqual(cue['item_count'],0)
        self.assertEqual(cue['review_count'],len(packet.review_queue))
        self.assertEqual(cue['totals'],packet.totals.model_dump(mode='json'))
        self.assertEqual(cue['completion'],packet.completion)

    async def test_completion_correction_during_wording_discards_stale_summary(self):
        self.add_frame(status='processed')
        self.runner.state.items.append(Item(id='i1',category='chair',frame_ref='frames/f1.jpg'))
        self.runner.state.completion='partial'
        from claim_agent.packet import write_bundle
        write_bundle(self.runner.state,self.store)
        self.guide.record('complete',self.runner.state.sweep.id)
        self.hold=asyncio.Event(); task=asyncio.create_task(self.request())
        await asyncio.wait_for(self.begun.wait(),1)
        await self.runner.correct({'action':'exclude','ref_id':'i1'})
        self.hold.set()
        self.assertEqual(await task,{'reply':'','event_id':''})
        self.assertNotIn('guide_cue',self.runner.voice_context())

    async def test_thirteenth_record_correction_invalidates_pending_clear_view(self):
        self.add_frame(status='processed')
        for index in range(13): self.add_book(f'b{index}')
        self.guide.record('processed','f1',new_ids=[b.id for b in self.runner.state.books])
        await self.runner.correct({'action':'mark_unidentified','ref_id':'b12'})
        self.assertEqual(await self.request(),{'reply':'','event_id':''})
        self.assertEqual(self.calls,[])

    async def test_thirteenth_record_correction_discards_inflight_bounded_context(self):
        self.add_frame(status='processed')
        for index in range(13): self.add_book(f'b{index}')
        self.guide.record('processed','f1',new_ids=[b.id for b in self.runner.state.books])
        self.hold=asyncio.Event(); task=asyncio.create_task(self.request())
        await asyncio.wait_for(self.begun.wait(),1)
        cue=self.calls[-1]['server_context']['guide_cue']
        self.assertEqual(len(cue['records']),12); self.assertTrue(cue['records_truncated'])
        self.assertNotIn('record_guard',cue)
        await self.runner.correct({'action':'mark_unidentified','ref_id':'b12'})
        self.hold.set()
        self.assertEqual(await task,{'reply':'','event_id':''})

    async def test_prepared_batch_currentness_is_read_only_and_rejects_later_view(self):
        for frame_id,book_id in [('f1','b1'),('f2','b2')]:
            self.add_frame(frame_id,'processed'); self.add_book(book_id,frame_id)
            self.guide.record('processed',frame_id,new_ids=[book_id])
        result=await self.request()
        before=self.runner.state.model_dump_json()
        self.assertTrue(self.guide.is_current(result['event_id']))
        self.assertEqual(self.runner.state.model_dump_json(),before)
        self.assertFalse(self.guide.is_current('unrecognized'))
        self.add_frame('f3'); self.guide.record('uploaded','f3')
        self.assertFalse(self.guide.is_current(result['event_id']))

    async def test_prepared_currentness_expires_and_validates_omitted_records(self):
        self.add_frame(status='processed')
        for index in range(13): self.add_book(f'b{index}')
        self.guide.record('processed','f1',new_ids=[b.id for b in self.runner.state.books])
        result=await self.request()
        self.assertTrue(self.guide.is_current(result['event_id']))
        with patch('claim_agent.sweep_guide.time.time',return_value=time_now()+21):
            self.assertFalse(self.guide.is_current(result['event_id']))
        await self.runner.correct({'action':'mark_unidentified','ref_id':'b12'})
        self.assertFalse(self.guide.is_current(result['event_id']))

    async def test_prepared_completion_currentness_checks_all_packet_facts(self):
        self.add_frame(status='processed'); self.add_book()
        self.runner.state.completion='partial'
        from claim_agent.packet import write_bundle
        write_bundle(self.runner.state,self.store)
        self.guide.record('complete',self.runner.state.sweep.id)
        result=await self.request()
        before=self.runner.state.model_dump_json()
        self.assertTrue(self.guide.is_current(result['event_id']))
        self.assertEqual(self.runner.state.model_dump_json(),before)
        self.runner.state.review_reasons.append('Additional owner review required')
        self.assertFalse(self.guide.is_current(result['event_id']))


def time_now():
    import time
    return time.time()
