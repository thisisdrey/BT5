# Q428: change_asset_shares: frozen or disabled account still reaches value-moving code [a-withdraw-that-targets-an] [cycle]

## Question
Can an unprivileged attacker route `lending_account_withdraw` through `change_asset_shares` with a withdraw that targets an account near initial-health failure so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns` and causing `Critical: direct theft or unauthorized withdrawal of protocol/user funds`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_asset_shares`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw that targets an account near initial-health failure
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns
- Expected Immunefi impact: Critical: direct theft or unauthorized withdrawal of protocol/user funds
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
