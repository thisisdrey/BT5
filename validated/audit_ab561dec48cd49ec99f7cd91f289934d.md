### Title
Nep245 dust-fee exemption lets a signer bypass protocol fees entirely by splitting a trade into unit-size legs - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives the protocol fee for `TokenIdType::Nep245` (and `Imt`/`Nep171`) whenever the per-intent `amount <= 1`. Because fee accounting happens independently per `TokenDiff` intent rather than per aggregate signer/token movement in a batch, a signer can split any Nep245 notional into a sequence of `amount == 1` legs and pay zero fee on the whole trade, instead of paying `Pips::fee_ceil` on the true notional.

### Finding Description
The binding that should hold is: for a fixed total notional `N` of a Nep245 token moved by a signer within a batch, the total fee collected should not depend on how that `N` is partitioned across `TokenDiff` intents, i.e. `fee_ceil(N, fee) == Σ fee_ceil(delta_i, fee)` for any partition `N = Σ delta_i`.

In `TokenDiff::token_fee`:
```
match token_id {
    TokenIdType::Nep141 => {}
    TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
    TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
}
fee
``` [1](#0-0) 

This is invoked once per intent, per negative delta, inside `TokenDiff::execute_intent`:
```
if *delta < 0 {
    let amount = delta.unsigned_abs();
    let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
    fees_collected.add(token_id.clone(), fee)...
}
``` [2](#0-1) 

With `fee = Pips::ONE_PERCENT`:
- Single `TokenDiff` with `delta = -2` on a Nep245 token: `amount = 2 > 1`, so `token_fee` returns `fee = ONE_PERCENT`, and `Pips::fee_ceil(2, ONE_PERCENT) = ceil(2 * 10000 / 1_000_000) = 1`.
- Two `TokenDiff` intents each with `delta = -1` on the same Nep245 token (matched by counter-legs elsewhere in the batch to satisfy the zero-sum invariant): each call has `amount = 1`, which hits the `amount <= 1` branch and returns `Pips::ZERO`, so `fee_ceil(1, ZERO) = 0` per leg, `0` total.

`Pips::fee_ceil` itself performs correct rounding:
```
pub fn fee_ceil(self, amount: u128) -> u128 {
    amount.checked_mul_div_ceil(self.as_pips().into(), Self::MAX.as_pips().into())...
}
``` [3](#0-2) 

None of the batch-level guards (`MultiPayload::verify`, nonce/salt checks, `#[private]`/`#[pause]`, `access_control_any`) constrain how a signer partitions a trade into multiple signed `TokenDiff` intents, and the zero-sum invariant enforced elsewhere in the engine only checks that deltas net to zero across the whole batch — it does not re-aggregate per-token fee-eligible amounts before applying the `amount <= 1` carve-out. Consequently an attacker (or attacker + cooperating/self-controlled counterparty legs) can structure any Nep245 trade as a chain of `amount == 1` legs and pay zero protocol fee on the entire notional, regardless of size.

### Impact Explanation
Protocol fees on Nep245 token flows can be under-collected/bypassed entirely by fragmenting trade legs, denying the fee collector revenue it is otherwise entitled to on every Nep245-based trade. This is repeatable per trade, per account, and scales with however many unit legs the attacker is willing to sign/submit, matching the "protocol fees bypassed or over-collected" Critical category.

### Likelihood Explanation
The only precondition is holding ≥2 units of a Nep245 token and being able to construct multiple signed `TokenDiff` intents (or coordinate matching legs) instead of one — well within reach of any unprivileged signer using `execute_intents`/`simulate_intents`. No special role or privileged key is required, and the attack is trivially repeatable.

### Recommendation
Aggregate the fee-eligible amount per `(signer, token_id)` across the whole batch before applying the Nep245/Imt "amount <= 1" dust exemption, rather than evaluating `amount <= 1` independently per `TokenDiff` intent. Alternatively, remove or tighten the dust exemption for `Nep245`/`Imt` token types when the token's semantics are fungible-like, since NEP-245 tokens are not guaranteed to be unique-unit NFTs the way NEP-171 tokens are.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (unit-level, no sandbox needed to demonstrate the mismatch)
use defuse_core::{fees::Pips, intents::token_diff::TokenDiff, token_id::nep245::Nep245TokenId};

#[test]
fn nep245_fee_bypass_by_splitting() {
    let token_id = Nep245TokenId::new("mt.near".parse().unwrap(), "ft1".to_string()).into();
    let fee = Pips::ONE_PERCENT;

    // Single leg moving 2 units
    let fee_single = TokenDiff::token_fee(&token_id, 2, fee).fee_ceil(2);

    // Two legs each moving 1 unit (same total notional)
    let fee_split = TokenDiff::token_fee(&token_id, 1, fee).fee_ceil(1) * 2;

    assert_ne!(fee_single, fee_split, "fee should scale with notional regardless of leg-splitting");
    assert_eq!(fee_single, 1);
    assert_eq!(fee_split, 0); // fee fully bypassed by splitting
}
```
This can be extended to a `near-workspaces` sandbox test executing `execute_intents` twice — once with one `TokenDiff{diff: {token: -2}}` and once with two `TokenDiff{diff: {token: -1}}` intents (paired with matching `+1`/`+2` counter-legs to satisfy the zero-sum invariant) — and asserting the fee collector's balance differs between the two runs (`1` vs `0`).

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L70-78)
```rust
            if *delta < 0 {
                let amount = delta.unsigned_abs();
                let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);

                // collect fee
                fees_collected
                    .add(token_id.clone(), fee)
                    .ok_or(DefuseError::BalanceOverflow)?;
            }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L206-216)
```rust
    #[inline]
    pub fn token_fee(token_id: impl Into<TokenIdType>, amount: u128, fee: Pips) -> Pips {
        let token_id = token_id.into();
        match token_id {
            TokenIdType::Nep141 => {}
            TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
            // do not take fees on NFTs and MTs with |delta| <= 1
            TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
        }
        fee
    }
```

**File:** crates/primitives/fees/src/lib.rs (L116-121)
```rust
    #[inline]
    pub fn fee_ceil(self, amount: u128) -> u128 {
        amount
            .checked_mul_div_ceil(self.as_pips().into(), Self::MAX.as_pips().into())
            .unwrap_or_else(|| unreachable!())
    }
```
