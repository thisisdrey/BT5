# Q1372: account_not_frozen_for_authority: migrated or delegated authority path accepts the wrong signer [a-frozen-account-just-after] [partial-transition]

## Question
Can an unprivileged attacker reach `account_not_frozen_for_authority` from `lending_account_withdraw` with a frozen account just after order or liquidation flags changed so a migrated, delegated, or PDA-owned account accepts the wrong authority, violating `freeze semantics must block every forbidden value-moving path for the affected authority and account` and causing `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a frozen account just after order or liquidation flags changed
- Exploit idea: Check all alternate authorization paths for mismatched signer identity, stale authority fields, or incorrect PDA derivation assumptions. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Model authority transfer/migration and verify that only the intended signer path can mutate or close the account at each phase. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
