---
paths:
  - "app/web/**/*.py"
  - "app/services/prompt_tap.py"
  - "tests/verify_memory_bound.py"
---

# Streaming responses

- Import `StreamingResponse` from `app.web.streaming`, never from `fastapi`/`starlette` directly: the wrapper keeps a stream's sync iterator off the shared threadpool (`verify_memory_bound` test_stream_isolation). Why: a raw TM export in `projects_glossary.py` used fastapi's class and turned a release sweep red.
- Text-processing endpoints must stay linear on pathological input (thousands of combining marks, huge single tokens): one `/api/translate/text` request once froze a server for 3.5 minutes. Add a fail-before test with such input for any new regex or normalization on request text.
