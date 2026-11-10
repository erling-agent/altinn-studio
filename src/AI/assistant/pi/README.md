# Studio Assistant on pi: spike

This folder runs Studio Assistant's work with [pi](https://pi.dev) as the agent harness, and compares it with the
assistant service. It is a spike for a decision, not code for production.

- [`extension/index.ts`](extension/index.ts) is what the assistant needs that a coding agent does not have: the Altinn
  tools, a guard that keeps file tools in the app and off the generated model files, and a check that does not let a
  turn end while validation fails.
- [`studio_tool.py`](studio_tool.py) runs the assistant's own Altinn tools for the extension, so the spike reuses the
  validators instead of porting them. `studioctl app vet` and the commands in #21800 can replace it.
- [`APPEND_SYSTEM.md`](APPEND_SYSTEM.md) and [`APPEND_CHAT.md`](APPEND_CHAT.md) are the prompt that pi gets beside
  its own. The skills in [`../agents/skills`](../agents/skills) carry the knowledge about Altinn apps.
- [`agent/models.json`](agent/models.json) adds the assistant's Azure deployment to pi.
- [`runner/Dockerfile`](runner/Dockerfile) builds an image that runs one turn in a container.
- [`eval/`](eval) runs the tasks in [`eval/tasks.json`](eval/tasks.json) on either harness, and scores the changed
  app with the e2e benchmark's rubric and a check for each task.

## Run the eval

The eval needs a key for the Azure OpenAI resource in `AZURE_OPENAI_API_KEY`, and a Designer API key for dev in
`STUDIO_DEV_API_KEY` to generate data models. It needs no Designer stack, no Gitea and no Langfuse.

```bash
uv sync
.venv/bin/python pi/eval/run.py --harness both --out pi/runs/wide --reps 3 --headroom 700000
.venv/bin/python pi/eval/summary.py pi/runs/wide
```

`--harness both` runs one turn at a time and alternates the harnesses. `--headroom` waits until the deployment has
that many tokens a minute left, because it is the deployment that production uses.
