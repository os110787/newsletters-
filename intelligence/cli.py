"""Command-line entry point for VISIWING Intelligence Engine."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import Store, ingest, load_sources
from .editorial import draft_newsletter, save_issue
from .research import DEFAULT_AGENT, STANDARD_AGENT, research_prompt, start_or_resume

BASE = Path(__file__).resolve().parent.parent


def previous_issue(out: Path, as_of: str) -> str:
    files = sorted(out.glob('*/newsletter.md'),reverse=True)
    for f in files:
        if f.parent.name < as_of:
            return f.read_text(encoding='utf-8')[-12000:]
    return ''


def candidates_from_feeds(base:Path)->list[dict]:
    p = base / 'data' / 'feed-cache.json'
    if not p.exists():return []
    return json.loads(p.read_text(encoding='utf-8'))[:12]


def weekly(args)->None:
    out = Path(args.out).resolve()
    date = args.as_of or datetime.now(ZoneInfo('Europe/Berlin')).date().isoformat()
    issue_dir = out / date
    prompt = research_prompt(date, previous_issue(out,date), candidates_from_feeds(BASE))
    issue_dir.mkdir(parents=True,exist_ok=True)
    (issue_dir/'research-prompt.md').write_text(prompt,encoding='utf-8')
    if args.dry_run:
        print(f'[DRY RUN] {len(prompt):,} characters of research prompt saved to {issue_dir}. No Gemini call.')
        return
    agent = STANDARD_AGENT if args.agent=='standard' else DEFAULT_AGENT
    report, state = start_or_resume(prompt,issue_dir/'interaction-state.json',agent=agent,
                                    poll_seconds=args.poll,timeout_seconds=args.timeout)
    print(f'Research complete ({state["id"]}); {len(report):,} characters.')
    issue = draft_newsletter(report,date)
    save_issue(issue,issue_dir)
    print(f'Newsletter DRAFT: {issue_dir / "newsletter.html"} ({len(issue["stories"])} stories). Review required.')


def demo(args):
    path = BASE / 'examples' / 'sample-issue-2026-10-14.json'
    issue = json.loads(path.read_text(encoding='utf-8'))
    directory=Path(args.out).resolve() / 'demo-2026-10-14'
    save_issue(issue,directory)
    print(f'Curated source-checked demonstration (NOT a Gemini API run): {directory / "newsletter.html"}')


def feeds(args):
    store=Store(BASE / 'data' / 'feeds.sqlite')
    result=ingest(load_sources(BASE/'config'/'sources.json'),store)
    articles=store.list(days=14,limit=100)
    (BASE/'data').mkdir(exist_ok=True)
    (BASE/'data'/'feed-cache.json').write_text(json.dumps(articles,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result['sources_ok']==0:
        raise RuntimeError('No sources reachable; ingestion failed rather than pretending to succeed.')


def main(argv=None):
    p=argparse.ArgumentParser(prog='visiwing-intel',description='Gemini Deep Research-first GEO/SEO newsletter engine')
    sub=p.add_subparsers(dest='cmd',required=True)
    w=sub.add_parser('weekly',help='Run one Deep Research job, produce a newsletter draft')
    w.add_argument('--as-of',help='YYYY-MM-DD (defaults to Berlin local date)')
    w.add_argument('--agent',choices=['max','standard'],default='max')
    w.add_argument('--out',default=str(BASE/'out'))
    w.add_argument('--dry-run',action='store_true')
    w.add_argument('--poll',type=int,default=20)
    w.add_argument('--timeout',type=int,default=3600)
    w.set_defaults(func=weekly)
    d=sub.add_parser('demo',help='Generate a newsletter preview from manually checked evidence (no API)')
    d.add_argument('--out',default=str(BASE/'out'))
    d.set_defaults(func=demo)
    f=sub.add_parser('ingest',help='Optional RSS discovery for extra leads; not the core research')
    f.set_defaults(func=feeds)
    args=p.parse_args(argv)
    try:args.func(args)
    except (RuntimeError,TimeoutError,ValueError,FileNotFoundError) as e:
        print(f'ERROR: {e}',file=sys.stderr)
        sys.exit(1)

if __name__=='__main__':main()
