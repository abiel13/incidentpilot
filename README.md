# IncidentPilot

IncidentPilot is a local incident investigation demo built with LangGraph. It investigates seeded logs, metrics, deployments, and runbooks, then suggests a configuration change for a person to review. The dashboard shows the investigation as it runs.

The graph can use Google Gemini to write the diagnosis. Deterministic demo mode works without an API key.

## Run locally

Requirements: Python 3.13, Node.js 20.20+, and npm 11. From PowerShell at the repository root:

```powershell
Copy-Item .env.example .env
py -3.13 -m venv backend/.venv
.\backend\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.txt
npm --prefix frontend ci
```

Start the API in one terminal:

```powershell
.\backend\.venv\Scripts\Activate.ps1
uvicorn app.main:app --app-dir backend --reload
```

Start the dashboard in another:

```powershell
npm --prefix frontend run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). The API docs are at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Use Gemini

Edit the root `.env`: set `LLM_PROVIDER=gemini` and add `GEMINI_API_KEY`. The default model is `gemini-3.5-flash-lite`. Keep the key on the backend; never put it in frontend variables.

Gemini supplies the diagnosis and citations. The app checks that cited evidence IDs exist, then keeps action selection and execution under graph control. Provider errors are shown in the run; the app does not silently switch to demo mode.

## What to try

- Start a seeded incident and watch the specialist investigations run in parallel.
- Open the evidence panel to see which logs, metrics, and configuration records support the diagnosis.
- Ask for a change, approve it, or reject it. Only approval writes a simulated configuration change.
- Restart the API while a run is waiting for review, then reopen that run and continue.
- Open the final report or start another run to see saved lessons.

## Check the project

```powershell
python -m pytest backend/tests -q
python -m ruff check backend/app backend/tests backend/scripts
python backend/scripts/evaluate_demo.py
npm --prefix frontend run build
```

## Scope

All incident data and service changes are simulated and stored in local SQLite. Verification checks the approved configuration values; it does not measure real traffic or recover a service. When Gemini is enabled, the incident description and seeded investigation evidence are sent to Google.
