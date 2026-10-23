"""Agent module: remote thin executors controlled by the main harness.

See AGENT_MODULE_SPEC.md for the full design. The main harness is the only brain
and the only peer each agent talks to. This package holds the no-Qt logic:

- ca.py        certificate authority (CA, CSR signing, fingerprints)   [Phase 0]
- enroll.py    one-time enrollment tokens and the CSR -> cert flow      [Phase 1]
- registry.py  agent records in the database                           [Phase 1]
- protocol.py  channel frame format                                    [Phase 2]
- bridge.py    in-process TLS listener, live registry, tool dispatch   [Phase 2-3]
- wrapper.py   the thin executor that runs on the host                 [Phase 2]

Qt lives only in ui/ and the wiring in worker.py / ui/main_window.py.
"""
