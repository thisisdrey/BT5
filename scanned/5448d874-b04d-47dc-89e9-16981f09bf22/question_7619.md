# Q7619: Recovery Path Lock in get_function_param_and_local_names

## Question
Can `third_party/move/move-vm/runtime/src/source_locator.rs::get_function_param_and_local_names` push honest user state into a condition that remains valid but cannot later be recovered, claimed, or progressed by the rightful owner?

## Target
- File/function: third_party/move/move-vm/runtime/src/source_locator.rs::get_function_param_and_local_names
- Entrypoint: Submit a transaction, view call, or package publish that reaches `get_function_param_and_local_names` during runtime execution.
- Attacker controls: transaction payloads, module bytes, view arguments, resource values, gas fields, and attacker-chosen state transitions
- Exploit idea: Exploit runtime state transitions that preserve validity checks while silently removing future recovery paths.
- Invariant to test: No accepted runtime state transition should destroy the only legitimate path to recover or progress honest user value.
- Expected Immunefi impact: Critical. Irreversible fund lock, permanently unspendable balances, or non-recoverable loss of access to user or protocol value caused by broken execution, storage, object, staking, vesting, multisig, or resource-account flows.
- Fast validation: Add a state-machine regression test that executes the edge case and proves the rightful owner can still complete the expected recovery path.
