"""Bounded narration of server facts, independent of capture and owner turns."""
import asyncio
import hashlib
import json
import time
from copy import deepcopy
from uuid import UUID

from fastapi import HTTPException

from .claude_assistant import AssistantRequest
from .image_selection import stationary
from .models import Book, Model
from .packet import build_packet


class GuideRequest(Model):
    request_id: UUID


EMPTY = {'reply': '', 'event_id': ''}
PRIORITY = {'quality': 0, 'appraisal_question': 1, 'failed': 1, 'complete': 0,
            'pending': 2, 'processed': 3, 'valuation': 4, 'duplicate_inventory': 5, 'duplicate_image': 6, 'uploaded': 7}


class SweepGuide:
    def __init__(self, runner, settings, save_callback):
        self.runner, self.settings, self.save = runner, settings, save_callback
        self.lock = asyncio.Lock()
        ledger = self.ledger
        ledger.pop('active_cue', None)
        # A new service instance means any admitted text call may already be billed.
        for cue in ledger['cues']:
            if cue['status'] in {'admitted', 'leased'}: cue['status'] = 'interrupted'
        for receipt in ledger['requests'].values():
            if receipt.get('status') == 'pending': receipt['status'] = 'interrupted'
        for receipt in ledger['realtime_leases'].values():
            if receipt['status'] == 'leased': receipt['status'] = 'interrupted'
        self.persist()

    @property
    def ledger(self):
        ledger = self.runner.state.metrics.setdefault('sweep_guide', {})
        for key, default in {'cues': [], 'seen_events': {}, 'clear_frames': [], 'requests': {},
                             'admissions': 0, 'last_attempt_at': 0, 'views': {}, 'semantic_attempts': {},
                             'realtime_leases': {}}.items():
            ledger.setdefault(key, default)
        return ledger

    def persist(self):
        if self.save: self.save()

    def _records(self, frame_id):
        ref = f'frames/{frame_id}.jpg'
        if ref not in self.runner.state.evidence: return []
        return [record for record in self.runner.state.books + self.runner.state.items
                if record.frame_ref == ref or ref in record.evidence_refs]

    @staticmethod
    def _signature(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _record_guard(self, frame_id):
        records = sorted(self._records(frame_id), key=lambda record: record.id)
        return self._signature({'processing_status': self.runner.state.frame_status.get(frame_id, 'unknown'),
                                'records': [r.model_dump(mode='json') for r in records]})

    def _valuation_guard(self, record_id):
        record = next((r for r in self.runner.state.books + self.runner.state.items if r.id == record_id), None)
        if record is None: return ''
        prices = [record.replacement_cost] + ([record.used_value] if isinstance(record, Book) else [])
        refs = {ref for p in prices for ref in (p.source_ref, p.fx_source_ref) if ref}
        evidence = self.runner.state.evidence
        return self._signature({'record': record.model_dump(mode='json'),
            'sources': {ref: evidence[ref].model_dump(mode='json') if ref in evidence else None for ref in refs}})

    def _observe_valuations(self, now):
        """Derive a bounded notification only from currently sanitized sourced prices."""
        ledger = self.ledger
        if len(ledger['seen_events']) >= 1024: return
        packet = build_packet(self.runner.state)
        for record in packet.books + packet.items:
            if record.excluded: continue
            prices = []
            for kind, price in [('replacement', record.replacement_cost)] + ([('used', record.used_value)] if isinstance(record, Book) else []):
                values = price.model_dump(mode='json')
                if not price.verified or values.get('amount', values.get('low')) is None: continue
                fields = ('amount', 'low', 'high', 'currency', 'match', 'match_basis', 'product_name', 'condition_assumed', 'source', 'retrieved_at')
                prices.append({'kind': kind, **{key: values[key] for key in fields if key in values}})
            if not prices: continue
            guard = self._valuation_guard(record.id)
            event_id = self._signature(['valuation', record.id, prices])[:32]
            if event_id in ledger['seen_events']:
                # No response has been admitted for a pending quote. A sibling
                # lookup finishing can change pricing_status without changing it.
                for cue in ledger['cues']:
                    if cue['event_id'] == event_id and cue['status'] == 'pending':
                        cue['valuation_guard'] = guard
                        cue['facts']['records'][0]['pricing_status'] = record.pricing_status
                        cue['facts']['records'][0]['status'] = record.status
                continue
            ledger['seen_events'][event_id] = True
            facts = {'stage': 'valuation', 'evidence_ids': [], 'can_move_on': False,
                'new_record_ids': [], 'repeated_record_ids': [], 'issues': [], 'physical_accuracy_verified': False,
                'record_count': 1, 'records_truncated': False, 'records': [{'id': record.id,
                    'label': (record.title or 'Unidentified book') if isinstance(record, Book) else record.category,
                    'revision': record.revision, 'status': record.status, 'excluded': False,
                    'pricing_status': record.pricing_status, 'prices': prices}]}
            for cue in ledger['cues']:
                if cue.get('valuation_record_id') == record.id and cue['status'] in {'pending', 'leased'}:
                    cue['status'] = 'superseded'
            ledger['cues'].append({'event_id': event_id, 'semantic_key': event_id, 'status': 'pending',
                'created_at': now, 'facts': facts, 'valuation_record_id': record.id, 'valuation_guard': guard})
            ledger['cues'] = ledger['cues'][-48:]
            if len(ledger['seen_events']) >= 1024: break

    def _completion_snapshot(self):
        state = self.runner.state
        if state.completion not in {'ready', 'partial'}: return None
        if not (self.runner.store.directory(state.sweep.id) / 'packet.zip').exists(): return None
        packet = build_packet(state)
        facts = {'completion': packet.completion, 'packet_saved': True,
                 'totals': packet.totals.model_dump(mode='json'),
                 'item_count': sum(not r.excluded for r in packet.items), 'review_count': len(packet.review_queue)}
        # Provider usage changes during narration; inventory, geometry and review
        # facts must remain exactly the same as those supplied to the assistant.
        return facts, self._signature(packet.model_dump(mode='json', exclude={'metrics', 'revision'}))

    def _same_pose(self, frame_id, source_id):
        def metadata(id):
            if id in self.runner.state.frames:
                return self.runner.state.frames[id].model_dump()
            ref = f'observations/meta_{id}.json'
            if ref in self.runner.state.evidence:
                return json.loads(self.runner.store.read_evidence(self.runner.state, ref))
            return {}
        current, source = metadata(frame_id), metadata(source_id)
        return bool(current.get('tracking') == source.get('tracking') == 'normal'
                    and current.get('ar_session_id') == source.get('ar_session_id')
                    and stationary(current['camera_transform'], source['camera_transform']))

    @staticmethod
    def _semantic_key(facts):
        stage = facts['stage']
        ids = sorted(row['id'] for row in facts['records'] if not row['excluded'])
        if stage == 'appraisal_question':
            value = [stage, facts['owner_questions']]
        elif stage in {'quality', 'failed', 'pending'}:
            value = [stage, ids, sorted(s.casefold().strip() for s in facts['issues'])]
        elif stage == 'duplicate_inventory' or (stage == 'processed' and not facts['new_record_ids']):
            value = ['inventory_revisit', ids]
        else:
            value = [stage, facts['evidence_ids']]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:32]

    def record(self, stage, evidence_id, *, source_frame_id='', new_ids=(), issues=()):
        """Called after durable pipeline transitions, never with phone-proposed facts."""
        if stage not in {'uploaded', 'processed', 'duplicate', 'failed', 'complete'}: return
        state, ledger, now = self.runner.state, self.ledger, time.time()
        event_id = hashlib.sha256(f'{stage}:{evidence_id}'.encode()).hexdigest()[:32]
        if event_id in ledger['seen_events'] or (stage != 'complete' and len(ledger['seen_events']) >= 1024): return
        if stage != 'complete' and f'observations/meta_{evidence_id}.json' not in state.evidence: return
        if stage != 'complete' and evidence_id not in ledger['views']:
            ledger['views'][evidence_id] = now
            ledger['latest_view'] = evidence_id
            ledger['views'] = dict(list(ledger['views'].items())[-512:])
        facts = {'stage': stage, 'evidence_ids': [evidence_id], 'can_move_on': False,
                 'new_record_ids': [], 'repeated_record_ids': [], 'records': [],
                 'issues': list(dict.fromkeys(str(s)[:300] for s in issues if str(s).strip()))[:5],
                 'physical_accuracy_verified': False}
        records = self._records(source_frame_id if stage == 'duplicate' else evidence_id)
        if stage == 'uploaded':
            if f'frames/{evidence_id}.jpg' not in state.evidence: return
            facts.update(evidence_saved=True, recognition_pending=True)
        elif stage == 'complete':
            summary = self._completion_snapshot()
            if summary is None: return
            facts.update(summary[0])
        else:
            source = source_frame_id if stage == 'duplicate' else evidence_id
            status = state.frame_status.get(source, 'unknown')
            facts.update(evidence_saved=True, processing_status=status)
            facts['records'] = [{'id': r.id, 'revision': r.revision,
                                 'label': (r.title or 'Unidentified book') if isinstance(r, Book) else r.category,
                                 'status': r.status, 'excluded': r.excluded,
                                 'identity_supported': r.status == 'identified' if isinstance(r, Book) else bool(r.brand_model),
                                 'pricing_status': r.pricing_status} for r in records[:12]]
            ids = [r.id for r in records if not r.excluded]
            facts['record_count'] = len(ids)
            facts['records_truncated'] = len(records) > 12
            if stage == 'duplicate':
                linked = bool(status == 'processed' and ids and self._same_pose(evidence_id, source_frame_id))
                facts['stage'] = 'duplicate_inventory' if linked else 'duplicate_image'
                facts['source_frame_id'] = source_frame_id
                facts['repeated_record_ids'] = ids[:12] if linked else []
                if not linked:
                    facts['records'] = []; facts['record_count'] = 0; facts['records_truncated'] = False
                facts['can_move_on'] = bool(linked and source_frame_id in ledger['clear_frames']
                    and all(r.status not in {'unidentified', 'needs_appraisal'} for r in records if not r.excluded))
            elif stage == 'failed' or status == 'failed':
                facts['stage'] = 'failed'
            else:
                facts['new_record_ids'] = [id for id in ids if id in new_ids][:12]
                facts['repeated_record_ids'] = [id for id in ids if id not in new_ids][:12]
                questions = [{'ref_id': r.id, 'question': 'original_or_print'} for r in records
                             if not isinstance(r, Book) and not r.is_print and r.status == 'needs_appraisal'
                             and any('original or a print' in reason for reason in r.review_reasons)]
                facts['owner_questions'] = questions[:3]
                unknown = any(r.status in {'unidentified', 'needs_appraisal'} for r in records)
                if facts['issues']: facts['stage'] = 'quality'
                elif questions: facts['stage'] = 'appraisal_question'
                elif status != 'processed' or not ids or unknown: facts['stage'] = 'pending'
                else:
                    facts['can_move_on'] = True
                    ledger['clear_frames'] = list(dict.fromkeys(ledger['clear_frames'] + [evidence_id]))[-512:]
        ledger['seen_events'][event_id] = True
        semantic_key = self._semantic_key(facts)
        ids = {row['id'] for row in facts['records'] if not row['excluded']}
        # New results replace earlier views of the same inventory, including speech
        # still being prepared. Distinct new objects can be narrated as one batch.
        for cue in ledger['cues']:
            old_ids = {row['id'] for row in cue['facts']['records'] if not row['excluded']}
            overlap = bool(ids and old_ids and old_ids <= ids and stage != 'uploaded')
            if cue['status'] in {'pending', 'admitted', 'leased'} and (stage == 'complete'
                    or evidence_id in cue['facts']['evidence_ids'] or overlap
                    or semantic_key == cue.get('semantic_key')):
                cue['status'] = 'superseded'
        previous = ledger['semantic_attempts'].get(semantic_key)
        suppressed = previous is not None and (facts['stage'] == 'appraisal_question' or now - previous < 90)
        cue = {'event_id': event_id, 'semantic_key': semantic_key,
               'status': 'suppressed' if suppressed else 'pending', 'created_at': now, 'facts': facts}
        if facts['stage'] not in {'uploaded', 'complete', 'duplicate_image'}:
            # This compact guard covers every contributing record, independently
            # of the twelve-row display limit sent to the wording model.
            frame_id = source_frame_id if stage == 'duplicate' else evidence_id
            cue['validation_frame_id'] = frame_id
            cue['record_guard'] = self._record_guard(frame_id)
        ledger['cues'].append(cue)
        ledger['cues'] = ledger['cues'][-48:]
        self.persist()
        live = getattr(self.runner, 'live', None)
        if self.settings.provider == 'openai' and live is not None:
            live.offer_context({'guide_updated': True})

    def _batch(self, cues):
        facts = deepcopy(cues[0]['facts'])
        for field in ('evidence_ids', 'new_record_ids', 'repeated_record_ids', 'issues'):
            values = list(dict.fromkeys(value for cue in cues for value in cue['facts'].get(field, [])))
            facts[field] = values[:12]
        records = {row['id']: row for cue in cues for row in cue['facts'].get('records', [])}
        facts['records'] = list(records.values())[:12]
        facts['records_truncated'] = len(records) > 12 or any(c['facts'].get('records_truncated') for c in cues)
        if facts['stage'] != 'complete':
            facts['record_count'] = sum(not r['excluded'] for r in records.values())
            facts['record_count_is_partial'] = facts['records_truncated']
        facts['can_move_on'] = (all(c['facts']['can_move_on'] for c in cues)
                                and self.ledger.get('latest_view') in facts['evidence_ids'])
        facts['batched_views'] = len(cues)
        return facts

    def _still_supported(self, cue):
        facts = cue['facts']
        if 'valuation_guard' in cue:
            return self._valuation_guard(cue['valuation_record_id']) == cue['valuation_guard']
        if facts['stage'] == 'complete':
            summary = self._completion_snapshot()
            return summary is not None and summary[1] == cue.get('completion_guard')
        if 'record_guard' in cue:
            return self._record_guard(cue['validation_frame_id']) == cue['record_guard']
        # Older persisted cues did not guard omitted rows. Their display subset
        # cannot establish that all original records are still supported.
        if facts.get('records_truncated'): return False
        current = {r.id: r for r in self.runner.state.books + self.runner.state.items}
        return all(row['id'] in current and current[row['id']].revision == row['revision']
                   and current[row['id']].status == row['status'] and current[row['id']].excluded == row['excluded']
                   for row in facts['records'])

    def is_current(self, event_id: str) -> bool:
        """Read-only validity check for prepared text before and after speech synthesis."""
        ledger = self.runner.state.metrics.get('sweep_guide', {})
        realtime = ledger.get('realtime_leases', {}).get(event_id)
        receipt = realtime if realtime and realtime['status'] in {'leased', 'delivered'} else next((row for row in ledger.get('requests', {}).values()
                        if row.get('event_id') == event_id and row.get('status') == 'prepared'
                        and row.get('result', {}).get('event_id') == event_id
                        and row.get('result', {}).get('reply')), None)
        if not receipt or not receipt.get('cue_ids'): return False
        cues = {cue['event_id']: cue for cue in ledger.get('cues', [])}
        if any(id not in cues for id in receipt['cue_ids']): return False
        if receipt.get('can_move_on') and ledger.get('latest_view') not in receipt.get('evidence_ids', []):
            return False
        now = time.time()
        for id in receipt['cue_ids']:
            cue = cues[id]
            if cue['status'] not in ({'leased', 'delivered'} if realtime else {'prepared'}) or not self._still_supported(cue): return False
            stage = cue['facts']['stage']
            if stage not in {'complete', 'valuation'} and self.runner.state.completion != 'capturing': return False
            if stage != 'complete' and not 0 <= now - cue['created_at'] <= 20: return False
        return True

    def _pending_batch(self, now):
        pending = []
        for cue in self.ledger['cues']:
            if cue['status'] != 'pending': continue
            stage = cue['facts']['stage']
            if stage == 'complete':
                summary = self._completion_snapshot()
                if summary is not None:
                    cue['facts'].update(summary[0]); cue['completion_guard'] = summary[1]
            if ((stage != 'complete' and now - cue['created_at'] > 20) or not self._still_supported(cue)
                    or (stage not in {'complete', 'valuation'} and self.runner.state.completion != 'capturing')):
                cue['status'] = 'expired'
            else: pending.append(cue)
        pending.sort(key=lambda cue: (PRIORITY[cue['facts']['stage']], -cue['created_at']))
        if not pending or (self.ledger['admissions'] >= 47 and pending[0]['facts']['stage'] != 'complete'): return []
        size = 1 if pending[0]['facts']['stage'] in {'appraisal_question', 'complete'} else 6
        return [cue for cue in pending if cue['facts']['stage'] == pending[0]['facts']['stage']][:size]

    def realtime_delay(self):
        """One debounce delay after a server notification; this does not schedule polls."""
        return max(0, 8 - (time.time() - self.ledger['last_attempt_at'])) if self.ledger['last_attempt_at'] else 0

    def lease_realtime(self):
        """Atomically admit current facts for one response in the existing audio session."""
        if self.settings.provider != 'openai' or self.lock.locked(): return None
        ledger, now = self.ledger, time.time()
        if ledger['admissions'] >= 48 or self.realtime_delay() > 0: return None
        self._observe_valuations(now)
        selected = self._pending_batch(now)
        if not selected: return None
        event_id = self._signature(sorted(c['event_id'] for c in selected))[:32]
        if event_id in ledger['realtime_leases']: return None
        facts = self._batch(selected)
        # Keep wording context short while private signatures still cover every row.
        def bound(value):
            if isinstance(value, str): return value[:300]
            if isinstance(value, list): return [bound(row) for row in value[:12]]
            if isinstance(value, dict): return {key: bound(row) for key, row in value.items()}
            return value
        facts = bound(facts)
        while len(facts['records']) > 1 and len(json.dumps(facts, ensure_ascii=False)) > 12000:
            facts['records'].pop()
            facts['records_truncated'] = True
            facts['record_count_is_partial'] = True
        if facts['stage'] == 'complete':
            # Its current total already includes these quotes; do not narrate the
            # same pre-completion valuations again on the following notification.
            for cue in ledger['cues']:
                if cue not in selected and cue['status'] == 'pending': cue['status'] = 'superseded'
        for cue in selected:
            cue['status'] = 'leased'; ledger['semantic_attempts'][cue['semantic_key']] = now
        ledger['semantic_attempts'] = dict(list(ledger['semantic_attempts'].items())[-288:])
        ledger['admissions'] += 1; ledger['last_attempt_at'] = now
        ledger['realtime_leases'][event_id] = {'event_id': event_id, 'status': 'leased',
            'cue_ids': [cue['event_id'] for cue in selected], 'can_move_on': facts['can_move_on'],
            'evidence_ids': facts['evidence_ids']}
        self.persist()  # Admission is durable before the bridge can send response.create.
        return {'event_id': event_id, 'guide_cue': facts}

    def finish_realtime(self, event_id, *, outcome='delivered'):
        if outcome not in {'delivered', 'interrupted'}: raise ValueError('Unsupported guidance outcome')
        receipt = self.ledger['realtime_leases'].get(event_id)
        if not receipt or receipt['status'] != 'leased': return
        receipt['status'] = outcome
        for cue in self.ledger['cues']:
            if cue['event_id'] in receipt['cue_ids'] and cue['status'] == 'leased': cue['status'] = outcome
        self.persist()

    async def respond(self, body: GuideRequest):
        if self.settings.provider == 'openai': return dict(EMPTY)
        # A poll must not queue behind either another narration or the owner's speech.
        if self.lock.locked():
            requests, key = self.ledger['requests'], str(body.request_id)
            if key not in requests and len(requests) < 512:
                requests[key] = {'result': dict(EMPTY)}
                self.persist()
            return dict(EMPTY)
        async with self.lock:
            ledger, key = self.ledger, str(body.request_id)
            prior = ledger['requests'].get(key)
            if prior:
                if 'result' in prior: return deepcopy(prior['result'])
                raise HTTPException(503, 'Guidance was interrupted; it will not be retried')
            if len(ledger['requests']) >= 512: return dict(EMPTY)
            receipt = {'result': dict(EMPTY)}
            ledger['requests'][key] = receipt
            now = time.time()
            assistant = getattr(self.runner, 'assistant', None)
            if (assistant is None or assistant.lock.locked() or ledger['admissions'] >= 48
                    or (ledger['last_attempt_at'] and now - ledger['last_attempt_at'] < 8)):
                self.persist(); return dict(EMPTY)
            pending = []
            for cue in ledger['cues']:
                if cue['status'] != 'pending': continue
                if cue['facts']['stage'] == 'complete':
                    summary = self._completion_snapshot()
                    if summary is not None:
                        cue['facts'].update(summary[0])
                        cue['completion_guard'] = summary[1]
                if (cue['facts']['stage'] != 'complete' and now - cue['created_at'] > 20) or not self._still_supported(cue):
                    cue['status'] = 'expired'
                else: pending.append(cue)
            if not pending:
                self.persist(); return dict(EMPTY)
            pending.sort(key=lambda cue: (PRIORITY[cue['facts']['stage']], -cue['created_at']))
            # Preserve a final-summary slot within the same bounded text budget.
            if ledger['admissions'] >= 47 and pending[0]['facts']['stage'] != 'complete':
                self.persist(); return dict(EMPTY)
            batch_size = 1 if pending[0]['facts']['stage'] in {'appraisal_question', 'complete'} else 6
            selected = [c for c in pending if c['facts']['stage'] == pending[0]['facts']['stage']][:batch_size]
            event_id = hashlib.sha256('|'.join(sorted(c['event_id'] for c in selected)).encode()).hexdigest()[:32]
            for cue in selected: cue['status'] = 'admitted'
            receipt.clear(); receipt.update(status='pending', event_id=event_id)
            ledger['admissions'] += 1; ledger['last_attempt_at'] = now
            for cue in selected: ledger['semantic_attempts'][cue['semantic_key']] = now
            ledger['semantic_attempts'] = dict(list(ledger['semantic_attempts'].items())[-288:])
            ledger['active_cue'] = self._batch(selected)
            receipt.update(cue_ids=[cue['event_id'] for cue in selected],
                           can_move_on=ledger['active_cue']['can_move_on'],
                           evidence_ids=list(ledger['active_cue']['evidence_ids']))
            self.persist()
            try:
                result = await assistant.respond(AssistantRequest(request_id=body.request_id, mode='reply',
                    text='Give one brief natural spoken update for the current server_context.guide_cue.',
                    phase='sweep_guidance', language=self.runner.state.sweep.language, allow_actions=False))
                # Processing and owner corrections can overtake a paid wording call.
                # Its usage is retained, but stale narration must never reach playback.
                stale = any(c['status'] == 'superseded' or not self._still_supported(c)
                            or (c['facts']['stage'] != 'complete' and time.time() - c['created_at'] > 20) for c in selected)
                if ledger['active_cue']['can_move_on'] and self.ledger.get('latest_view') not in ledger['active_cue']['evidence_ids']:
                    stale = True
                output = dict(EMPTY) if stale else {'reply': result.reply[:1200], 'event_id': event_id}
                receipt.update(status='prepared', result=output)
                for cue in selected: cue['status'] = 'superseded' if stale else 'prepared'
                return output
            except BaseException:
                receipt['status'] = 'interrupted'
                for cue in selected: cue['status'] = 'interrupted'
                raise
            finally:
                ledger.pop('active_cue', None)
                self.persist()
