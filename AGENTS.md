# 声稿 / Voice Studio

- Read README.md and docs/ARCHITECTURE.md before changing behavior.
- Code lives here; models, reference recordings, project data, generated media,
  dependency caches and test outputs live outside the repository and all sync roots.
- Never modify or move the previously accepted ZipVoice assets. Reference them through
  machine-local config/local.json, which must remain untracked.
- API server uses Python 3.12+ standard library. Heavy inference runs in the separately
  configured Python environment. Frontend is native ES modules with no build step.
- Preserve whole-sentence synthesis, native chunk-map assignment and real silence
  between segments. A passing automated test is not listening acceptance.
- Run Python with -B or PYTHONDONTWRITEBYTECODE=1. Set VOICE_STUDIO_TEST_ROOT to an
  external validated task directory before tests. Do not generate caches here.
- Do not automatically upload voices, manuscripts or outputs. Cloud integrations,
  public hosting and publication are separate user actions.
- Keep the API contract, UI states and docs synchronized. Never show unimplemented
  rewriting, alignment, video or emotion controls as working features.
