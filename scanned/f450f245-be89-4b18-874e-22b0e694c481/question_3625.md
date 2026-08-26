# Q3625: UniversalStateInit action state/deposit accounting — deterministic_account_id.rs

## Question
Can an unprivileged mainnet account, entering through a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account, an init carrying maximal state entries and a deposit exactly at the storage-staking floor, when combined with a DeployContract earlier in the same action list, and additionally when combined with a DeleteAccount later in the same action list, reach `len_bytes` in `core/primitives-core/src/deterministic_account_id.rs` and create an account whose storage cost was never charged, or whose deposit was charged twice, breaking the invariant that state written by an init is fully paid for by the deposit attached to that same action, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives-core/src/deterministic_account_id.rs` :: `len_bytes`
- Entrypoint: a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account
- Attacker controls: an init carrying maximal state entries and a deposit exactly at the storage-staking floor; when combined with a DeployContract earlier in the same action list; when combined with a DeleteAccount later in the same action list
- Exploit idea: create an account whose storage cost was never charged, or whose deposit was charged twice
- Invariant to test: state written by an init is fully paid for by the deposit attached to that same action
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test comparing storage_usage against the charged deposit after init
