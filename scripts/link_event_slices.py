"""Register frozen Event spans and link already stored slices without embeddings."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember.event_slices import EventSliceLinks, register_snapshot, sync_from_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--event-snapshot', type=Path)
    parser.add_argument('--instance', default='default')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.event_snapshot:
        results = register_snapshot(args.data_dir, json.loads(args.event_snapshot.read_text(encoding='utf-8-sig')), args.instance)
    else:
        results = sync_from_files(args.data_dir, args.instance)
    links = EventSliceLinks(args.data_dir).all(args.instance)
    report = {'registered_events': len(results), 'ready_events': sum(r['state'] == 'ready' for r in results),
        'unresolved_events': [r for r in results if r['state'] != 'ready'],
        'linked_events': len({r['event_id'] for r in links}), 'linked_slices': len({r['slice_id'] for r in links}),
        'memberships': len(links), 'links': links, 'vectors_recomputed': 0}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'links'}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
