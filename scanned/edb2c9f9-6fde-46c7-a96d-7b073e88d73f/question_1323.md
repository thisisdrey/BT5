# Q1323: account_not_frozen_for_authority: authority binding bypass on account state mutation [a-frozen-account-just-after] [role-reuse]

## Question
Can an unprivileged attacker call `lending_account_withdraw` and make `account_not_frozen_for_authority` accept a frozen account just after order or liquidation flags changed so another user's account state mutates without valid authority, violating `freeze semantics must block every forbidden value-moving path for the affected authority and account` and leading to `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a frozen account just after order or liquidation flags changed
- Exploit idea: Probe signer checks, transferred authorities, PDA ownership, and migrated-account state so public calls cannot rewrite who controls a margin account. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Use two users and assert the controlled call cannot mutate the victim account's owner, flags, balances, or closeability. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
