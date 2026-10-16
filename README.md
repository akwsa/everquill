# Everquill

Everquill is an independent terminal coding-agent harness, not an official Meta
product. This public distribution supports only the Meta provider. It retains
local project context, reviewed proposals/diffs, explicit approvals, safe file
writes, validation, sessions and a Node terminal UI. The model does not grant
itself authority to execute local tools or modify files.

The Python runtime uses the standard library. Python 3.11 or later is required.
Node.js 22 or later is optional for the TUI; classic and one-shot modes do not
need Node. The public wheel and source archive exclude the historical Muse
transport, extracted routes/descriptors, browser authentication and internal
reports. No browser cookies or unofficial service access is supported.

## Installation and basic use

A ready-to-install wheel is not publicly available on a package registry. After
reviewing an Owner-approved local artifact, install it with pip in a virtual
environment:

```sh
python -m pip install ./everquill-0.1.0-py3-none-any.whl
everquill --help
everquill --version
everquill --provider meta --session "Explain this project"
everquill --provider meta --continue "Review the previous answer"
everquill --no-tui "Explain sample.py"
everquill --proposal "Propose a small correction"
everquill --iterate "Propose and apply a correction"
```

Live requests require an Owner-provisioned `MODEL_API_KEY` in the process
environment. Never put credentials in project files or share them in reports.
`EVERQUILL_PROVIDER=meta` is supported; no provider fallback exists. Without a
key an approved request reports an actionable error and exits nonzero. Live
Meta API verification is deferred; offline fixtures verify the request/reply
contract, not service availability, account entitlement or current model access.

`everquill gateway ...` and `--provider muse` are unsupported in the public
artifact and return a clear nonzero result. Private historical source remains
outside this package pending provenance/distribution review. Exclusion is not
a legal conclusion about that material.

## Consent, tools and sessions

Every outgoing project context requires explicit lowercase `y` or `yes`.
The model-dispatched tool is bounded, screened, read-only `read_file`. No
shell/write tool is automatically enabled. Sending tool results requires fresh
consent. Proposed MODIFY/CREATE/DELETE edits use existing diff/validation and
explicit approval; DELETE additionally requires a target-specific confirmation.
The TUI cannot approve DELETE and rejects it rather than weakening approval.

Run from your project directory. Session state defaults to
`~/.config/everquill/sessions.json`. It contains normalized project paths,
provider response/call identities and possibly screened project text. Keep it
private; never commit it. Atomic replacement prevents partial JSON but is not
an encrypted/authenticated store or a concurrent-writer lock.

Pending read-tool recovery uses `--continue`:

- `prepared`: fresh execution approval before reading, then fresh send approval.
- `results_ready`: send the exact saved output after consent, without rereading.
- `executing` or `continuation_started`: outcome ambiguous; stop for manual
  review and never blindly execute/resend. No exactly-once guarantee is made.

Recovery finishes only the saved read-only turn. An appended new prompt is not
sent in the same recovery invocation; make a subsequent explicit request.
Missing/corrupt correlation metadata fails safely; historical text-only state
is supported. Deleting a whole journal is indistinguishable from legacy absence.

## Exit codes and limitations

One-shot/pending-recovery codes: `0` completed answer/applied proposal/valid no
changes; `1` failed or incomplete work, including provider, persistence,
ambiguous recovery and proposal rejection; `2` argument/unsupported-command or
saved-session lookup error; `130` cancellation or declined context/tool consent,
including EOF/unknown input. Interactive lifetime is a separate contract.

Task 10 process containment remains deferred. Approved test execution has
LIMITED isolation and is not an OS sandbox; arbitrary descendants, outside
files and account permissions are not fully contained. Timeout/cancellation
can stop local waiting without proving remote work or every descendant stopped.
Progressive streaming and strict cancellation bounds remain deferred. No live
Meta verification or universal security guarantee is claimed.

## License and source boundary

The upstream MIT copyright and permission notice is retained in `LICENSE`.
Everquill originated as an extension of nikships/muse-cli; retained local helper
code and adaptations do not constitute a claim of sole authorship or clean-room
provenance. The public boundary and exclusion rationale are in
[docs/public/BOUNDARY.md](docs/public/BOUNDARY.md);
[docs/public/package-manifest.json](docs/public/package-manifest.json)
is an explicit source selection, not publication permission. Broader historical
D1/D2 records remain private and unchanged. Owner review of retained attribution
and final publication is still required.

[Bahasa Indonesia](docs/public/README.id.md)
