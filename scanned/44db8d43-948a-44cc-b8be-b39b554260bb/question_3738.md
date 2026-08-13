# Q3738: lending_account_start_flashloan: session reuse or replay after a valid close [a-start-call-when-the] [repay-domain]

## Question
Can an unprivileged attacker replay or reuse `lending_account_start_flashloan` with a start call when the account recently exited another multi-phase state so `lending_account_start_flashloan` treats an old flashloan session as still valid, violating `starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved` and leading to `Critical: direct theft or unbacked debt through flashloan misuse`? Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_start_flashloan`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a start call when the account recently exited another multi-phase state
- Exploit idea: Check whether hashes, discriminators, or instruction bindings used to link start and end are one-time and domain-separated. Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.
- Invariant to test: starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved
- Expected Immunefi impact: Critical: direct theft or unbacked debt through flashloan misuse
- Fast validation: Attempt to replay the exact end conditions after one successful close and assert all subsequent reuse attempts fail. Compare principal, fee, vault delta, and user delta under the controlled path and assert close succeeds only on exact repayment.
