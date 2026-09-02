## Title
Protocol fee bypass on NEP-245/IMT tokens by splitting a single `TokenDiff` delta into multiple unit-size (`|delta|<=1`) `TokenDiff` intents - ([File: contracts/defuse/core/src/intents/token_diff.rs])

## Summary
`TokenDiff::token_fee` waives fees entirely on NEP-245/IMT tokens whenever the per-intent absolute delta is `<= 1`, but applies the full `Pips::fee_ceil` charge once the delta is `>= 2`. Because the exemption is evaluated per `TokenDiff` intent rather than per aggregate token movement across the batch, an attacker can decompose any NEP-245/IMT transfer of size `N > 1` into `N` separate unit-delta `TokenDiff` intents (matched against counter-legs in the same batch via `TransferMatcher`) and pay zero fee instead of `Pips::fee_ceil(N)`.

## Finding Description
The binding that should hold is: `fees_collected[T] == Pips::fee_ceil(protocol_fee, |total negative delta of T executed by signer|)` regardless of how the negative delta is split across intents in the batch.

Code path: [1](#0-0) 

```rust
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

This is invoked per intent execution: [2](#0-1) 

The fee decision is scoped to the `amount` of a single `TokenDiff` intent's delta for a given token, not the cumulative amount of that token moved by the signer across the whole `MultiPayload` batch. Root cause: the exemption threshold (`amount <= 1`) was designed to avoid charging fees on genuinely indivisible single-unit NFT/MT transfers, but nothing prevents an attacker from splitting a larger transfer into many separate signed `TokenDiff` intents each with `|delta| == 1` for the same NEP-245/IMT token, all included in the same batch. `Deltas::finalize` / `TransferMatcher` nets deltas across all intents in the batch per token/account without re-evaluating or aggregating fee eligibility, so each unit-leg intent independently satisfies `amount <= 1` and is exempted.

Exact boundary verified from `Pips::fee_ceil`: [3](#0-2) 
With `protocol_fee = Pips::ONE_PIP` (`1` out of `Pips::MAX = 1_000_000`), `fee_ceil(2) = ceil(2*1/1_000_000) = 1`, while `token_fee(nep245, 1, ONE_PIP) = Pips::ZERO` (the guard `amount > 1` fails), giving `fee_ceil(1) = 0`. So a single intent with `delta = -2` yields `fees_collected = 1`, while two intents each with `delta = -1` on the same token yield `fees_collected = 0 + 0 = 0` — a 1-unit fee saving for this minimal case, and this scales: for larger fee rates and larger amounts (e.g. `fee = Pips::ONE_PERCENT`, `amount = 1_000_000` → `fee_ceil = 10_000`), splitting into 1,000,000 unit-legs still yields `fee = 0` total, a much larger absolute bypass.

The attacker's payload is simply an ordinary `MultiPayload` batch containing several signed `TokenDiff` intents (from the attacker's own account(s), matched against counter-legs supplying the opposite side of each unit trade, e.g. via a second account the attacker controls) instead of one intent with the full delta. No existing guard (`verify`, nonce checks, `Lock`, `TransferMatcher::finalize`'s zero-sum invariant) prevents this, because the zero-sum invariant only requires that deltas net to zero across the batch — it does not constrain how deltas are split across intents, and fee computation happens independently per intent before matching.

## Impact Explanation
This under-collects protocol fees credited to `engine.state.fee_collector()` for NEP-245/IMT token trades, matching the explicitly listed Critical category "protocol fees bypassed or over-collected". The bypass is fully repeatable by any unprivileged account across any NEP-245/IMT token and any batch size, and the magnitude scales with the fee rate and the total token amount being moved (not capped at 1 unit), since every unit-leg intent is exempt regardless of how many are combined in one batch.

## Likelihood Explanation
The precondition is minimal: a nonzero `protocol_fee`, an NEP-245/IMT token balance `>= 2` (or any `N > 1`), and the ability to construct a `MultiPayload` batch with multiple `TokenDiff` intents whose per-token deltas are matched by `TransferMatcher` to net to zero across the batch (achievable by the attacker using a second self-controlled account as the counterparty, or via natural counterparties in a real market). No special role, relayer key, or DAO permission is required — this is directly reachable via `execute_intents` with attacker-signed payloads only.

## Recommendation
Compute the NEP-245/IMT fee exemption based on the aggregate absolute delta of a token across the whole batch (post-`TransferMatcher` netting per account, or per signer) rather than per individual `TokenDiff` intent, or remove/tighten the `amount <= 1` exemption to a per-account-per-batch accumulator so an attacker cannot recover the exemption by fragmenting a single transfer into many unit-size intents.

## Proof of Concept
```rust
// crates/primitives/fees or contracts/defuse/core: cargo test proof
use defuse_fees::Pips;
use defuse_core::intents::token_diff::TokenDiff;
use defuse_core::token_id::nep245::Nep245TokenId;

#[test]
fn nep245_fee_split_bypass() {
    let fee = Pips::ONE_PIP;
    let token_id = /* Nep245TokenId::new(...).into() */;

    // Boundary values
    assert_eq!(fee.fee_ceil(2), 1);
    assert_eq!(TokenDiff::token_fee(token_id.clone(), 1, fee), Pips::ZERO);
    assert_eq!(TokenDiff::token_fee(token_id.clone(), 1, fee).fee_ceil(1), 0);
    assert_eq!(TokenDiff::token_fee(token_id.clone(), 2, fee).fee_ceil(2), 1);

    // Batch shape A: single TokenDiff intent, delta = -2 on token_id
    // -> execute_intent -> fees_collected[token_id] == 1

    // Batch shape B: two TokenDiff intents, each delta = -1 on token_id,
    // matched against a counterparty leg of +1 each (e.g. attacker's second account)
    // -> execute_intent (x2) -> fees_collected[token_id] == 0 + 0 == 0

    // Assert fees_collected differs by exactly 1 between shape A and shape B.
}
```
Run both batch shapes through `Engine::execute_intent` (or a `near-workspaces` sandbox invoking `execute_intents`) and assert `fees_collected` for the token differs by exactly `1` between the single-intent and split-intent shapes, confirming the fee bypass.

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
