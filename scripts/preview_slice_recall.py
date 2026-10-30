"""Fresh input embedding against local raw slices; save the full ranked report."""
import argparse
import html
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ember.slice_recall import SliceRecall


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', required=True)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--instance', default='default')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/slice-recall-preview.html')
    args = parser.parse_args()
    recall = SliceRecall(args.data_dir, args.instance, args.config)
    metadata = recall.match(args.query)
    result = recall.trace(metadata['trace_id'], include_sources=True)
    result['query'] = args.query
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    escape = html.escape
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>原文匹配排名</title>',
        '<style>body{font:16px/1.7 system-ui;max-width:1000px;margin:40px auto;padding:0 20px;background:#f7f7f5;color:#222}details{background:white;border:1px solid #ddd;border-radius:8px;padding:14px;margin:12px 0}summary{cursor:pointer}pre{white-space:pre-wrap;font:inherit}small{color:#666}</style>',
        '<h1>原文切片匹配排名</h1>', '<p>' + escape(args.query) + '</p>',
        '<p>模型：' + escape(result.get('model', '')) + ' · 匹配 ' + str(result['ranked_count']) + ' 组</p>',
        '<p>本页保留完整排名，无门槛、无候选截断，也未向聊天注入内容。点击展开原文。</p>']
    for item in result['rankings']:
        messages = item.get('messages', [])
        preview = next((m['content'] for m in messages if m['role'] == 'user'),
                       messages[0]['content'] if messages else item.get('source_status', ''))
        parts.append('<details><summary><b>#' + str(item['rank']) + ' · ' + f"{item['score']:.6f}" + '</b>　'
                     + escape(preview[:100]) + '</summary><small>会话 ' + escape(item['session_id'])
                     + ' · 消息 ' + escape(str(item['user_message_id'])) + ' / '
                     + escape(item['assistant_message_id']) + '</small>')
        for message in messages:
            parts.append('<p><b>' + escape(message['role']) + '</b></p><pre>' + escape(message['content']) + '</pre>')
        parts.append('</details>')
    parts.append('</html>')
    args.output.write_text('\n'.join(parts), encoding='utf-8')
    print(json.dumps({**metadata, 'report': str(args.output),
                      'top_scores': [round(r['score'], 6) for r in result['rankings'][:5]]}, ensure_ascii=True))
    return 0 if metadata['status'] in ('ranked', 'no_vectors') else 1


if __name__ == '__main__':
    raise SystemExit(main())
