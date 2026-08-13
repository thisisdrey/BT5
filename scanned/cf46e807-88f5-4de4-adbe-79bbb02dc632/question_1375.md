# Q1375: account_not_frozen_for_authority: migrated or delegated authority path accepts the wrong signer [a-frozen-account-where-sync] [role-reuse]

## Question
Can an unprivileged attacker reach `account_not_frozen_for_authority` from `lending_account_withdraw` with a frozen account where sync-like helper paths run before the main action so a migrated, delegated, or PDA-owned account accepts the wrong authority, violating `freeze semantics must block every forbidden value-moving path for the affected authority and account` and causing `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a frozen account where sync-like helper paths run before the main action
- Exploit idea: Check all alternate authorization paths for mismatched signer identity, stale authority fields, or incorrect PDA derivation assumptions. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Model authority transfer/migration and verify that only the intended signer path can mutate or close the account at each phase. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
