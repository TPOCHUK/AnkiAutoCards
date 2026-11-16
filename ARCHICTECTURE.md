# Architecture

## Purpose

Anki AutoCards is a Windows desktop application that turns English words and phrases into
ready-to-review Anki cards. It checks for duplicates, asks an LLM for a Ukrainian translation
and a contextual example, lets the user review the result, optionally generates audio, and
adds approved cards to Anki through AnkiConnect.

## Data flow

```yaml
Tkinter word list
      ↓
Input normalization + duplicate check in Anki
      ↓
LLM generation → JSON parsing and card validation
      ↓
Editable preview in the desktop UI
      ↓
Optional Edge TTS audio generation
      ↓
AnkiConnect: media upload + note creation
```

One word or phrase is processed at a time. Preview generation does not write anything to
Anki. A card is added only after the user approves the preview and starts the add operation.
The same workflow validates both newly generated and manually edited cards.

## Main components

| **Component** | **Responsibility** |
| --- | --- |
| `launcher.pyw` | Starts the desktop app and records startup failures |
| `anki_autocards.py` | Compatibility entry point for launching the application |
| `app.py` | Tkinter UI, background jobs, progress, logs, and token history |
| `workflow.py` | LangGraph card workflow, retries, validation, audio, and insertion |
| `anki_core.py` | AnkiConnect client, prompts, parsing, templates, and Edge TTS adapter |
| `settings.py` | Settings validation, JSON persistence, `.env` import, and key storage |
| `tests/` | Workflow, settings, token display, and keyboard shortcut tests |

## Card lifecycle

Each card moves through a small state machine implemented in `workflow.py`.

| **State** | **Meaning** |
| --- | --- |
| `working` | The card is being checked or generated |
| `invalid` | The model response failed validation and may be retried |
| `ready` | The preview is valid and can be added to Anki |
| `added` | The note and optional audio were written to Anki |
| `duplicate` | The word or normalized lemma already exists in the deck |
| `failed` | A provider, validation, audio, or Anki operation failed |
| `cancelled` | Processing stopped after a user cancellation request |

The workflow checks duplicates before generation and again after the model returns a lemma.
An invalid model response is retried once. Audio and note creation run only in add mode, so
creating a preview has no side effects in Anki.

## Concurrency and lifecycle

Tkinter owns the main thread. LLM requests, AnkiConnect calls, and audio generation run in a
daemon worker thread so the window remains responsive. Workers publish immutable event data
to a thread-safe queue; `App.drain()` applies those updates to UI widgets on the Tkinter
thread.

A `threading.Event` carries cancellation requests. The current network call is allowed to
finish, but the workflow checks the event before starting later steps. Words are processed
sequentially, and a local rate limiter pauses large batches without blocking the UI thread.

## Configuration and extension points

`Settings` validates the provider, model, API base URL, AnkiConnect URL, token limit, deck,
note type, voice, and translation language. Public settings are stored as JSON in the user's
application-data directory. API keys are stored separately in Windows Credential Manager.

`build_graph()` connects the card-processing steps and accepts injected LLM, TTS, Anki, and
notification adapters for testing. The UI depends on the compiled workflow rather than on a
specific model provider, which allows OpenAI, OpenRouter, Anthropic, and OpenAI-compatible
services to share the same card pipeline.

For changes:

1. Test parsing and Anki integration helpers in `tests/test_workflow.py`.
2. Test card states and provider failures through the compiled workflow.
3. Keep all blocking network and audio operations outside the Tkinter thread.
4. Run `pytest` and Ruff before building the Windows executable.

The current design supports one desktop session, one sequential word queue, and one local
Anki instance. Parallel generation or shared multi-user state would require explicit request
scheduling and persistent coordination outside the UI process.
