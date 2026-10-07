# Run the attack demo

Use Python 3.12+ and run commands from the repository root. The demo uses Python's standard library; no runtime packages are required.

## Replay the recording

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m router_attack_demo replay --details
```

This replays the included [real API recording](../examples/api/trace.json) without a key, network request, or model download.

Each attack also has its own entry point:

```bash
python -m attacks.request_injection replay --details
python -m attacks.response_tampering replay --details
python -m attacks.model_selection replay --details
```

## Run with an API key

Set `OPENROUTER_API_KEY` in your shell, then run the configured Claude Opus 4.6 / Llama 3.2 3B model pool:

```bash
python -m router_attack_demo run --backend api --provider openrouter --out results/api.json
```

For direct OpenAI access, set `OPENAI_API_KEY` and select its provider profile:

```bash
python -m router_attack_demo run --backend api --provider openai --out results/openai.json
```

To run only one attack, use its module with the same options:

```bash
python -m attacks.model_selection run --backend api --provider openrouter --out results/selection.json
```

The default run makes at most six model calls, each capped at 384 output tokens. Response tampering reuses the normal generation. API usage is billed by the provider; fresh outputs may differ. Add `--dry-run` to inspect the plan without credentials or network calls. `--max-calls` and `--max-tokens` change the limits.

## Configure models and selection

Edit [routing.json](../configs/routing.json), or pass `--routing-config path/to/routing.json`.

| Field | What to configure |
|---|---|
| `providers.<name>.models` | Any number of model entries |
| Model `id`, `base_url`, `key_env` | Exact API model ID, endpoint, and credential environment variable |
| Model `capabilities`, `preference` | Routing capability tags and priority; lower preference wins |
| `policy.rules`, `policy.default` | Question categories and their required capabilities |
| `selectors.semantic`, `selectors.predictor` | Embedding data and selector settings |

The selector evaluates the original question and chooses a category. Required capabilities determine the eligible model set; preference selects within that set. With the bundled OpenRouter policy, study advice permits the compact model, while access rules and stable ranking select Opus. The model-selection attack overrides that choice with an ineligible model and claims the selected identity.

Choose a method with `--selector`:

| Method | Selection method |
|---|---|
| `rules` | Match phrases in the question; unmatched questions use the default category |
| `semantic` | Cosine nearest-neighbor search over example-question embeddings, followed by voting |
| `predictor` | A linear classifier trained on example-question embeddings and category labels |

```bash
python -m router_attack_demo run --backend api --selector semantic --out results/semantic.json
python -m router_attack_demo run --backend api --selector predictor --out results/predictor.json
```

The embedding methods use the included [routing examples](../configs/routing_examples.json) and [real embedding cache](../configs/routing_features.json), so these commands make no extra embedding calls. They follow the query-embedding approach used in [LLMRouter](https://github.com/ulab-uiuc/LLMRouter).

For a different corpus or embedding model, edit the selector's `examples` and `encoder` settings, set its configured key, and prepare a new cache:

```bash
python scripts/prepare_routing.py --out configs/my_routing_features.json
```

Then point the selectors' `cache` fields to that file. Preparation makes one embedding request and no chat-model calls. `--questions path/to/questions.json` adds a JSON array of question strings; `--dry-run` previews the preparation plan.

## Run local models

Start your own local OpenAI-compatible model servers. Edit `providers.local.models` with each server's model ID, HTTP loopback `base_url`, capabilities, and preference, then run:

```bash
python -m router_attack_demo run --backend local --out results/local.json
```

For the bundled Qwen models, the optional setup scripts start the two servers:

```bash
python scripts/prepare_models.py --runtime-dir .runtime
python scripts/with_local_models.py --runtime-dir .runtime -- \
  python -m router_attack_demo run --backend local --out results/local.json
```

Their download configuration is [local_models.lock.json](../configs/local_models.lock.json). Use your own server launcher when configuring different models.

## View the results

Each run saves one JSON file containing the question, routing choice, actual model input and output, and delivered response. Existing run files are not overwritten; errors are retained without retries.

```bash
python -m router_attack_demo replay --input results/api.json --details
python -m router_attack_demo report --input results/api.json --out results/REPORT.md
```

`python -m router_attack_demo list` lists the attacks and tasks. `--case access-policy` restricts a run to that task. To show model substitution with a truthful model label, add `--model-claim actual`.

## Regenerate the images

This optional step requires Pillow:

```bash
python -m pip install -r requirements-media.txt
python scripts/build_workflow_diagram.py
python scripts/build_readme_gifs.py
```
