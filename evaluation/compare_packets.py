"""Offline evaluation of saved comparison packets against one frozen truth set."""
import argparse
import copy
import json
from pathlib import Path
from datetime import datetime

from claim_agent.evaluate import evaluate


def _timestamp(value, name):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('Timezone required')
        return parsed
    except (ValueError,AttributeError,TypeError) as error:
        raise ValueError(f'{name} needs an ISO timestamp with a timezone') from error


def _metric_deltas(results):
    lidar=results['lidar']['metrics']
    camera=results['camera']['metrics']
    identity_verified=all(metrics['title_identification']['unverified_high_confidence']==0 for metrics in [camera,lidar])
    def coverage(metrics):
        spine=metrics['spine_dimensions']
        return spine['covered']/spine['selected'] if spine['selected'] else None
    specifications=[
        ('book_count_relative_error','book_count','relative_error','lower'),
        ('title_recall','title_identification','recall','higher'),
        ('confident_wrong_fraction','title_identification','wrong_fraction_high_confidence','lower'),
        ('spine_mean_relative_error','spine_dimensions','mean_error','lower'),
        ('object_recall','non_book_items','recall','higher'),
        ('floor_area_relative_error','floor_area','relative_error','lower'),
        ('wall_area_relative_error','wall_area','relative_error','lower'),
    ]
    deltas={}
    for name,metric,key,direction in specifications:
        a,b=camera[metric][key],lidar[metric][key]
        if name=='confident_wrong_fraction' and not identity_verified:
            a=b=None
        deltas[name]={'value':100*(a-b) if a is not None and b is not None else None,
                      'unit':'percentage_points','favorable_direction':direction}
    a,b=coverage(camera),coverage(lidar)
    deltas['spine_coverage']={'value':100*(a-b) if a is not None and b is not None else None,
                              'unit':'percentage_points','favorable_direction':'higher'}
    deltas['confidently_wrong']={'value':camera['title_identification']['confidently_wrong']-lidar['title_identification']['confidently_wrong'] if identity_verified else None,
                                 'unit':'predictions','favorable_direction':'lower'}
    for name in ['confident_wrong_fraction','confidently_wrong']:
        deltas[name]['verified_coverage_complete']=identity_verified
    return deltas


def evaluate_pair(directory: Path, truth: dict):
    report=json.loads((directory/'comparison.json').read_text())
    if report.get('status')!='complete':
        raise ValueError('A completed comparison is required')
    if not truth.get('frozen_at'):
        raise ValueError('Freeze independent physical ground truth before evaluating')
    source_packet=json.loads((directory/'lidar_packet.json').read_text())
    frozen=_timestamp(truth['frozen_at'],'frozen_at')
    captured=_timestamp(source_packet.get('sweep',{}).get('captured_at'),'LiDAR captured_at')
    if frozen>captured:
        raise ValueError('Independent truth must be frozen before the original LiDAR capture; prediction mappings/checks may be added later')
    results={}
    for mode in ['lidar','camera']:
        mapped=copy.deepcopy(truth)
        for key in ['books','items','spines','prices','identity_checks']:
            for row in mapped.get(key,[]):
                ids=row.pop('predicted_ids',None)
                if not isinstance(ids,dict) or set(ids)!={'lidar','camera'}:
                    raise ValueError(f'Every {key} row needs explicit lidar and camera predicted_ids; use null for unmatched truth')
                row['predicted_id']=ids[mode]
                if key=='identity_checks':
                    checks=row.pop('correct_by_mode',None)
                    if 'correct' in row or not isinstance(checks,dict) or set(checks)!={'lidar','camera'} or any(value is not None and type(value) is not bool for value in checks.values()):
                        raise ValueError('Every identity_checks row needs independent correct_by_mode values for lidar and camera (boolean or null), with no shared correct field')
                    row['correct']=checks[mode]
        packet=json.loads((directory/f'{mode}_packet.json').read_text())
        results[mode]=evaluate(packet,mapped)
    return {'experiment':report['experiment'],'source_sweep_id':report['source_sweep_id'],
            'synthetic':report['synthetic'],'ground_truth_frozen_at':truth['frozen_at'],
            'evaluations':results,'deltas_camera_minus_lidar':_metric_deltas(results),
            'limitations':report['limitations'],
            'notes':['Both modes use the same independently measured truth rows; human physical-ID matches differ by mode.',
                     'Missing prediction matches remain in coverage and recall. Model confidence is not calibrated accuracy.',
                     'Evaluation does not make capture-to-packet and saved-frame replay timings comparable.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comparison-dir',type=Path,required=True)
    parser.add_argument('--truth',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=evaluate_pair(args.comparison_dir,json.loads(args.truth.read_text()))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':
    main()
