"""Authenticated, persisted same-frame depth ablation; never an accuracy benchmark.

Every replay gets a fresh runner/provider and a private evidence store. A filesystem
lock prevents duplicate spend even across server processes. Failed/interrupted runs
are returned on retry; retrying an HTTP request never authorizes a second analysis.
"""
import asyncio
import fcntl
import hashlib
import json
import time

from fastapi import Depends, HTTPException

from .models import Sweep, SweepState, utcnow
from .packet import build_packet
from .storage import EvidenceStore, atomic_write


LIMITATIONS = [
    'Same-frame depth ablation, not a pure camera versus LiDAR sensor benchmark: AR poses and tracking were captured during the LiDAR session and may be sensor-assisted.',
    'Camera tracking without depth cannot reliably merge repeated observations in this pipeline; counts are detection/track candidates with unresolved duplicate risk, not proven physical counts. This is a pipeline limitation, not a fundamental limit of cameras.',
    'Independent model calls are stochastic even with the same configured model and RGB frames. Output differences do not establish that LiDAR improves OCR.',
    'Camera replay inherits no inventory, prices, geometry, spatial tracks, owner corrections or physical item IDs. Owner-edited source responses may differ for that additional reason.',
    'The camera pipeline has no independent monocular scale or room reconstruction implementation; withheld depth and RoomPlan leave unsupported measurements unknown.',
    'No matched independent ground truth is supplied to the runtime. Counts, coverage and response differences are not accuracy metrics.',
    'Original timing includes capture overlap, voice and upload; replay timing starts from already saved frames. They are not directly comparable.',
]


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def _summary(state):
    packet = build_packet(state)
    processed = sum(v=='processed' for v in state.frame_status.values())
    # An empty result after zero successful analyses is unavailable, not zero objects.
    count_known = bool(processed or state.books or state.items)
    books = [b for b in state.books if not b.excluded]
    items = [i for i in state.items if not i.excluded]
    identified = sum(bool(b.title) and b.status=='identified' for b in books)
    measured = sum(b.spine_height_cm is not None and b.spine_thickness_cm is not None for b in books)
    item_measured = sum(all(v is not None for v in i.dimensions_cm.model_dump().values()) for i in items)
    return {
        'completion':state.completion, 'synthetic':state.sweep.synthetic,
        'book_count':len(books) if count_known else None,
        'object_count':len(items) if count_known else None,
        'count_basis':'pipeline inventory candidates; cross-frame identity requires review',
        'identity_coverage':{'identified_books':identified,'book_candidates':len(books),
                             'fraction':identified/len(books) if books else None},
        'measurement_coverage':{'books_with_both_spine_dimensions':measured,
                                'book_candidates':len(books),'fraction':measured/len(books) if books else None,
                                'objects_with_all_dimensions':item_measured,'object_candidates':len(items)},
        'room':{k:{'available':getattr(state.room,k) is not None,'value':getattr(state.room,k)}
                for k in ['floor_area_m2','wall_area_m2','net_wall_area_m2','shelved_wall_area_m2']},
        'review_count':len(packet.review_queue),
        'review_needs':[r.model_dump(mode='json') for r in packet.review_queue],
        'frames_saved':len(state.frames),'frames_processed':processed,
        'frames_failed':sum(v=='failed' for v in state.frame_status.values()),
        'frames_unprocessed':sum(v not in {'processed','failed','duplicate_suppressed','duplicate_geometry_processed'} for v in state.frame_status.values()),
        'response':{'books':[{'id':b.id,'title':b.title or None,'author':b.author or None,
                              'status':b.status,'frame_ref':b.frame_ref,'evidence_refs':b.evidence_refs,
                              'owner_claims_present':bool(b.user_claims),
                              'spine_height_cm':b.spine_height_cm,'spine_thickness_cm':b.spine_thickness_cm}
                             for b in books],
                    'objects':[{'id':i.id,'category':i.category,'description':i.description,
                                'frame_ref':i.frame_ref,'evidence_refs':i.evidence_refs,
                                'owner_claims_present':bool(i.user_claims),
                                'dimensions_cm':i.dimensions_cm.model_dump()} for i in items]},
        'provider_usage':state.metrics.get('provider_usage') or None,
        'live_provider_usage':state.metrics.get('live_usage') or None,
        'usage_scope':'Original source includes any recorded capture voice usage; camera replay includes fresh vision and pricing only',
        'cost_usd':state.metrics.get('cost_usd'),
        'cost_status':state.metrics.get('cost_status','Unknown: actual usage and billing rates unavailable'),
        'owner_corrections_present':bool(any(r.user_claims for r in state.books+state.items) or state.excluded_shelves),
    }


def _delta(camera,lidar):
    return camera-lidar if camera is not None and lidar is not None else None


def _write(path,value):
    atomic_write(path,json.dumps(value,indent=2,sort_keys=True,allow_nan=False).encode())


def register_comparison_routes(app, auth, runner_for, make_runner, runners, store, settings, active=None):
    """Register before the static mount. POST takes no body; GET never spends."""
    active=active if active is not None else set()

    def announce(runner, report):
        runner.publish({'type':'comparison_ready','report':report})
        if runner.live and report.get('status')=='complete':
            compact={}
            for mode in ['lidar','camera']:
                compact[mode]={k:report[mode][k] for k in ['book_count','object_count','identity_coverage','measurement_coverage','room','review_count','frames_processed']}
            runner.live.offer_context({'comparison_ready':True,'summary':compact,
                                       'analysis_status':report.get('analysis_status'),
                                       'deltas':report['deltas_camera_minus_lidar'],
                                       'limitations':report['limitations'],'accuracy':report['accuracy']})

    def paths(id):
        directory=store.directory(id)/'camera-comparison'
        return directory,directory/'comparison.json'

    @app.get('/api/sweeps/{id}/camera-comparison',dependencies=[Depends(auth)])
    async def get_comparison(id:str):
        await runner_for(id)
        _,path=paths(id)
        if not path.exists():
            raise HTTPException(404,'No camera comparison has been requested')
        return json.loads(path.read_bytes())

    @app.post('/api/sweeps/{id}/camera-comparison',dependencies=[Depends(auth)])
    async def compare(id:str):
        source_runner=await runner_for(id)
        directory,path=paths(id)
        directory.mkdir(parents=True,exist_ok=True)
        with (directory/'analysis.lock').open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise HTTPException(409,{'code':'already_running','message':'Camera comparison is already running; GET its status'})
            if path.exists():
                previous=json.loads(path.read_bytes())
                if previous['status']=='running':
                    # Holding the lock means no worker owns this persisted run.
                    previous.update(status='failed',workflow_complete=True,analysis_status='interrupted',finished_at=utcnow(),error='Server interrupted comparison; HTTP retries do not repeat provider spend')
                    _write(path,previous)
                announce(source_runner,previous)
                return previous
            async with source_runner.lock:
                source=source_runner.state.model_copy(deep=True)
            if source.completion not in {'ready','partial'}:
                raise HTTPException(409,{'code':'source_unavailable','message':'Finish the LiDAR sweep before comparing'})
            if getattr(source.sweep,'capture_mode','lidar')!='lidar':
                raise HTTPException(409,{'code':'source_unavailable','message':'Comparison requires a LiDAR source sweep'})
            if not source.frames or not (source.geometry or any(r.startswith('depth/') for r in source.evidence)):
                raise HTTPException(409,{'code':'source_unavailable','message':'Source needs original RGB frames and LiDAR depth or RoomPlan evidence'})
            models=source.metrics.get('capture_models',{})
            expected={'provider':settings.provider,'vision_model':settings.vision_model}
            if any(models.get(k)!=v for k,v in expected.items()):
                reason='This older sweep does not record its AI model. Capture a new LiDAR sweep for a controlled comparison.' if any(not models.get(k) for k in expected) else 'The analysis model changed after this sweep. Capture a new LiDAR sweep for a controlled comparison.'
                raise HTTPException(409,{'code':'source_unavailable','message':reason})
            if len(source.frames)>settings.max_frames:
                raise HTTPException(409,{'code':'source_unavailable','message':'Source exceeds replay frame budget'})
            capacity=max(1,settings.max_sweeps)
            capturing=sum(r.state.completion in {'capturing','processing'} for r in runners.values())
            if len(active)+capturing>=capacity:
                raise HTTPException(409,{'code':'capacity','message':'Analysis capacity reached; finish active work before comparing'})
            report={'schema_version':1,'workflow_complete':False,'analysis_status':'running','status':'running','source_sweep_id':id,'source_revision':source.revision,
                    'started_at':utcnow(),'synthetic':source.sweep.synthetic,
                    'experiment':'same-frame depth ablation','models':models,
                    'locale':{'language':getattr(source.sweep,'language','English'),'country':source.sweep.country,'currency':source.sweep.currency},
                    'limitations':LIMITATIONS,'lidar':_summary(source),'camera':None,
                    'accuracy':{'available':False,'reason':'Independent matched ground truth unavailable',
                                'book_count':None,'identity':None,'measurements':None,'room_area':None},
                    'provenance':{'source_state_sha256':_digest(source.model_dump(mode='json')),
                                  'source_owner_corrections_present':any(r.user_claims for r in source.books+source.items),
                                  'reused_ar_poses':True,'depth_withheld':True,'roomplan_withheld':True,'frames':[]}}
            runner=None
            started=time.monotonic()
            active.add(id)
            try:
                _write(path,report)
                # Store the frozen source packet independently of future owner edits.
                _write(directory/'lidar_packet.json',build_packet(source).model_dump(mode='json'))
                replay_store=EvidenceStore(directory/'replay',blob_root=store.blob_root)
                camera=replay_store.create(source.sweep.device,source.sweep.country,source.sweep.currency,synthetic=source.sweep.synthetic)
                sweep_values=source.sweep.model_dump()
                sweep_values.update(id=camera.sweep.id,captured_at=utcnow(),duration_s=None,ended_at=None,packet_ready_at=None)
                if 'capture_mode' in sweep_values:
                    sweep_values.update(capture_mode='camera',comparison_requested=False)
                camera.sweep=Sweep.model_validate(sweep_values)
                camera.metrics['capture_models']=dict(models)
                runner=make_runner(camera)
                runner.store=replay_store
                runner.start()
                async with asyncio.timeout(settings.finish_timeout):
                    for frame_id,original in source.frames.items():
                        jpeg=store.read_evidence(source,f'frames/{frame_id}.jpg')
                        meta=original.model_copy(update={'depth_width':0,'depth_height':0},deep=True)
                        report['provenance']['frames'].append({
                            'source_frame_id':frame_id,'camera_frame_id':frame_id,
                            'source_jpeg_sha256':hashlib.sha256(jpeg).hexdigest(),
                            'camera_jpeg_sha256':hashlib.sha256(jpeg).hexdigest(),
                            'source_metadata_sha256':_digest(original.model_dump(mode='json')),
                            'camera_metadata_sha256':_digest(meta.model_dump(mode='json')),
                            'same_intrinsics':original.intrinsics==meta.intrinsics,
                            'same_camera_transform':original.camera_transform==meta.camera_transform})
                        admission=await runner.upload(meta,jpeg,b'',b'')
                        if admission['processing']=='duplicate_suppressed':
                            report['provenance']['frames'][-1]['duplicate_of'] = admission['duplicate_of']
                        elif admission['processing']!='queued':
                            raise RuntimeError('Replay admission failed; comparison cannot omit frames')
                        # Block admission instead of allowing live capture's drop policy.
                        await runner.queue.join()
                    await runner.finish(0)
                camera.metrics['provider_usage']=getattr(runner.provider,'usage',None)
                _write(directory/'camera_packet.json',build_packet(camera).model_dump(mode='json'))
                lidar_summary,camera_summary=_summary(source),_summary(camera)
                report.update(status='complete',workflow_complete=True,
                              analysis_status='failed' if not camera_summary['frames_processed'] else 'partial' if camera_summary['frames_failed'] or camera_summary['frames_unprocessed'] else 'completed',
                              finished_at=utcnow(),lidar=lidar_summary,camera=camera_summary,
                              timing={'directly_comparable':False,'lidar_time_to_packet_s':source.metrics.get('time_to_packet_s'),
                                      'lidar_basis':source.metrics.get('timing_basis','Original capture end to packet, including concurrent work and uploads'),
                                      'camera_replay_wall_s':time.monotonic()-started,
                                      'camera_basis':'Fresh server replay from already saved RGB frames; includes evidence copy, vision, independent pricing and packet finalization'},
                              deltas_camera_minus_lidar={
                                  **{k:_delta(camera_summary[k],lidar_summary[k]) for k in ['book_count','object_count','review_count']},
                                  **{k:_delta(camera_summary['room'][k]['value'],lidar_summary['room'][k]['value']) for k in camera_summary['room']}})
                _write(path,report)
            except BaseException as error:
                report.update(status='failed',workflow_complete=True,analysis_status='interrupted',finished_at=utcnow(),
                              error='Comparison deadline reached' if isinstance(error,TimeoutError) else f'Comparison interrupted ({type(error).__name__}); HTTP retries do not repeat provider spend')
                if runner:
                    runner.state.metrics['provider_usage']=getattr(runner.provider,'usage',None)
                    report['camera']=_summary(runner.state)
                    report['camera']['completion']='interrupted'
                _write(path,report)
                if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)):
                    raise
            finally:
                try:
                    if runner:
                        try:
                            await runner.close()
                        finally:
                            await runner.provider.close()
                finally:
                    active.discard(id)
            announce(source_runner,report)
            return report
