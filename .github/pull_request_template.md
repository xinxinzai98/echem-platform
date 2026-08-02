## What changed

Describe the user-visible and maintainer-visible changes.

## Why

Explain the problem, evidence, and chosen boundary.

## Validation

- [ ] `python3 -m unittest discover -s tests -v`
- [ ] `python3 scripts/run_demo.py`
- [ ] `python3 scripts/check_public_tree.py`
- [ ] `node --check static/app.js`

## Safety and data checklist

- [ ] Source experimental files remain read-only.
- [ ] Loopback-only, no-serial, and no-instrument-control boundaries are unchanged or explicitly reviewed.
- [ ] No real experimental data, private path, account name, network address, credential, log, installer, SDK, or vendor-proprietary file is included.
- [ ] New fixtures are synthetic or have documented publication authorization and provenance.
- [ ] Documentation, validation, limitations, and changelog are updated where needed.
