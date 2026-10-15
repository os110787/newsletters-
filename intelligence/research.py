"""Gemini Deep Research orchestration. No research is simulated on API failure."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_AGENT = 'deep-research-max-preview-04-2026'
STANDARD_AGENT = 'deep-research-preview-04-2026'
TOPICS = [
    'AI Visibility / Generative Engine Optimization (GEO), KI-Zitierungen und Markenempfehlungen',
    'Google Search SEO, AI Overviews, AI Mode, Search Console, Bing Webmaster',
    'ChatGPT Search, Gemini, Perplexity, Claude und andere KI-Antwortsysteme',
    'Structured data / Schema.org, Crawling, Robots, Content, E-E-A-T, Digital PR',
    'Tools, APIs, Open-Source-Releases, Preisänderungen, neue Anbieter',
    'LLM-Experimente, wissenschaftliche Studien, belastbare Benchmarks',
    'Local Search, E-Commerce, Multimodal Search, Social Search und Attribution',
]


def research_prompt(as_of: str, previous: str = '', source_hints: list[dict] | None = None) -> str:
    date = datetime.fromisoformat(as_of).date()
    start = (date - timedelta(days=7)).isoformat()
    memory = previous[-10000:] if previous else 'Keine früheren Ausgaben vorhanden.'
    hints = '\n'.join(f'- {x["title"]}: {x["url"]}' for x in (source_hints or [])[:12])
    return f'''Du bist Senior Research Director bei VISIWING, einer deutschen AI-Visibility-Plattform.
DEEP RESEARCH: Recherchiere selbstständig im OFFENEN WEB, nicht nur aus diesen Links.
Stichtag {as_of}, Nachrichtenfenster {start} bis {as_of} inklusive. Zeiträume strikt einhalten.
Finde überraschende, relevante und übersehene Entwicklungen. Verfolge Primärquellen,
prüfe widersprüchliche Berichte und suche zusätzlich nach Werkzeugen, Experimenten und Studien.
Vermeide SEO-GEO-Mythen, Schein-Kausalität und unbelegte Ranking-Versprechen.
Zielgruppe: DACH-KMU, SEO/GEO-Agenturen, Digital-Marketing- und Growth-Verantwortliche.

Recherchiere ausgewogen:
{chr(10).join('- '+x for x in TOPICS)}

Pflichten:
- Unterscheide VERÖFFENTLICHUNGSDATUM und tatsächliches EREIGNISDATUM je Meldung.
- Priorisiere Entwicklungen aus dem Zeitfenster, hinterlege ältere als Kontext.
- Jede Hauptaussage braucht volle, direkte Quellen-URL, vorzugsweise Primärquelle und Datum.
- Setze keine Quelle gleich mit unabhängiger Validierung; Unsicherheiten explizit.
- Berichte über aktuelle neue Tools MIT Vergleich zu existierenden Alternativen,
  Verfügbarkeit, ggf. Preis sowie möglichem Praxisnutzen; keine Preise raten.
- Für Tipps: begründete Hypothese, Zielgruppe, messbarer Test, Aufwand und Risiken.
- Schließe explizit aus, was nur Duplikat oder Wiederholung der Vorwoche ist.
- Gib 10-20 mögliche Geschichten als recherchierten KANDIDATENKATALOG aus,
  markiere die 5 bis 8 redaktionell stärksten für den Newsletter.
- Spezifische Schwerpunkte für AI-Visibility-Messung auf Ebene einzelner
  Leistungen/Services, Wettbewerbsvergleich, Interventions- und Outcome-Tracking.
- Am Ende Quellenverzeichnis mit vollständigen HTTP(S)-URLs und Originaltiteln.
- Keine Formulierungen wie 'Quelle: Recherche' ohne konkrete URL.

REDAKTIONELLES GEDÄCHTNIS (nur zum Abgleich, nicht ungeprüft als Wahrheit):
{memory}

OPTIONALE FRÜHINDIKATOREN (keine exklusive Quellenbasis):
{hints or 'Keine'}

Sprache Deutsch. Schreibe einen präzisen, fundierten Markdown-Research-Report
mit Executive Summary, Top Stories, weiteren News, Tools, Quick Wins,
Evidenzgraden, Methodenhinweisen, Quellen und offenen Fragen.
'''


def extract_report(interaction) -> str:
    text = getattr(interaction, 'output_text', None)
    if isinstance(text, str) and text.strip():
        return text
    steps = getattr(interaction, 'steps', None) or []
    fragments = []
    for step in steps:
        for content in getattr(step, 'content', None) or []:
            t = getattr(content, 'text', None)
            if isinstance(t, str) and t.strip():
                fragments.append(t)
    if not fragments:
        raise RuntimeError('Completed research interaction contains no textual report')
    return fragments[-1]


def start_or_resume(prompt: str, state_path: Path, *, agent: str = DEFAULT_AGENT,
                    poll_seconds: int = 20, timeout_seconds: int = 3600, client=None) -> tuple[str, dict]:
    """Persist the interaction ID BEFORE polling; resume rather than charging twice on retries."""
    if client is None:
        if not os.environ.get('GEMINI_API_KEY'):
            raise RuntimeError('GEMINI_API_KEY fehlt – es wurde keine kostenpflichtige Recherche gestartet.')
        from google import genai
        client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    import hashlib
    fingerprint = hashlib.sha256((agent + prompt).encode()).hexdigest()
    current = json.loads(state_path.read_text()) if state_path.exists() else None
    if current and current.get('fingerprint') == fingerprint and current.get('status') == 'completed':
        cached = state_path.parent / current['report_file']
        if cached.exists():
            return cached.read_text(encoding='utf-8'), current
    if current and current.get('fingerprint') == fingerprint and current.get('status') in ('pending','running'):
        interaction_id = current['id']
    else:
        response = client.interactions.create(input=prompt, agent=agent, background=True)
        interaction_id = response.id
        current = {'id': interaction_id, 'status':'pending', 'agent':agent,
                   'fingerprint':fingerprint, 'started_at': datetime.now(timezone.utc).isoformat()}
        state_path.write_text(json.dumps(current,indent=2),encoding='utf-8')
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        interaction = client.interactions.get(id=interaction_id)
        status = str(getattr(interaction,'status','')).lower()
        current['status'] = status
        state_path.write_text(json.dumps(current, indent=2),encoding='utf-8')
        if status == 'completed':
            report = extract_report(interaction)
            report_name = 'deep-research-report.md'
            (state_path.parent / report_name).write_text(report,encoding='utf-8')
            current['report_file'] = report_name
            current['finished_at'] = datetime.now(timezone.utc).isoformat()
            # Save citation metadata for auditing if the SDK exposes it.
            if hasattr(interaction, 'model_dump'):
                detail = interaction.model_dump(mode='json', exclude_none=True)
                (state_path.parent / 'raw-interaction.json').write_text(
                    json.dumps(detail, ensure_ascii=False, indent=2),encoding='utf-8')
            state_path.write_text(json.dumps(current,indent=2),encoding='utf-8')
            return report, current
        if status in ('failed','cancelled','canceled','expired'):
            raise RuntimeError(f'Gemini Deep Research ended as {status}: {getattr(interaction,"error",None)}')
        time.sleep(poll_seconds)
    raise TimeoutError(f'Research still running: {interaction_id}. Rerun to resume without another request.')


def http_urls(text: str) -> set[str]:
    return {u.rstrip('.,);]}>') for u in re.findall(r'https?://[^\s<>"\']+',text)}
