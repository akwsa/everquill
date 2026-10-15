# Public Meta-only package boundary

The Owner selected an official-Meta-only public MVP. This selection does not
clear legacy provenance, grant publication permission or claim live provider
acceptance. The package uses a reviewed path selection; local files excluded
from an archive are preserved rather than deleted.

## Included runtime

`everquill` supplies the provider-neutral reply contract, official Meta adapter,
context/proposal controller, session recovery, validation/executor and terminal
UX. `everquill_gateway` contains only a version marker, stdlib error types,
local context/path helpers and atomic state migration/writing helpers. The
namespace is shared support, not an exposed gateway transport.

Public runtime requires no third-party Python dependencies. The historical
transport's dependencies are a private development group only, not wheel
`Requires-Dist` requirements. Retained upstream MIT software/adaptations retain
the complete root `LICENSE`; this is not a sole-authorship or clean-room claim.

## Excluded paths and relationships

- `src/everquill/muse_backend.py`: private MuseReasoningBackend adapter; public
  provider selection rejects Muse without fallback.
- `src/everquill_gateway/cli.py`, `gateway.py`, `update.py`, `__main__.py`:
  historical gateway commands, transport/auth and updater; public gateway
  commands are unsupported. Source-checkout private functionality may still
  load these files when physically present.
- `src/everquill_gateway/routes.json`, `desc0.bin`, `desc1.bin`: extracted
  method data and framing descriptors used by that transport. Excluded pending
  provenance/distribution review, not declared legally restricted/proprietary.
- `src/muse_cli/` and `src/muse_code/`: original historical implementations,
  including backups. Preserved locally; absent from public archives/candidate.
- `docs/PROTOCOL.md`, non-public `docs/`, root historical `README.md`,
  `EVERQUILL-*.md`, `goals.md`, `AGENTS.md`, migration-control/contract files,
  portfolio/evidence trees and `skills/`: historical/private guidance and review
  records, not runtime inputs for Meta; no root-directory wildcard selection.
- Assets/images, old ZIPs, backups, caches, `.git`, local environments,
  credential/session/browser state and temporary files: not selected. Their
  absence from artifacts does not assert their safety or ownership.
- Mixed legacy-provider tests remain local. The source archive includes focused
  public Meta mapping/packaging and Node tests only; full private checkout
  regression remains separate and must not be claimed from that subset.

## Verification and remaining review

The exact source allowlist is [package-manifest.json](package-manifest.json).
Wheel runtime paths must exactly match its runtime files plus generated
metadata. Sdist payload must exactly match its source selection plus generated
PKG-INFO. The selected `.gitignore` contains exclusion patterns only; Hatch
automatically retains it in the sdist and it is explicitly inventoried, not
used as proof of archive privacy. Both are inspected for unexpected members, legacy transport/assets,
internal evidence, backups and private paths. A wheel rebuilt from the sdist
must preserve the same runtime boundary.

Installed checks run outside the checkout with isolated imports and test the
real executable, including Meta fixtures, continuation/correlation, error codes
and rejected legacy commands/modules. Offline fixtures do not establish live
API availability or remote cancellation.

D1 exclusions are respected by absence, not satisfied by a disclaimer. Broader
D2 contribution/third-party provenance questions remain Owner review matters;
no blanket artifact rights clearance is claimed. Archive selection cannot
sanitize existing private Git history; a public repository would require its
own selected clean lineage and explicit Owner authorization. Task 10, live
Meta, progressive streaming and strict cancellation proof remain deferred.
