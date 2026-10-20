"""Read-only, evidence-backed preview of conservative item identity repair."""
import copy
import json
from collections import defaultdict

from .config import Settings
from .models import FrameMetadata, PriceRange
from .pipeline import SweepRunner
from .pricing import pricing_query
from .vision import Scan


class _ReadOnlyEvidence:
    def __init__(self, store):
        self.read_evidence = store.read_evidence


def repair_inventory(store, state):
    """Return a repaired copy and report; never save, start workers or call a provider.

    Immutable overlays retain their original IDs. Consumers can resolve those
    through ``metrics.inventory_aliases`` after explicitly saving this preview.
    Books, unmatched items and uncertain histories remain intact.
    """
    repaired = state.model_copy(deep=True)
    report = {'before_items': len(state.items), 'after_items': len(state.items), 'merged_items': 0,
              'replayed_frames': 0, 'skipped_books': len(state.books), 'ambiguous_ids': [],
              'incomplete_ids': [], 'blocked_ids': [], 'aliases': {}, 'errors': []}
    items = {item.id: item for item in state.items}
    aliases = dict(state.metrics.get('inventory_aliases', {}))

    def resolve(id):
        seen = set()
        while id in aliases:
            if id in seen:
                return None
            seen.add(id)
            id = aliases[id]
        return id

    def read_json(ref):
        return json.loads(store.read_evidence(state, ref))

    replay = state.model_copy(deep=True)
    replay.books, replay.items, replay.spatial_tracks, replay.shelf_geometry, replay.pricing_jobs = [], [], {}, {}, {}
    geometry_ref = state.room.geometry_ref
    try:
        # RoomPlan anchors must also be backed by immutable saved evidence.
        replay.geometry = read_json(geometry_ref) if geometry_ref in state.evidence else {}
        if not replay.geometry:
            replay.room.geometry_ref = ''
        overlays = {}
        expected = defaultdict(set)
        for ref in sorted(state.evidence):
            if not ref.startswith('observations/overlays_') or not ref.endswith('.json'):
                continue
            frame = ref[len('observations/overlays_'):-5]
            entries = read_json(ref)['detections']
            overlays[frame] = entries
            for index, entry in enumerate(entries):
                id = resolve(entry.get('id'))
                if entry.get('kind') == 'item' and id in items:
                    expected[id].add((frame, index))
    except (OSError, ValueError, KeyError, TypeError) as error:
        report['errors'].append(f'Immutable evidence could not be verified ({type(error).__name__})')
        return repaired, report

    runner = SweepRunner(replay, _ReadOnlyEvidence(store), Settings(paid_pricing=False), None, None)
    targets, matched, first_seen = defaultdict(set), defaultdict(set), {}
    measurement_frame, forbidden_targets = {}, set()
    try:
        frames = []
        for frame in state.frames:
            ref = f'observations/meta_{frame}.json'
            if ref not in state.evidence or f'observations/vision_{frame}.json' not in state.evidence:
                continue
            metadata = FrameMetadata.model_validate(read_json(ref))
            if metadata.id != frame:
                raise ValueError('Saved frame metadata ID mismatch')
            frames.append(metadata)
        for metadata in sorted(frames, key=lambda m: (m.timestamp_s, m.id)):
            scan = Scan.model_validate(read_json(f'observations/vision_{metadata.id}.json')['scan'])
            event = runner.apply_scan(metadata, scan)
            report['replayed_frames'] += 1
            after, before = event['detections'], overlays.get(metadata.id, [])
            aligned = len(before) == len(after) and all(
                a.get('kind') == b.get('kind') and a.get('polygon') == b.get('polygon')
                for a, b in zip(before, after))
            if not aligned:
                forbidden_targets.update(entry['id'] for entry in after if entry['kind'] == 'item')
                continue
            for index, (original, current) in enumerate(zip(before, after)):
                id = resolve(original.get('id'))
                if original.get('kind') != 'item' or id not in items:
                    continue
                targets[id].add(current['id'])
                matched[id].add((metadata.id, index))
                first_seen.setdefault(id, (metadata.timestamp_s, index))
                if current.get('world_corners'):
                    measurement_frame[current['id']] = metadata.id
    except (OSError, ValueError, KeyError, TypeError) as error:
        report['errors'].append(f'Observation replay could not be verified ({type(error).__name__})')
        return repaired, report

    incomplete, ambiguous = set(), set()
    for id, item in items.items():
        required_frames = {ref[7:-4] for ref in item.evidence_refs + [item.frame_ref]
                           if ref.startswith('frames/') and ref.endswith('.jpg')}
        required_frames.update(state.spatial_tracks.get(id, {}).get('frames', []))
        observed_frames = {frame for frame, _ in matched[id]}
        if not targets[id] or matched[id] != expected[id] or not required_frames <= observed_frames:
            incomplete.add(id)
        if len(targets[id]) > 1:
            ambiguous.add(id)
    for id in incomplete | ambiguous:
        forbidden_targets.update(targets[id])
    report['incomplete_ids'] = sorted(incomplete)
    report['ambiguous_ids'] = sorted(ambiguous)
    groups = defaultdict(list)
    for id in items:
        if id in incomplete | ambiguous:
            continue
        target = next(iter(targets[id]))
        if target in forbidden_targets:
            report['blocked_ids'].append(id)
            continue
        groups[target].append(id)

    replay_items = {item.id: item for item in replay.items}
    retained = {item.id: item for item in repaired.items}
    order = {item.id: index for index, item in enumerate(state.items)}
    retired = set()
    for target, group in groups.items():
        group.sort(key=lambda id: (first_seen.get(id, (float('inf'), 0)), order[id]))
        canonical = group[0]
        records = [items[id] for id in group]
        claimed = [record for record in records if record.user_claims]
        identity_fields = ('category', 'description', 'material', 'brand_model', 'is_print')
        if len({tuple(getattr(record, key) for key in identity_fields) for record in claimed}) > 1:
            report['blocked_ids'].extend(group)
            continue
        record = retained[canonical]
        previous_query = pricing_query(record)
        if len(group) > 1:
            if claimed:
                for key in identity_fields:
                    setattr(record, key, getattr(claimed[0], key))
            record.evidence_refs = list(dict.fromkeys(ref for member in records for ref in member.evidence_refs + [member.frame_ref]))
            record.review_reasons = list(dict.fromkeys(reason for member in records for reason in member.review_reasons))
            record.user_claims = list(dict.fromkeys(claim for member in records for claim in member.user_claims))
            record.excluded = any(member.excluded for member in records)
            if any(member.status == 'needs_appraisal' for member in records):
                record.status = 'needs_appraisal'
            record.revision += 1
            archive = repaired.metrics.setdefault('inventory_alias_records', {})
            for id in group[1:]:
                aliases[id] = canonical
                retired.add(id)
                archive.setdefault(id, items[id].model_dump(mode='json'))
                repaired.spatial_tracks.pop(id, None)
            for job in repaired.pricing_jobs.values():
                if job.get('id') in group and (job.get('id') != canonical or job.get('status') in {'queued','searching'}):
                    job.update(status='unavailable', reason='Inventory identity repaired; no automatic repeat valuation')
            if record.pricing_status in {'queued','searching'}:
                record.pricing_status = 'unavailable'
                record.pricing_reason = 'Inventory identity repaired; no automatic repeat valuation'
        # Replay tracks contain only hash-verified observation provenance.
        repaired.spatial_tracks[canonical] = copy.deepcopy(replay.spatial_tracks[target])
        fresh = replay_items[target]
        frame = measurement_frame.get(target)
        refs = ([f'observations/meta_{frame}.json', f'depth/{frame}.bin', f'depth/{frame}.confidence'] if frame else [])
        if refs and all(ref in state.evidence for ref in refs):
            record.world_corners = copy.deepcopy(fresh.world_corners)
            record.evidence_refs = list(dict.fromkeys(record.evidence_refs + refs))
        if replay.geometry and any(value is not None for value in fresh.dimensions_cm.model_dump().values()):
            record.dimensions_cm = fresh.dimensions_cm.model_copy(deep=True)
            record.evidence_refs = list(dict.fromkeys(record.evidence_refs + [geometry_ref]))
        if pricing_query(record) != previous_query:
            # Raw dimensions matter even inside the same pricing cache bucket.
            # Keep the prior offer for audit, but never carry it to a changed
            # query without source revalidation or initiate another search here.
            reason = 'Repaired identity or measurements changed; prior offer requires revalidation'
            if record.replacement_cost.verified or record.replacement_cost.low is not None:
                repaired.metrics.setdefault('inventory_repair_previous_prices', {})[canonical] = record.replacement_cost.model_dump(mode='json')
            record.replacement_cost = PriceRange(exclusion_reason=reason)
            if record.status != 'needs_appraisal':
                record.status = 'unpriced'
            record.pricing_status, record.pricing_reason = 'unavailable', reason
            if len(group) == 1:
                record.revision += 1
            for job in repaired.pricing_jobs.values():
                if job.get('id') == canonical:
                    job.update(status='unavailable', reason=reason)

    repaired.items = [record for record in repaired.items if record.id not in retired]
    existing = {record.id for record in repaired.items + repaired.books}
    repaired.metrics['inventory_aliases'] = {id: resolve(id) for id in aliases
                                           if id not in existing and resolve(id) in existing}
    report.update(after_items=len(repaired.items), merged_items=len(retired),
                  aliases={id: repaired.metrics['inventory_aliases'][id] for id in sorted(retired)})
    return repaired, report
