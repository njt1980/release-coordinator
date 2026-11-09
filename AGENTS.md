# Instructions for AI Coding Assistants

These rules apply to all AI-assisted work in this repository.

## Plan before code

- Inspect the relevant files and repository conventions before proposing changes.
- Before editing, provide a concise plan describing the intended files, behavior, and validation.
- Do not begin implementation until the user approves the plan. A request that explicitly directs implementation of a specific change counts as approval for that change, but does not authorize unrelated work.
- Keep changes within the approved scope. If new requirements materially alter the plan or architecture, stop and request permission.

## Types and data contracts

- Use strict Python type hints for all public functions, methods, and module-level interfaces.
- Model structured inputs and outputs, especially data crossing service or tool boundaries, with Pydantic models. Do not pass loosely structured dictionaries where a typed model is appropriate.
- Validate external data at the boundary where it enters the system; preserve clear, explicit error behavior.

## Modularity and integration boundaries

- Keep functions focused, small, and independently testable.
- Organize integrations as isolated, typed tool modules that simulate MCP boundaries: each module should have a clear input/output contract and must not reach into unrelated integrations' internals.
- Keep orchestration separate from discovery, core domain types, and external-service adapters. Do not introduce cross-layer coupling without explicit approval.

## Dependencies and architecture

- Do not add, remove, or upgrade external dependencies unless the user explicitly authorizes the dependency change.
- Do not change architectural scope, introduce new services or infrastructure, or replace established design choices without explicit user permission.
- Prefer existing project dependencies and patterns; explain any requested change that cannot be completed without an additional dependency.

## Implementation and verification

- Make the smallest complete change that satisfies the approved request; do not generate unrelated application logic.
- Add or update tests for behavior changes and run the narrowest relevant validation commands.
- Report the files changed, validation performed, and any remaining limitations.
