# SafeKeep — Post-Occupancy ESM Compliance Tracker

A functional model for tracking ongoing **Essential Safety Measure (ESM)** compliance
of existing buildings, localised to Australian requirements (NSW Annual Fire Safety
Statement model + AS 1851 servicing intervals + NCC building classes).

The gap it addresses: most "AI for construction compliance" tools check buildings at
**design time**. Almost nothing tracks whether a building *stays* compliant across its
operational life — which is a recurring legal obligation in Australia. This app models
that ongoing post-occupancy tracking.

## Run it

```bash
pip install flask transformers torch
python app.py
# open http://127.0.0.1:5000
```

No API key. On first use of the AI feature the ML model weights (~250MB) download
once, then run fully offline. If the download can't happen (no internet), the app
automatically falls back to a deterministic classifier so it never crashes.

No build step, no config. SQLite database (`safekeep.db`) and demo data are created
automatically on first run. Delete `safekeep.db` to reset.

## AI component — inspection report understanding

The AI feature (`ai_extract.py`) turns a **free-text inspection report** into a
**structured compliance record**: result (Pass / Defect noted / Fail), severity
(minor / moderate / critical), and a summary.

- **Engine:** a HuggingFace *zero-shot classification* model
  (`typeform/distilbert-base-uncased-mnli`). Zero-shot means it classifies into the
  compliance labels without being trained on inspection data — a genuine ML/NLP model
  running locally, no API key.
- **Where it's used:** on the *Log inspection* screen, paste a report → "Analyse
  report" → the result and severity are extracted and pre-fill the form for review.
- **Why ML over keywords:** the model handles negation and paraphrase that naive
  keyword matching gets wrong (e.g. *"no defects found"* is a Pass, not a defect).
  The deterministic fallback includes negation handling too, but the ML model
  generalises to wording the rules don't anticipate.
- **Robustness:** if the model can't load, the app falls back to a rule-based
  classifier and labels which engine produced each result, so it always runs.

This is "unstructured text → structured data," a defensible and well-scoped use of AI
rather than AI bolted on for its own sake.


## Features

**Buildings (full CRUD)**
- Create / view / edit / delete buildings with NCC classification and owner contact
- New buildings auto-populate with the standard ESM catalogue

**Essential Safety Measures (full CRUD)**
- Add / delete measures per building, each with a servicing standard and inspection interval
- Autocomplete from a catalogue of common AU measures (fire alarms, hydrants, exit signs, etc.)

**Inspections**
- Log an inspection (date, inspector, Pass/Defect/Fail, notes) against any measure
- Logging an inspection automatically recalculates compliance and updates the due date
- Full per-measure inspection history

**Compliance engine (the core logic)**
- Each measure's status is computed from `last_inspected + interval`:
  `compliant` / `due_soon` (≤30 days) / `overdue` / `never inspected`
- Per-building health score and overall status (Compliant / Action needed / Non-compliant)
- Portfolio dashboard aggregating all buildings

**Annual statement generator**
- Produces a draft AFSS-style statement listing every measure and its status
- **Blocks certification** if any measure is overdue or never inspected — mirroring the
  real-world rule that you can't lodge a fire safety statement with outstanding items

## Architecture

Single-file Flask app, server-rendered HTML (templates embedded as strings).

| Layer | What it does |
|-------|--------------|
| `ESM_CATALOGUE` | Domain knowledge: measures + required intervals + standards |
| `compute_status()` | Core compliance reasoning per measure |
| `building_summary()` | Aggregates status + computes health score |
| SQLite (3 tables) | `building` → `measure` → `inspection` (FK cascade) |
| Routes | Dashboard, building/measure CRUD, inspections, statement |

### Data model

```
building (1) ──< measure (1) ──< inspection
```

- `building`: name, address, NCC classification, owner contact
- `measure`: name, standard, interval_months, last_inspected
- `inspection`: date, inspector, result, notes

## Notes for marking / extension ideas

This is a deliberately scoped functional model. Natural extensions:
- Photo/document upload against each inspection (evidence trail)
- Email reminders before due dates
- Multi-tenant accounts for facility managers vs accredited inspectors
- State-by-state schedule variations (NSW AFSS vs VIC ESM report)
- An LLM layer to read uploaded inspection reports and auto-fill results
