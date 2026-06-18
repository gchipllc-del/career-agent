# External red-team tools (dev / CI only — never imported at runtime)

The stdlib suite (`tests/test_redteam.py`) is the always-on regression + precision
gate over `fixtures/injection_corpus.json`. These external tools are heavier,
out-of-band audits you run occasionally against the **live** endpoint — they are
**not** runtime dependencies and must never be imported by `core.py`/`server.py`.

## promptfoo — CI red-team harness (Node, MIT)
[github.com/promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) — now part of
OpenAI, still open-source MIT. Point its red-team module (prompt injection, indirect
injection, jailbreaks) at the running dashboard API:

```bash
npx promptfoo@latest redteam init        # scaffold a config
# set the target to http://127.0.0.1:8000/api/runs (job_text field)
npx promptfoo@latest redteam run
```
Use it to prove the **fence holds end-to-end** through the real tailor→validate graph,
not just the regex layer.

## garak — model/probe scanner (Apache-2.0, NVIDIA)
[github.com/NVIDIA/garak](https://github.com/NVIDIA/garak). Its `latentinjection`
family includes a **`LatentInjectionResume`** probe — injections hidden in
resume/report context, an exact match for this app's threat model:

```bash
pip install garak                          # in a 3.10+ env, NOT the app's runtime
python -m garak --model_type ... --probes latentinjection.LatentInjectionResume
```
Run periodically; it's an audit, not a gate.

## Optional: vendor a public eval slice
You may add a slice of [deepset/prompt-injections](https://huggingface.co/datasets/deepset/prompt-injections)
(**Apache-2.0** — keep the attribution/NOTICE) into `fixtures/` as an independent
eval set for the regex layer. Our committed corpus is hand-authored (no third-party
license) and tailored to job-posting/resume injection; deepset is a broader,
general-purpose set. Prefer deepset over Lakera's PINT for a vendored fixture — PINT
is a benchmark harness, not a freely-redistributable labeled corpus.

> Trust model: everything here is dev/CI. The runtime defense is the always-on
> `core.py` fence + advisory scan (`tests/test_redteam.py` guards it), with the
> human-approval gate as the final backstop.
