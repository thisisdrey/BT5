### Title
Splitting a Nep245/Imt trade into unit-amount `TokenDiff` intents bypasses protocol fee - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` (contracts/defuse/core/src/intents/token_diff.rs:206-216) returns `Pips::ZERO` whenever a Nep245/Imt token's per-intent `delta` has `unsigned_abs() <= 1`, and `TokenDiff::execute_intent` (contracts/defuse/core/src/intents/token_diff.rs:41-104) computes and credits this fee purely at the granularity of a single `TokenDiff` intent's `diff` map entry. Because a `MultiPayload`/`DefuseIntents` batch can carry an unbounded number of separate `TokenDiff` intents, an attacker can split one large negative delta of a Nep245/Imt token into `N` intents of `delta = -1`, each independently evaluated as `amount == 1` and thus fee-exempt, whereas a single `TokenDiff` with `delta = -N` would owe `Pips::fee_ceil(N)`.

### Finding Description
The intended binding is: `fees_collected_for_T_in_batch == Pips::fee_ceil(sum_of_negative_deltas_of_T_in_batch)`. The code instead evaluates fee per `TokenDiff` intent using only that intent's own `delta`: [1](#0-0) 

and the "no fee" carve-out is keyed off that single intent's `amount`: [2](#0-1) 

`Nep245`/`Imt` `TokenId`s can represent fungible-like multi-token balances (not just true 1-of-1 NFTs), so the "don't fee NFTs/MTs with |delta|<=1" heuristic is not safe when the same token can be traded in bulk. An attacker constructs one `DefuseIntents` message containing `N` `TokenDiff` intents, each `{diff: {T: -1, X: +k}}` (or matched against a counterparty's positive-`T` `TokenDiff`s), instead of one `TokenDiff` `{T: -N, X: +N*k}`. `Engine::execute_signed_intent` calls `DefuseIntents::execute_intent`, which iterates every intent and calls `TokenDiff::execute_intent` for each: [3](#0-2)  — each call independently computes `amount=1` and credits `Pips::ZERO` to `fees_collected`, which is only credited to `fee_collector` per-intent via `internal_add_balance` [4](#0-3) . Nothing aggregates deltas of `T` across intents before computing the fee.

The batch-level invariant enforced at `Engine::finalize`/`TransferMatcher::finalize` [5](#0-4)  only checks that deposits and withdrawals of each token net to zero across the whole execution (all `MultiPayload`s in the call); it does not, and structurally cannot, prevent or detect fee under-collection, since fee accounting is a side effect computed before/independently of the matching step. So as long as the attacker (alone, or with a cooperating counterparty) supplies matching positive deltas of `T` totaling `N` somewhere in the batch (satisfying `TransferMatcher::finalize`), the whole batch settles successfully while the aggregate fee collected for token `T` is `0` instead of `fee_ceil(N)`.

### Impact Explanation
This under-collects protocol fees owed to `fee_collector` on Nep245/Imt trades, letting any unprivileged signer (or two colluding signers acting as trade counterparties) capture, per trade, the fee that would otherwise accrue to the protocol. This is systematic and fully repeatable: any Nep245/Imt swap of arbitrary size can be chunked into unit-size `TokenDiff` intents (bounded only by intents-per-payload/gas limits) to always land in the `amount<=1` zero-fee branch, at zero marginal cost beyond constructing more intents in the same signed payload. This matches the rubric's Critical category "protocol fees bypassed ... " since it is a direct, repeatable underpayment of fees that would otherwise be owed to `fee_collector`.

### Likelihood Explanation
No special role or privilege is required — a normal signer can author a single `DefuseIntents` message with `N` `TokenDiff` intents (or arrange a counterparty transaction) and sign it once. Cost is purely additional intents in the payload (limited by gas/intents-per-payload constraints, which are out of scope but do not prevent moderate `N`), so this is practical and cheap to execute for any attacker wanting to avoid MT/IMT trading fees.

### Recommendation
Aggregate negative deltas per `token_id` across all `TokenDiff` intents (and ideally across the whole batch/signer) before evaluating `token_fee`'s `amount<=1` exemption, or remove/restrict the exemption to token types that are provably always minted/transferred in units of exactly 1 (e.g., only `Nep171`), rather than `Nep245`/`Imt` which can represent fungible quantities.

### Proof of Concept
`cargo test` plan (rust, in `contracts/defuse/core/src/intents/token_diff.rs` test module or an integration test using the sandbox harness in `tests/src/tests/defuse/intents/token_diff.rs`):
1. Construct a Nep245 `TokenId` `T` and a non-zero protocol fee `Pips` (e.g. `Pips::ONE_PERCENT`).
2. Build a single `DefuseIntents` containing `N=50` `TokenDiff` intents, each `{diff: {T: -1, X: +1}}`, matched by a counterparty `TokenDiff` providing `{T: +50, X: -50}` so `TransferMatcher::finalize` succeeds.
3. Execute via `Engine::execute_signed_intents` (or `env.defuse_execute_intents` in sandbox) and sum `fees_collected` for `T` across all emitted `TokenDiffEvent`s — assert total is `0`.
4. In a second run, execute a single `TokenDiff` `{diff: {T: -50, X: +50}}` matched analogously, and assert `fees_collected` for `T` equals `Pips::ONE_PERCENT.fee_ceil(50) > 0`.
5. Assert the two totals differ, demonstrating that splitting into unit-delta intents bypasses the fee that the equivalent single-diff trade would incur.

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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L96-101)
```rust
        // deposit fees to collector
        if !fees_collected.is_empty() {
            engine
                .state
                .internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)?;
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

**File:** contracts/defuse/core/src/intents/mod.rs (L97-112)
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
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L265-283)
```rust
    // Finalizes all transfers, or returns unmatched deltas.
    // If unmatched deltas overflow, then Err(None) is returned.
    pub fn finalize(self) -> Result<Transfers, InvariantViolated> {
        let mut transfers = Transfers::default();
        let mut deltas = TokenDeltas::default();
        for (token_id, transfer_matcher) in self.0 {
            if let Err(unmatched) = transfer_matcher.finalize_into(&token_id, &mut transfers)
                && (unmatched == 0 || deltas.apply_delta(token_id, unmatched).is_none())
            {
                return Err(InvariantViolated::Overflow);
            }
        }
        if !deltas.is_empty() {
            return Err(InvariantViolated::UnmatchedDeltas {
                unmatched_deltas: deltas,
            });
        }
        Ok(transfers)
    }
```
