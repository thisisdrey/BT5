### Title
Protocol fee on NEP-245/IMT token transfers can be bypassed by splitting a delta into multiple `TokenDiff` intents of magnitude ≤1 - (`contracts/defuse/core/src/intents/token_diff.rs`)

### Finding Description
The binding that should hold is: `fees_collected[T] == Pips::fee_ceil(fee, Σ|negative deltas of T across the batch|)`. The actual implementation computes and collects fees independently **per `TokenDiff` intent**, not per aggregated token across the batch.

`TokenDiff::execute_intent` iterates only over the deltas inside a *single* `TokenDiff.diff` map and, for each negative delta, computes the fee as `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` [1](#0-0) . `token_fee` explicitly exempts `Nep245`/`Imt` tokens from any fee when the transferred `amount <= 1`: `TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {} ... TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO` [2](#0-1) .

Because `execute_signed_intent` calls `intents.execute_intent(...)` once per signed `DefuseIntents` message and each `TokenDiff` inside that message is executed by its own `execute_intent` call with its own local `fees_collected` [3](#0-2) , an attacker who wants to move `-2` of a NEP-245/IMT token can instead submit two separate `TokenDiff` intents, each with `delta = -1` on the same `token_id`, inside the same signed message. Each `execute_intent` call independently computes `token_fee(token_id, 1, fee) == Pips::ZERO`, so no fee is charged on either leg, whereas a single `TokenDiff{delta:-2}` would compute `token_fee(token_id, 2, fee) == fee` and charge `fee.fee_ceil(2)`.

The overall balance invariant enforced at `Engine::finalize` only checks that total deltas across the whole batch net to zero (matched by counterparty legs elsewhere in the batch); it does not re-check per-token fee totals, so nothing catches the divergence.

Note: the question's framing attributes this to `TokenDiff::closure_many`/`closure_deltas` diverging from `execute_intent`. That is not accurate — `closure_many`/`closure_deltas` are off-chain helper functions for computing a counterparty's required closing deltas and are not used to enforce or collect on-chain fees; the actual, exploitable divergence is entirely within `execute_intent`'s per-`TokenDiff` fee computation.

### Impact Explanation
This under-collects protocol fees on any NEP-245 (`Nep245TokenId`) or IMT token transfer by splitting a larger negative delta into multiple unit (`|delta|<=1`) `TokenDiff` intents signed in the same message. The attacker pays zero fee on the entire transferred amount instead of the fee that would apply to the equivalent single-intent transfer, at no cost beyond constructing more intents in the same signature. This is a genuine under-collection of protocol fees credited to `fee_collector`, matching the "protocol fees bypassed or over-collected" Critical category. It is fully repeatable across any NEP-245/IMT token and any amount (an attacker can always decompose a transfer of magnitude `N` into `N` unit legs to pay zero fee regardless of `N`).

### Likelihood Explanation
No special privileges are required — any signer can construct a `DefuseIntents` message containing multiple `TokenDiff` intents on the same token, each with `|delta| <= 1`, and sign it normally. The only precondition is that the token in question is of type `Nep245`/`Imt` (fee-exempt threshold applies to these types only, per `token_fee`), and that the batch's other legs provide the matching counter-deltas to satisfy the zero-sum invariant at `finalize`. This is low-cost and trivially repeatable per transfer.

### Recommendation
Compute and apply the NEP-245/IMT fee exemption (`amount <= 1`) based on the **aggregated** per-token negative delta across the whole signed intents batch (or at least across all `TokenDiff` intents processed within one `execute_signed_intent` call), rather than per individual `TokenDiff.diff` entry. This can be done by accumulating per-token negative deltas across all `TokenDiff` intents in a `DefuseIntents` message before computing `token_fee`, matching the batch-level supply-delta aggregation already performed conceptually (but not applied) in `closure_deltas`.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (or an integration test using near-workspaces sandbox)
// Compare fees collected for:
// (a) two TokenDiff intents in one signed DefuseIntents message, each {mt_token_id: -1},
//     matched by a counterparty leg providing +2 (or two +1 legs) to satisfy zero-sum.
// (b) one TokenDiff intent {mt_token_id: -2}, matched equivalently.
//
// Execute both via Engine::execute_signed_intents with the same protocol_fee (e.g. Pips::ONE_PERCENT).
// Assert: fee_collector's credited balance for mt_token_id in case (a) == 0,
//         while in case (b) == protocol_fee.fee_ceil(2) > 0.
// This demonstrates fees_collected[T] for case (a) != Pips::fee_ceil(fee, total |negative deltas|),
// confirming the fee-bypass via TokenDiff splitting.
```

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

**File:** contracts/defuse/core/src/engine/mod.rs (L76-80)
```rust
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;

        intents.execute_intent(&signer_id, self, hash)?;
        self.inspector.on_intent_executed(&signer_id, hash, nonce);
```
