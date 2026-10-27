# Five-minute interview walkthrough

1. **Frame the problem (30 seconds).** Show the three seeded incidents. Explain that the application investigates simulated telemetry and requires human approval before any simulated change.
2. **Start an investigation (75 seconds).** Choose the API deployment scenario. Watch parallel specialists and tool events stream in. Point out the intentionally injected log timeout and retry.
3. **Inspect the diagnosis (60 seconds).** In Gemini mode, show the model, response ID, token usage, diagnosis and cited evidence IDs. Explain that the graph uses the model's output, validates its references, and keeps tools and actions under application control. Model confidence is uncalibrated.
4. **Review and approve (75 seconds).** Request a change, inspect the revised proposal and second interrupt, then approve. Show the simulated action, the configuration check and Markdown report. The check does not measure actual traffic or service recovery.
5. **Explain persistence (60 seconds).** Open graph history and saved lessons. For a restart demonstration, start another run, leave it at review, restart the API, reopen the run and submit a decision. The SQLite file contains the application records and checkpoints.

For the alternative path, reject a proposal and show that the report records no action. A custom incident without a supported simulated change ends inconclusive.
