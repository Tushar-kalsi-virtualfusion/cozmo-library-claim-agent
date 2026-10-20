import asyncio
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from claim_agent.models import Book, Item, Locale, Price
from claim_agent.pricing import PriceProvider, extract_offers
from claim_agent.pipeline import SweepRunner
from claim_agent.config import Settings
from claim_agent.storage import EvidenceStore
from claim_agent.costing import usage_cost
from claim_agent.providers import ProviderUnavailable
from tests import test_anthropic as anthropic_fixtures
from tests.test_anthropic import response
from tests.test_pricing import page


class SearchTests(unittest.IsolatedAsyncioTestCase):
    provider = anthropic_fixtures.ClaudeProviderTests.provider

    def search_response(self, content=None, stop='end_turn'):
        body = response(stop=stop)
        body['content'] = content if content is not None else [
            {'type':'server_tool_use','id':'srv_1','name':'web_search','input':{'query':'lamp India'}},
            {'type':'web_search_tool_result','tool_use_id':'srv_1','content':[
                {'type':'web_search_result','title':'Lamp','url':'https://example.com/lamp','encrypted_content':'fixture'},
                {'type':'web_search_result','title':'Duplicate','url':'https://example.com/lamp','encrypted_content':'fixture'},
                {'type':'web_search_result','title':'Private','url':'http://127.0.0.1/private','encrypted_content':'fixture'}]},
            {'type':'text','text':'https://invented.example/price costs 999'}]
        body['usage']['server_tool_use']={'web_search_requests':1}
        return body

    async def test_only_validated_search_results_reach_pricer_with_separate_budget(self):
        provider=await self.provider(self.search_response(),max_calls=0,max_search_calls=1,workspace_id='wrkspc_fixture')
        with patch('claim_agent.anthropic_provider.public_url',side_effect=lambda url: (_ for _ in ()).throw(ValueError()) if '127.0.0.1' in url else 'example.com'):
            self.assertEqual(await provider.search_urls('lamp India'),['https://example.com/lamp'])
        payload=json.loads(self.requests[0].content)
        self.assertEqual(payload['tools'],[{'type':'web_search_20250305','name':'web_search','max_uses':1}])
        self.assertEqual(self.requests[0].headers['anthropic-workspace-id'],'wrkspc_fixture')
        self.assertEqual(provider.usage[-1]['usage']['server_tool_use']['web_search_requests'],1)
        with self.assertRaisesRegex(ProviderUnavailable,'budget'):
            await provider.search_urls('another lamp')
        self.assertEqual(len(self.requests),1)

    async def test_embedded_error_and_paused_search_are_explicit_and_costed(self):
        for body in [self.search_response([{'type':'web_search_tool_result','tool_use_id':'srv_1','content':{'type':'web_search_tool_result_error','error_code':'unavailable'}}]),self.search_response(stop='pause_turn')]:
            provider=await self.provider(body)
            with self.assertRaises(ProviderUnavailable):
                await provider.search_urls('lamp')
            self.assertEqual(len(provider.usage),1)
            self.assertEqual(provider.usage[0]['usage']['input_tokens'],1000)

    async def test_text_only_response_cannot_supply_search_urls(self):
        provider=await self.provider(response('https://example.com/fake'))
        with self.assertRaisesRegex(ProviderUnavailable,'search'):
            await provider.search_urls('lamp')

    def test_opus_and_reported_search_usage_are_costed_once(self):
        usage=response()['usage']|{'cache_read_input_tokens':2000,'cache_creation_input_tokens':1000,
            'cache_creation':{'ephemeral_5m_input_tokens':600,'ephemeral_1h_input_tokens':400},
            'server_tool_use':{'web_search_requests':2}}
        row={'provider':'anthropic','model':'claude-opus-5-5','response_id':'msg_opus','usage':usage}
        result=usage_cost([row,row])
        self.assertAlmostEqual(result['cost_usd'],.0326)
        self.assertEqual(result['costed_requests'],1)


class OfferIntegrityTests(unittest.TestCase):
    def offers(self, product, query):
        doc='<script type="application/ld+json">'+json.dumps(product|{'@type':'Product','offers':{
            '@type':'Offer','price':'799.50','priceCurrency':'INR','availability':'https://schema.org/InStock',
            'itemCondition':'https://schema.org/NewCondition','eligibleRegion':'IN'}})+'</script>'
        return extract_offers(doc,'https://shop.example/product',query,Locale(country='India',currency='INR'),'new')

    def test_item_accessory_is_not_a_compatible_replacement(self):
        q={'kind':'item','title':'wood chair','category':'chair','material':'wood','brand_model':''}
        self.assertEqual(self.offers({'name':'wood chair cover','category':'chair cover','material':'wood'},q),[])

    def test_generic_equivalent_has_explicit_product_and_matching_basis(self):
        q={'kind':'item','title':'wood chair','category':'chair','material':'wood','brand_model':''}
        offers=self.offers({'name':'Modern wood chair','category':'chair','material':'wood'},q)
        self.assertEqual(len(offers),1)
        price=offers[0][0]
        self.assertEqual(price.match,'equivalent')
        self.assertTrue(getattr(price,'match_basis',''))
        self.assertEqual(getattr(price,'product_name',''),'Modern wood chair')

    def test_model_name_alone_does_not_establish_exact_identity(self):
        q={'kind':'item','title':'Acme L20','category':'lamp','material':'','brand_model':'Acme L20'}
        exact=self.offers({'name':'Acme L20 lamp','category':'lamp','brand':{'name':'Acme'},'model':'L20'},q)
        self.assertEqual(exact[0][0].match,'exact')
        self.assertEqual(self.offers({'name':'Acme L20 compatible lamp','category':'lamp','brand':'Other','model':'L20'},q),[])

    def test_nonfinite_source_dimensions_cannot_establish_equivalence(self):
        q={'kind':'item','title':'chair','category':'chair','dimensions_cm':{'w':50}}
        self.assertEqual(self.offers({'name':'chair','category':'chair','width':{'value':'NaN','unitCode':'CMT'}},q),[])


class QuoteCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_no_offer_search_is_coalesced_and_negative_cached(self):
        class Search:
            def __init__(self): self.calls=0
            async def search_urls(self,query):
                self.calls+=1
                await asyncio.sleep(.01)
                return []
        search=Search(); prices=PriceProvider(search)
        q={'title':'Missing book'}; locale=Locale(country='India',currency='INR')
        results=await asyncio.gather(*(prices.quote(q,locale,'new') for _ in range(4)))
        await prices.quote(q,locale,'new')
        self.assertEqual(search.calls,2)  # One local and one explicitly converted-market attempt.
        self.assertTrue(all(p.amount is None for p,_ in results))


class PricingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(Path(self.tmp.name))
        state=self.store.create('test','India','INR',synthetic=True)
        self.store.put_evidence(state,'frames/f1.jpg',b'frame')
        self.runner=SweepRunner(state,self.store,Settings(data_dir=Path(self.tmp.name),paid_pricing=True),None,None)
        self.addAsyncCleanup(self.runner.close)
        self.runner.state.books=[Book(id='b1',frame_ref='frames/f1.jpg',status='identified',title='A Book')]

    def price(self, amount='100'):
        return Price(amount=amount,currency='INR',country='India',source='Shop',url='https://example.com/book',
            retrieved_at='2026-10-20T00:00:00Z',source_ref='sources/price.json',verified=True,
            condition_assumed='new',match='equivalent',exclusion_reason='')

    async def test_replacement_publishes_while_used_pending_and_admission_deduplicates(self):
        new_ready=asyncio.Event(); release_used=asyncio.Event(); calls=[]
        async def quote(query,locale,kind):
            calls.append(kind)
            if kind=='used': await release_used.wait()
            else: new_ready.set()
            return self.price(),[('sources/price.json',{'price':'100'})]
        from types import SimpleNamespace
        self.runner.prices=SimpleNamespace(quote=quote)
        events=asyncio.Queue(); self.runner.listeners.add(events)
        self.runner.start()
        self.runner.enqueue_price(self.runner.state.books[0]); self.runner.enqueue_price(self.runner.state.books[0])
        await asyncio.wait_for(new_ready.wait(),1)
        for _ in range(20):
            if self.runner.snapshot().books[0].replacement_cost.amount is not None: break
            await asyncio.sleep(.01)
        self.assertEqual(self.runner.snapshot().books[0].replacement_cost.amount,Decimal('100'))
        self.assertFalse(release_used.is_set())
        release_used.set()
        await asyncio.wait_for(self.runner.price_queue.join(),1)
        self.assertEqual(sorted(calls),['new','used'])
        self.assertEqual(self.runner.state.books[0].pricing_status,'completed')
        self.assertTrue(any(e['type']=='inventory' for e in list(events._queue)))

    async def test_resume_never_retries_ambiguous_search_but_requeues_unstarted_work(self):
        self.runner.enqueue_price(self.runner.state.books[0])
        self.assertTrue(hasattr(self.runner.state,'pricing_jobs'))
        jobs=self.runner.state.pricing_jobs
        jobs['b1:new']['status']='searching'
        self.store.save(self.runner.state)
        recovered=SweepRunner(self.store.load(self.runner.state.sweep.id),self.store,self.runner.settings,None,None)
        self.addAsyncCleanup(recovered.close)
        recovered.resume_pricing()
        self.assertEqual(recovered.state.pricing_jobs['b1:new']['status'],'interrupted')
        self.assertEqual(recovered.price_queue.qsize(),1)

    async def test_legacy_saved_inventory_only_admits_unstarted_supported_records_once(self):
        state=self.runner.state
        state.sweep.synthetic=False
        state.completion='partial'
        state.items=[Item(id='lamp',category='lamp',frame_ref='frames/f1.jpg'),
                     Item(id='unknown',category='Object',frame_ref='frames/f1.jpg'),
                     Item(id='art',category='painting',frame_ref='frames/f1.jpg',status='needs_appraisal'),
                     Item(id='old',category='chair',frame_ref='frames/f1.jpg'),
                     Item(id='missing',category='table',frame_ref='frames/missing.jpg')]
        state.metrics['pricing_stages']=[{'ref_id':'old','kind':'new'}]
        state.books[0].replacement_cost=self.price()
        with patch.object(self.runner,'_start_pricing_workers'):
            self.runner.resume_pricing()
            self.runner.resume_pricing()
        self.assertEqual(set(state.pricing_jobs),{'lamp:new'})
        self.assertEqual(self.runner.price_queue.qsize(),1)
        self.store.save(state)
        recovered=SweepRunner(self.store.load(state.sweep.id),self.store,self.runner.settings,None,None)
        self.addAsyncCleanup(recovered.close)
        with patch.object(recovered,'_start_pricing_workers'):
            recovered.resume_pricing()
        self.assertEqual(set(recovered.state.pricing_jobs),{'lamp:new'})
        self.assertEqual(recovered.price_queue.qsize(),1)

    async def test_legacy_migration_does_not_retry_unattributed_search_or_interrupted_job(self):
        state=self.runner.state
        state.sweep.synthetic=False
        state.completion='partial'
        state.metrics['provider_usage']=[{'request_kind':'search','usage':None}]
        state.items=[Item(id='lamp',category='lamp',frame_ref='frames/f1.jpg')]
        self.runner.resume_pricing()
        self.assertEqual(state.pricing_jobs,{})
        self.assertEqual(self.runner.price_queue.qsize(),0)

    async def test_legacy_provider_rows_without_request_kind_do_not_repeat_search(self):
        for row in [{'provider':'openai','web_search_calls':1,'usage':None},
                    {'provider':'gemini','usage':{'input_tokens':100}}]:
            with self.subTest(provider=row['provider']):
                state=self.runner.state
                state.sweep.synthetic=False; state.completion='partial'
                state.metrics={'provider_usage':[row]}
                self.runner.pricing_resumed=False
                self.runner.resume_pricing()
                self.assertEqual(state.pricing_jobs,{})
                self.assertEqual(self.runner.price_queue.qsize(),0)

    def test_generic_object_without_identity_cannot_be_priced(self):
        from claim_agent.pricing import can_price_record
        self.assertFalse(can_price_record(Item(id='unknown',category='Object',frame_ref='frames/f1.jpg')))

    async def test_late_equivalent_preserves_match_and_refreshes_bundle(self):
        self.runner.state.books=[]
        self.runner.state.items=[Item(id='i1',category='lamp',frame_ref='frames/f1.jpg')]
        self.runner.state.completion='partial'
        await self.runner.apply_prices('i1',0,self.price(),Price(),[('sources/price.json',{'price':'100'})])
        result=self.runner.snapshot().items[0].replacement_cost
        self.assertEqual(getattr(result,'match','unknown'),'equivalent')
        bundle=self.store.directory(self.runner.state.sweep.id)/'claim_packet.json'
        self.assertTrue(bundle.exists())
        self.assertEqual(json.loads(bundle.read_text())['items'][0]['replacement_cost']['low'],'100')

    def scan_detection(self, number, **fields):
        import numpy as np
        from tests.test_geometry import frame
        from claim_agent.vision import Scan, Detection
        meta=frame().model_copy(update={'id':f'f{number}'})
        meta.ocr=[{'text':fields.get('visible_text',''),'center':[.5,.5]}]
        self.store.put_evidence(self.runner.state,f'frames/{meta.id}.jpg',b'frame')
        self.store.put_evidence(self.runner.state,f'depth/{meta.id}.bin',np.full((100,100),2,dtype='<f4').tobytes())
        self.store.put_evidence(self.runner.state,f'depth/{meta.id}.confidence',bytes([2])*10000)
        self.runner.apply_scan(meta,Scan(detections=[Detection(polygon=[[.45,.45],[.55,.45],[.55,.55],[.45,.55]],**fields)]))

    async def test_more_confident_same_book_does_not_invalidate_or_repeat_quote(self):
        self.runner.state.books=[]
        self.scan_detection(1,kind='book',title='Dune',visible_text='Dune',confidence=.8)
        book=self.runner.state.books[0]; revision=book.revision
        book.replacement_cost=self.price()
        self.scan_detection(2,kind='book',title='Dune',visible_text='Dune',confidence=.95)
        self.assertEqual(len(self.runner.state.books),1)
        self.assertEqual(book.revision,revision)
        self.assertEqual(book.replacement_cost.amount,Decimal('100'))
        self.assertEqual(self.runner.price_queue.qsize(),2)

    async def test_supported_item_enrichment_reprices_and_rejects_old_quote(self):
        self.runner.state.books=[]
        self.scan_detection(1,kind='item',category='lamp',confidence=.8)
        item=self.runner.state.items[0]; previous=item.revision
        self.scan_detection(2,kind='item',category='lamp',material='metal',brand_model='Acme L20',visible_text='Acme L20',confidence=.95)
        self.assertEqual(len(self.runner.state.items),1)
        self.assertEqual(item.brand_model,'Acme L20')
        self.assertGreater(item.revision,previous)
        self.assertFalse(await self.runner.apply_quote(item.id,previous,'new',self.price(),[('sources/price.json',{})]))
        revision=item.revision
        self.scan_detection(3,kind='item',category='lamp',material='metal',brand_model='Acme L20',visible_text='Acme L20',confidence=.99)
        self.assertEqual(item.revision,revision)

    async def test_exclusion_updates_pricing_status_and_blocks_late_result(self):
        book=self.runner.state.books[0]
        self.runner.enqueue_price(book)
        await self.runner.correct({'action':'exclude','ref_id':'b1'})
        self.assertEqual(book.pricing_status,'unavailable')
        self.assertFalse(await self.runner.apply_quote('b1',0,'new',self.price(),[]))

    async def test_search_cancellation_is_durable_and_does_not_leave_searching_state(self):
        from types import SimpleNamespace
        begun=asyncio.Event()
        async def quote(*args):
            begun.set(); await asyncio.Event().wait()
        self.runner.prices=SimpleNamespace(quote=quote)
        self.runner.enqueue_price(self.runner.state.books[0]); self.runner.start()
        await asyncio.wait_for(begun.wait(),1)
        await self.runner.close()
        state=self.store.load(self.runner.state.sweep.id)
        self.assertTrue(all(j['status']!='searching' for j in state.pricing_jobs.values()))
        self.assertEqual(state.books[0].pricing_status,'interrupted')

    async def test_speech_usage_and_pending_admission_are_included_in_snapshot(self):
        self.runner.state.metrics['speech_usage']=[{'provider':'elevenlabs','model':'eleven_v3','request_id':'done','usage':None}]
        self.runner.state.metrics['speech_requests']={'pending':{'provider':'elevenlabs','model':'eleven_v3','status':'pending'}}
        packet=self.runner.snapshot()
        self.assertEqual(packet.metrics['cost_status'],'unavailable')
        self.assertEqual(packet.metrics['uncosted_requests'],2)

    async def test_resume_is_idempotent_without_duplicate_queue_entries(self):
        self.runner.enqueue_price(self.runner.state.books[0]); self.store.save(self.runner.state)
        recovered=SweepRunner(self.store.load(self.runner.state.sweep.id),self.store,self.runner.settings,None,None)
        self.addAsyncCleanup(recovered.close)
        recovered.resume_pricing(); recovered.resume_pricing()
        self.assertEqual(recovered.price_queue.qsize(),2)

    async def test_unsupported_provider_amount_cannot_mark_record_completed(self):
        book=self.runner.state.books[0]
        self.runner.enqueue_price(book)
        await self.runner.apply_quote('b1',0,'new',Price(amount='999',verified=False),[])
        self.assertIsNone(book.replacement_cost.amount)
        self.assertEqual(self.runner.state.pricing_jobs['b1:new']['status'],'unavailable')

    async def test_voice_context_has_sanitized_values_and_exclusion_state(self):
        book=self.runner.state.books[0]; book.replacement_cost=self.price(); book.excluded=True
        row=self.runner.voice_context()['books'][0]
        self.assertTrue(row.get('excluded'))
        self.assertIn('replacement_cost',row)
        self.assertIsNone(row['replacement_cost']['amount'])
        self.assertIn('pricing_status',row)

    async def test_post_deadline_print_confirmation_can_finish_new_valuation(self):
        from types import SimpleNamespace
        self.runner.state.books=[]
        self.runner.state.items=[Item(id='i1',category='portrait',frame_ref='frames/f1.jpg',status='needs_appraisal')]
        self.runner.settings.finish_timeout=.001
        await self.runner.price_queue.put(('stale',0,'new','old'))
        await self.runner.finish(1)
        async def quote(*args): return self.price(),[('sources/price.json',{'price':'100'})]
        self.runner.prices=SimpleNamespace(quote=quote)
        await self.runner.correct({'action':'confirm_print','ref_id':'i1'})
        await asyncio.wait_for(self.runner.price_queue.join(),.2)
        self.assertEqual(self.runner.snapshot().items[0].replacement_cost.low,Decimal('100'))
