### Title
Protocol fee bypass on Nep245/Imt tokens via splitting a single trade into many unit-delta `TokenDiff` intents - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::execute_intent` computes the fee for each `TokenDiff` intent independently, using only that intent's own delta magnitude as input to `TokenDiff::token_fee`. Because `token_fee` returns `Pips::ZERO` for `TokenIdType::Nep245`/`Imt` whenever the per-intent `amount <= 1`, an attacker can split what would otherwise be a single large negative delta on an MT/IMT token into many separate `TokenDiff` intents each with `|delta| == 1`, batched together in one `MultiPayload` set (or across colluding accounts), and pay zero fee on the aggregate amount even though a single `TokenDiff` moving the same aggregate amount would be charged `Pips::fee_ceil`.

### Finding Description
The broken binding: `fee_collector` balance credit for token T after executing a batch == `Pips::fee_ceil(aggregate_negative_delta_for_T)`. In reality, the code computes and credits fee per intent, not per aggregate: [1](#0-0) 

computes, for each `(token_id, delta)` pair inside *one* `TokenDiff`, `let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);` where `amount = delta.unsigned_abs()` — this is scoped strictly to that single intent's own delta, never to the sum of deltas on T across other intents in the same `MultiPayload`/batch.

`TokenDiff::token_fee` deliberately zeroes the fee for `Nep245`/`Imt` when the single-intent amount is `<= 1`: [2](#0-1) 

Root cause: this exemption was designed for the case of trading a single non-fungible-like MT edition (quantity 1), but `Nep245`/`Imt` token ids can also represent fungible-like quantities, and nothing prevents an attacker from decomposing a bulk transfer of N units of the same `TokenId` into N separate `TokenDiff` intents (potentially across two colluding accounts A and B, or a single account signing N intents with different nonces) each carrying `delta = -1`. Each such intent is fee-exempt individually. The engine only enforces a *balance* invariant across the whole batch via `TransferMatcher`/`Deltas::finalize` — it nets deltas per `TokenId` across all accounts in the batch (see `contracts/defuse/core/src/engine/state/deltas.rs`, `TransferMatcher::finalize`) but performs no aggregation for the *fee* calculation, which is done strictly inside each `TokenDiff::execute_intent` call, one at a time, in `contracts/defuse/core/src/engine/mod.rs execute_signed_intent` (line 79: `intents.execute_intent(&signer_id, self, hash)?;`).

None of the existing guards (`MultiPayload::verify`, nonce/salt checks, `TransferMatcher::finalize`'s balance invariant) address this because they only ensure signatures/nonces are valid and that token balances net to zero across the batch — they say nothing about fee amounts, which are computed and credited to `fee_collector` per-intent before finalize is even reached.

Attacker's exact payload: N separate `TokenDiff` intents (potentially split between two attacker-controlled accounts A and B, or a single colluding pair with a counterparty absorbing the matched legs) each with `diff = { T: -1, U: +k }` for some other token U, signed and submitted together in one `execute_intents(signed: Vec<MultiPayload>)` call. Each intent triggers `token_fee(T, amount=1, fee) == Pips::ZERO`, so `fees_collected` for that intent is empty, and no balance is ever added to `fee_collector` for T, while the same aggregate -N trade done as a single `TokenDiff` (`delta = -N`, N>1) would trigger the normal `fee` and a non-zero `fee_ceil(N)` credited to `fee_collector`.

### Impact Explanation
This is a protocol fee bypass affecting `fee_collector`'s expected revenue on any `Nep245`(MT) or `Imt` token id, for any volume, by simply restructuring the same economic trade as a sequence of unit legs. It is fully repeatable across accounts, tokens, and batches, and costs the attacker nothing beyond extra transaction/gas overhead for additional intents in the same call. This falls under the "protocol fees bypassed" Critical impact category defined in the rules, since value that should go to `fee_collector` never materializes while the underlying trade still executes to completion. Nep141 tokens are unaffected since `token_fee` always charges fee for `TokenIdType::Nep141` regardless of amount.

### Likelihood Explanation
Preconditions are minimal and available to any unprivileged signer: hold balances of an MT/IMT token in the Verifier (attacker can mint/deposit their own MT contract's tokens), and a non-zero `self.fees.fee`. No special roles, relayer keys, or DAO permissions are required — only the ability to sign `DefusePayload`s and call `execute_intents`/`simulate_intents`, both of which are explicitly in the attacker's capability set. The only friction is needing counterparties (or self-counterparties across two attacker-owned accounts) so that `TransferMatcher::finalize`'s per-token net-zero invariant is satisfied across the whole batch — this is a standard requirement for any multi-intent trade under this design and is not a meaningful deterrent.

### Recommendation
Aggregate the fee-relevant "amount" per `TokenId` across the whole batch (or at least across all intents from the same `signer_id` in the same `MultiPayload`) before applying the `Nep245`/`Imt` `amount <= 1` exemption in `TokenDiff::token_fee`, rather than evaluating it per individual `TokenDiff` intent. Alternatively, remove or tighten the `amount <= 1` fee exemption for `Nep245`/`Imt` so it cannot be trivially defeated by unit-delta decomposition (e.g., base the exemption on whether the underlying MT id is a true 1-of-1 NFT-like token rather than on the per-intent delta magnitude).

### Proof of Concept
```
cargo test in near-workspaces sandbox:
1. Create Verifier with self.fees.fee = Pips::ONE_PERCENT (non-zero), fee_collector = C.
2. Deploy an MT contract, deposit token T into Verifier balances for accounts A and B
   such that A holds N units of T and some counterparty D holds N units of token U.
3. Baseline: have A sign ONE TokenDiff intent { T: -N, U: +closure } combined with D's
   matching TokenDiff, execute via execute_intents, assert
   mt_balance_of(C, T) == Pips::fee_ceil(N) (non-zero).
4. Exploit: reset state; have A and (optionally) B sign N separate TokenDiff intents
   each { T: -1, U: +closure_unit } (aggregate delta on T == -N), combined with
   matching counterpart TokenDiff intents from D providing U in exchange, batch all
   into one execute_intents(signed) call.
5. Assert mt_balance_of(C, T) == 0 after step 4, while the net T balance change for A/B
   (aggregate -N) and U balance change for D match the same aggregate trade as step 3.
6. Compare: fee_collector balance in step 3 (baseline, > 0) vs step 4 (exploit, == 0)
   for the same aggregate traded amount N, proving fee bypass.
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
