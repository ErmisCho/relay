# Router benchmark (TASK-45): gemma4:e4b vs Laya

Standalone; does not import or start the Delegator. Decides which model
classifies a dispatched commitment as `easy` (runs on local gemma4:e4b) or
`hard` (runs on gpt-6-luna, qwen3.8 fallback). No cloud calls.

## Run
Prerequisites: Ollama on `localhost:11434` with `gemma4:e4b` pulled; Apple
Silicon for Laya.

```sh
# everything: both routers, prompt variants v1+v2, 3 repetitions each
uv run python benchmarks/router/run_benchmark.py

# quick smoke test, no files written
uv run python benchmarks/router/run_benchmark.py --limit 4 --reps 1 --no-write

# one router only
uv run python benchmarks/router/run_benchmark.py --routers gemma
```

Laya is run by `run_benchmark.py` as a subprocess in an isolated, throwaway
uv environment, so the project's `pyproject.toml`/`uv.lock` are untouched. To
run it by hand:

```sh
uv run --no-project --with laya-mlx==0.2.0 \
  python benchmarks/router/laya_router.py --variant v1 --out /tmp/laya.json
```

- package: `laya-mlx` 0.2.0 (PyPI; pulls `mlx` 0.32.2, `tokenizers`, `huggingface-hub`, `numpy`)
- model: `aac6fef/laya-mlx` (Hugging Face; 421M ModernBERT-large, FP16), downloaded on first use to `~/.cache/huggingface/hub`
- expected warning on load: the checkpoint's `choice:11+` temperature is clamped to 0.1006 and confidence is uncalibrated. The benchmark never reads `confidence` or the `score` head; it uses the argmax label of one binary `choice` question.

If you prefer a persistent env: `uv venv benchmarks/router/.venv && uv pip install --python benchmarks/router/.venv laya-mlx==0.2.0` (ignored by `.gitignore`).

## Files
- `labels.jsonl` (60 tasks) and `LABELLING.md` (criteria)
- `common.py`: shared question wording (the same for both routers), variants `v1`/`v2`, fail-safe label
- `gemma_router.py`: Ollama OpenAI-compatible call, `temperature=0`, `reasoning_effort="none"`, JSON output, 30 s timeout; invalid/timeout/error -> `hard`, counted separately
- `laya_router.py`: binary `choice` only; one fresh process per repetition so load and first call are cold
- `run_benchmark.py`: runs the routers one after another, writes `results.json` (every call) and `RESULTS.md` (tables; the hand-written analysis block is kept across reruns)

## Measuring latency
Close other GPU work first. `results.json` stores `loadavg` and `ollama ps`
before every repetition so contention is visible after the fact.
