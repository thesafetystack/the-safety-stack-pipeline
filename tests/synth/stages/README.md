TEST-ONLY stand-in stages for the build scenario (`tests/test_scenario_m5.py`, T5.1a). Selected by
`STACK_WORKFLOWS_ROOT=tests/synth/stages` (read by `stack.stage_runner.stage_module_path`); `fold.*`
still dispatches to `stack.fold.stage`. Same protocol as `workflows/echo_test/main.py`. They stand in
for the real `collect.*`, `gather`, `triage`, `acquire`, `annotate.screen` (tracks A, B, C) so the
build machinery is tested without them; they are not those stages and know nothing of providers.
