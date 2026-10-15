import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from intelligence.core import Store, parse_feed, score_item
from intelligence.research import http_urls, research_prompt, start_or_resume
from intelligence.editorial import validate_item,render_html,save_issue


def test_research_includes_window_and_history():
    p=research_prompt('2026-10-14','Previous: sample story')
    assert '2026-10-07' in p and '2026-10-14' in p
    assert 'Previous: sample story' in p
    assert 'Deep Research' in p or 'DEEP RESEARCH' in p


def test_rss_atom_ingestion():
    atom=b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Google test</title><link rel="alternate" href="https://example.org/x?utm_source=foo"/><updated>2026-10-14T08:00:00Z</updated><summary>SEO change</summary></entry></feed>'''
    rows=parse_feed(atom,{'id':'example','name':'example','trust':99})
    assert len(rows)==1 and rows[0]['url']=='https://example.org/x'
    assert rows[0]['published_at'].startswith('2026-10-14')


def test_store_deduplicates(tmp_path):
    store=Store(tmp_path/'data.sqlite')
    item={'url':'https://example.org/x?utm_campaign=news','title':'Google SEO update',
          'description':'AI', 'source':'test','source_id':'test','source_trust':100,
          'source_category':'SEO & Google','published_at':datetime.now(timezone.utc).isoformat()}
    assert store.upsert(item) is True
    assert store.upsert(dict(item,url='https://example.org/x')) is False
    assert len(store.list())==1


def test_editorial_rejects_uncited_and_html_escapes():
    raw={'title':'<script>alert(1)</script>','summary':'text','sources':['https://not-in-report.test']}
    assert validate_item(raw,{'https://legit.test'}) is None
    raw['sources']=['https://legit.test']
    issue={'subject':'sample','as_of':'2026-10-14','stories':[validate_item(raw,{'https://legit.test'})]}
    rendered=render_html(issue)
    assert '&lt;script&gt;' in rendered and '<script>alert' not in rendered


def test_gemini_resume_without_second_charge(tmp_path):
    class FakeInteractions:
        def __init__(self): self.created=0
        def create(self,**kwargs):
            self.created+=1
            assert kwargs['background'] is True
            return SimpleNamespace(id='interaction-123')
        def get(self,**kwargs):return SimpleNamespace(status='completed',output_text='Report with https://example.org/source')
    interactions=FakeInteractions()
    fake=SimpleNamespace(interactions=interactions)
    report, state = start_or_resume('prompt',tmp_path/'state.json',client=fake,poll_seconds=0)
    report2, state2 = start_or_resume('prompt',tmp_path/'state.json',client=fake,poll_seconds=0)
    assert interactions.created==1 and report==report2
    assert 'example.org' in report


def test_demo_structure():
    p=Path(__file__).parent.parent/'examples'/'sample-issue-2026-10-14.json'
    data=json.loads(p.read_text())
    assert len(data['stories'])>=3
    assert all(item['sources'] and item['review_status']=='pending' for group in ('stories','tools','quick_wins') for item in data[group])
