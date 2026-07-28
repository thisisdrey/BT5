# Q3367: TrustedCalls delegate execution: batch order / payload extension / atomic batch intent

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the order of several batched trusted calls that share state across the same Safe or target while another Safe in the system has different delegates or target trust expectations and use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary, breaking the rule that batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit and leading to Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the order of several batched trusted calls that share state across the same Safe or target
- Exploit idea: use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary
- Invariant to test: batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit
- Expected Immunefi impact: Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface
- Fast validation: Fuzz trusted selectors with attacker-chosen trailing calldata and ensure the whitelist boundary cannot be widened by payload shape alone.
