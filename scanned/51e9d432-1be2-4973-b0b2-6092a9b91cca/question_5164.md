# Q5164: promise batch action ordering and gas weight — universal_account_id.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a batch mixing weighted and fixed gas attachments so the weight denominator is zero or one, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `is_universal_account_id` in `core/primitives-core/src/universal_account_id.rs` and make unspent gas distribution divide by zero, or hand one promise more gas than was prepaid, breaking the invariant that distributed gas never exceeds the unspent prepaid gas of the calling receipt, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives-core/src/universal_account_id.rs` :: `is_universal_account_id`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a batch mixing weighted and fixed gas attachments so the weight denominator is zero or one; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: make unspent gas distribution divide by zero, or hand one promise more gas than was prepaid
- Invariant to test: distributed gas never exceeds the unspent prepaid gas of the calling receipt
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: unit test on gas-weight distribution with degenerate weights
