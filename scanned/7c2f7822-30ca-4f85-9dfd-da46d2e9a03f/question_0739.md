# Q739: check_utilization_ratio: frozen or disabled account still reaches value-moving code [a-same-slot-sequence-that] [cache-order]

## Question
Can an unprivileged attacker route `lending_account_borrow` through `check_utilization_ratio` with a same-slot sequence that first changes liquidity then borrows so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and causing `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a same-slot sequence that first changes liquidity then borrows
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
