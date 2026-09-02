### Title
NEP-245/IMT protocol fees can be bypassed by splitting a bulk transfer into many unit-amount `TokenDiff` legs - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives the protocol fee whenever a single leg's `|delta| <= 1` for `Nep245`/`Imt` token types, intending to avoid charging fees on indivisible NFT-like units. Because the fee is computed per-`TokenDiff`-intent leg rather than on the aggregate amount moved by a signer for a given token, an attacker can split one large fungible-style NEP-245 outflow into N legs of exactly 1 unit each (even inside a single signed `DefuseIntents` payload) and pay zero fee on every leg, whereas a single `TokenDiff` moving the same total amount N would be charged `protocol_fee.fee_ceil(N)`.

### Finding Description
The broken binding: for a signer moving total amount `N` of a NEP-245 token id `T` out of their balance in one batch, the protocol expects
`fee_collected(T) == protocol_fee.fee_ceil(N)`
when expressed as one `TokenDiff{delta: -N}`. Instead, when expressed as `N` separate `TokenDiff{delta: -1}` legs (all valid, distinct, and combinable into one signed `DefuseIntents` message or multiple payloads from the same signer), the actual result is
`fee_collected(T) == sum_{i=1}^{N} TokenDiff::token_fee(T, 1, protocol_fee).fee_ceil(1) == 0`,
since `token_fee` at [1](#0-0)  returns `Pips::ZERO` for `Nep245`/`Imt` whenever `amount <= 1`. These two values are not equal for any `protocol_fee > 0` and `N > 1`, breaking "protocol fees bypassed" invariant.

Root cause and path: `TokenDiff::execute_intent` computes the fee independently per leg inside the loop over `self.diff` items, using `delta.unsigned_abs()` as `amount` for that single leg only: [2](#0-1) . The fee-exemption threshold in `token_fee` is a per-leg amount check with no aggregation across the multiple `TokenDiff` intents that can appear in one `DefuseIntents` message or across multiple signed payloads submitted together in one `execute_intents` call: [3](#0-2)  and [4](#0-3) . Since each `Intent::TokenDiff` is executed and fee-charged independently, an attacker signs a single `DefuseIntents` containing `N` `TokenDiff` intents, each `{diff: {T: -1, U: +k}}` (or matched by other legs/counterparties elsewhere in the batch to satisfy the batch-level netting invariant enforced in `finalize`/`Deltas::finalize`), and every leg individually falls into the `amount <= 1` branch of `token_fee`, yielding `Pips::ZERO` fee on each.

No existing guard catches this: `MultiPayload::verify`, nonce/salt checks, and the batch-level `InvariantViolated::UnmatchedDeltas` check in `finalize` (seen in `engine/state/deltas.rs`) only ensure aggregate token deltas net to zero across the whole batch - they do not recompute or aggregate the fee based on the total amount moved per token per signer. The per-leg exemption threshold in `token_fee` (`amount > 1` vs `amount <= 1`) is checked before any aggregation, so it structurally cannot see the cumulative amount.

### Impact Explanation
Value that should be collected by the `fee_collector` is never credited: for any nonzero `protocol_fee` and NEP-245 (or IMT) token, a user swapping/moving out an arbitrarily large quantity `N` pays `0` fee instead of `protocol_fee.fee_ceil(N)`, simply by encoding the transfer as `N` `TokenDiff` legs of `-1` each (matched by corresponding `+1`/aggregated positive legs elsewhere to satisfy the net-zero invariant). This is repeatable without bound across any NEP-245/IMT token, any account, and any batch, and costs the attacker nothing beyond ordinary gas for a larger message. It matches the "protocol fees bypassed" Critical category since it is a systematic under-collection of protocol fees owed to `fee_collector`, achievable by any unprivileged signer.

### Likelihood Explanation
Preconditions are minimal and fully within an unprivileged attacker's reach: hold/trade a NEP-245 (or IMT) token under a Verifier with `fee() > 0`, and be able to sign a `DefuseIntents` message with multiple `TokenDiff` intents (or multiple signed payloads) in one `execute_intents`/`simulate_intents` call - both already supported operations for any user. No role, relayer key, or victim key is needed. The attack is trivially cheap (one signature, N small intents in one message) and fully repeatable.

### Recommendation
Compute and apply the NEP-245/IMT fee exemption based on the aggregate negative delta for a given `(signer, token_id)` across the whole batch (or at minimum across all `TokenDiff` intents in a single `DefuseIntents` message), not per individual `TokenDiff` leg. Alternatively, remove the `amount <= 1` exemption for `Nep245`/`Imt` entirely (reserving the exemption only for genuinely non-fungible `Nep171` token ids), since NEP-245 token ids can represent arbitrarily fungible balances.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core/src/intents/token_diff.rs (or a new test module)
use crate::{fees::Pips, token_id::nep245::Nep245TokenId, intents::token_diff::TokenDiff, AccountId};

#[test]
fn nep245_fee_split_bypass() {
    let token = Nep245TokenId::new("mt.near".parse::<AccountId>().unwrap(), "ft1".to_string()).into();
    let protocol_fee = Pips::ONE_PERCENT; // nonzero fee
    let n: u128 = 1000;

    // Single TokenDiff moving all N units at once: fee should be nonzero
    let single_leg_fee = TokenDiff::token_fee(&token, n, protocol_fee).fee_ceil(n);
    assert!(single_leg_fee > 0);

    // N separate TokenDiff legs of 1 unit each: sum of fees collected
    let split_fee_total: u128 = (0..n)
        .map(|_| TokenDiff::token_fee(&token, 1, protocol_fee).fee_ceil(1))
        .sum();
    assert_eq!(split_fee_total, 0);

    // Binding broken: fee collected differs based on batching, despite moving the same total amount
    assert_ne!(single_leg_fee, split_fee_total);
}
```
This can be extended into a `near-workspaces`/sandbox integration test (mirroring `tests/src/tests/defuse/intents/token_diff.rs::swap_many`) that signs one `DefuseIntents` payload containing N `TokenDiff{diff:{T:-1, U:+k}}` intents (paired against a counterparty supplying `U`), executes it via `execute_intents`, and asserts the resulting `fee_collector` balance for `T` is `0`, contrasted against a single `TokenDiff{diff:{T:-N, U:+K}}` intent producing `protocol_fee.fee_ceil(N) > 0` credited to `fee_collector`.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L59-78)
```rust
        for (token_id, delta) in &self.diff {
            if *delta == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            // add delta to signer's account
            engine
                .state
                .internal_apply_deltas(signer_id, [(token_id.clone(), *delta)])?;

            // take fees only from negative deltas (i.e. token_in)
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

**File:** contracts/defuse/core/src/engine/mod.rs (L32-40)
```rust
    pub fn execute_signed_intents(
        mut self,
        signed: impl IntoIterator<Item = MultiPayload>,
    ) -> Result<Transfers> {
        for signed in signed {
            self.execute_signed_intent(signed)?;
        }
        self.finalize()
    }
```

**File:** contracts/defuse/core/src/intents/mod.rs (L97-113)
```rust
impl ExecutableIntent for DefuseIntents {
    fn execute_intent<S, I>(
        self,
        signer_id: &AccountIdRef,
        engine: &mut Engine<S, I>,
        intent_hash: [u8; 32],
    ) -> Result<()>
    where
        S: State,
        I: Inspector,
    {
        for intent in self.intents {
            intent.execute_intent(signer_id, engine, intent_hash)?;
        }
        Ok(())
    }
}
```
