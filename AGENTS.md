# Repository Guidelines

## Project Structure & Module Organization

`backend/src_python/` contains the FastAPI service, configuration, database layer, and the real-time news engine; provider contracts/adapters live in `backend/src_python/providers/`, and engine tasks live in `backend/src_python/engine/`. Backend tests are in `backend/tests/`, while one-off utilities belong in `backend/scripts/`. `frontend/` is a Next.js 14 application organized into `app/`, `components/`, `hooks/`, and `lib/`. `dashboard/` is a separate Streamlit replay tool. Deployment examples live in `deploy/`, and design notes live in `docs/`.

## Build, Test, and Development Commands

- `pip install -r backend/requirements.txt -r backend/requirements-dev.txt` installs backend and test dependencies.
- From `backend/src_python/`, run `python engine.py` for the engine and `uvicorn api_server:app --host 127.0.0.1 --port 8000` for the API.
- `cd backend && python -m pytest tests/ -q` runs the offline test suite.
- `cd frontend && pnpm install --frozen-lockfile && pnpm run dev` starts Next.js on port 3030.
- `cd frontend && pnpm run build` performs the production and TypeScript build checks.
- `cd dashboard && pip install -r requirements.txt && streamlit run app.py --server.port 8501` starts the replay dashboard.

## Coding Style & Naming Conventions

Use four-space indentation and `snake_case` for Python functions/modules; use `PascalCase` for React components and `camelCase` for TypeScript helpers. Keep TypeScript strict and do not enable build-error suppression. Preserve existing local formatting because no repository-wide formatter or linter is configured. Put new environment settings in `backend/src_python/config.py` and document them in `.env.example`.

## Testing Guidelines

Use pytest and name files `test_<feature>.py`. Tests must remain offline: mock network, exchange, and LLM calls, and use temporary or in-memory databases. Never modify `backend/trident_event_bus.db`. Add regression tests for engine, filtering, schema, or API behavior; run `python -m py_compile <changed.py>` for touched Python files.

## Commit & Pull Request Guidelines

History uses short Conventional Commit-style prefixes such as `feat:` and `chore:`; continue with focused messages like `fix: make migration idempotent`. Keep refactors separate from changes to prompts, keyword lists, and trading thresholds. Pull requests should explain behavior and risk, list verification commands, link issues when available, and include screenshots for UI changes.

## Security & Data Rules

Never commit API keys or populated `.env` files; Jin10 access must use an authorized `JIN10_API_KEY` supplied through the environment. Make schema changes only in `backend/src_python/db.py`; migrations must be idempotent and must not swallow errors.
