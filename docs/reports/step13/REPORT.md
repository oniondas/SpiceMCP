# Step 13: version 1.0.0, the interface is frozen

End of Phase 3 of the industry-readiness roadmap.

## Before and after

| | 0.17.0 | 1.0.0 |
|---|---|---|
| Tool names and inputs | free to change in any MINOR release | frozen for 1.x |
| Check | none | `tests/test_api_freeze.py` against `tests/fixtures/api_v1.json` |
| Frozen | - | 14 tools (inputs, required set, accepted values), 13 templates (spec keys) |

## Rules for 1.x

- Add: tools, templates, optional parameters, accepted values, answer fields (MINOR).
- Never before 2.0.0: remove or rename any of the above, make an input required.
- Numbers in answers may improve with better models; names and meaning stay.

## Not claimed

1.0 is an interface promise. Designs are checked in ngspice and LTspice, not on a built
board. Phase 4 (bench test plan and parts) needs hardware and the owner's go-ahead.
