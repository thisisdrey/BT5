## Analysis

The binding claimed: `fee_collector_credit(T) == Pips::fee_ceil(fee, |Σ negative deltas of T across the batch|)`.

Actual code computes the fee **per `TokenDiff` intent, per token key inside that single intent's diff map**, not aggregated across the batch or even across the signer's other intents:

```rust
// contracts/defuse/core/src/intents/token_diff.rs:59-78
for (token_id, delta) in &self.diff {
    ...
    if *delta < 0 {
        let amount = delta.unsigned_abs();
        let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
        fees_collected.add(token_id.clone(), fee).ok_or(DefuseError::BalanceOverflow)?;
    }
}
``` [1](#0-0) 

and the dust exemption:
```rust
pub fn token_fee(token_id: impl Into<TokenIdType>, amount: u128, fee: Pips) -> Pips {
    let token_id = token_id.into();
    match token_id {
        TokenIdType::Nep141 => {}
        TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
        TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
    }
    fee
}
``` [2](#0-1) 

`amount` here is the magnitude of the delta contained in *that single intent instance*, evaluated independently for every `TokenDiff` intent as `DefuseIntents::execute_intent` iterates `self.intents` [3](#0-2)  and as `Engine::execute_signed_intents` iterates every `MultiPayload` in the batch [4](#0-3) . Nothing pre-aggregates a signer's deltas per `TokenId` before this fee computation runs. The only place deltas from multiple intents/accounts get combined is `TransferMatcher`/`Deltas::finalize`, and that machinery nets deposits vs. withdrawals purely to compute inter-account transfers/invariant checks — it has no bearing on, and runs strictly after, the fee amount already fixed inside `execute_intent`. [5](#0-4) 

So the attack works exactly as described: submit 1000 `TokenDiff` intents (in one or many signed `MultiPayload`s inside a single `execute_intents` call) each with `diff = {NEP-245_token_id: -1}` instead of one intent with `diff = {NEP-245_token_id: -1000}`. Each leg independently hits `amount = 1 <= 1` → `Pips::ZERO` → zero fee, while a single `-1000` intent would hit `amount = 1000 > 1` and pay `fee.fee_ceil(1000)`. The signer's own balance is debited the same total 1000 either way (`internal_apply_deltas`), so this is not a "money out of nowhere" bug for the *signer* — but the fee owed to `fee_collector` (`Pips::fee_ceil`) is entirely bypassed for a batch that would otherwise incur it, i.e. it under-collects the protocol fee for the token actually moved. The saved fee benefits whichever counterparty in the batch would otherwise have had to top up the shortfall for `TransferMatcher::finalize` to net to zero (e.g., the attacker's own second account acting as counterparty in the swap), meaning value that should have gone to `fee_collector` stays with the attacker's counterparty account instead.

None of the listed guards (`verify`, `has_public_key`, `verify_intent_nonce`, nonce/salt checks, `Lock`, `assert_one_yocto`, access-control roles, checked-arithmetic) address this, since it is not an authentication/replay/overflow bug — it's a fee-computation granularity bug: `token_fee`'s dust exemption for NEP-245/IMT tokens is evaluated per-intent rather than per aggregate delta.

### Title
Protocol fee bypass via splitting NEP-245 `TokenDiff` deltas into unit legs - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::execute_intent` computes the protocol fee independently for each `TokenDiff` intent using `Self::token_fee(token_id, amount, protocol_fee)`, where `amount` is only the magnitude of that single intent's delta on that token. Because NEP-245/IMT tokens are fee-exempt when `amount <= 1`, an attacker can split one large negative delta into many `delta = -1` legs on the same token id within a single batch, driving the aggregate collected fee to zero.

### Finding Description
Broken binding: `fee_collector_credit(T)` should equal `Pips::fee_ceil(fee, |Σ negative deltas of T|)` for the whole batch, but the code computes `Σ Pips::fee_ceil(fee, |delta_i|)` per intent, and `token_fee` zeroes the fee whenever a single leg's `|delta_i| <= 1` for NEP-245/IMT tokens (`contracts/defuse/core/src/intents/token_diff.rs:206-216`). `execute_intent` (lines 59-78) applies this per-intent, per-token-in-diff, and neither `DefuseIntents::execute_intent` (`contracts/defuse/core/src/intents/mod.rs:97-113`) nor `Engine::execute_signed_intents` (`contracts/defuse/core/src/engine/mod.rs:32-40`) aggregate deltas across intents/payloads before fee assessment. The attacker's payload is simply `N` `TokenDiff` intents (or `N` separately signed `MultiPayload`s in one `execute_intents([...])` call), each with `diff = {nep245_token_id: -1}`, versus one intent with `diff = {nep245_token_id: -N}`. `TransferMatcher`/`Deltas::finalize` only nets deposits/withdrawals for settlement after fees are already fixed, so it cannot recover the missed fee.

### Impact Explanation
`fee_collector` under-collects protocol fees relative to the true value moved on a NEP-245 token, matching the listed Critical category "protocol fees bypassed." The shortfall benefits whichever account in the batch would otherwise have had to supply the fee-covering surplus deposit for the invariant to balance (e.g., an attacker-controlled counterparty account), so the attacker (using two of their own accounts) can execute large NEP-245 swaps essentially fee-free by chunking into 1-unit legs. This is repeatable for any amount and any NEP-245/IMT token id, limited only by batch size/gas.

### Likelihood Explanation
No privileged role is required — any unprivileged user can call `execute_intents`/`simulate_intents` with a `MultiPayload` batch containing arbitrarily many signed `TokenDiff` intents. The only cost is gas for more intents/signatures; the technique is straightforward and requires no special contract deployment. Feasibility is high given `execute_intents` accepts `Vec<MultiPayload>` and each `MultiPayload`'s `DefuseIntents.intents` accepts an arbitrary `Vec<Intent>`.

### Recommendation
Aggregate negative deltas per `(signer, token_id)` (or globally per `token_id` across the whole `MultiPayload`/`execute_intents` batch) before evaluating `token_fee`'s dust-exemption threshold and before calling `fee_ceil`, rather than evaluating the exemption per individual `TokenDiff` intent's local delta.

### Proof of Concept
`cargo test` (near-workspaces sandbox) plan:
1. Deploy Defuse with `fee = Pips::ONE_PERCENT` (or similar nonzero fee) and a configured `fee_collector`.
2. Create a NEP-245 token contract, deposit `1000` units of `token_id` to `user1` via `mt_on_transfer`.
3. Case A: `user1` signs one `TokenDiff{diff: {token_id: -1000, other_token: +X}}` (paired with a counterparty intent netting to zero); execute via `execute_intents`. Assert `fee_collector` balance for `token_id` == `Pips::ONE_PERCENT.fee_ceil(1000)` (nonzero).
4. Case B: `user1` signs 1000 `TokenDiff{diff: {token_id: -1}}` intents (packed in one or more `MultiPayload`s in a single `execute_intents([...])` call), matched by a counterparty providing the same aggregate `other_token: +X`; execute via `execute_intents`. Assert `fee_collector` balance for `token_id` == `0`.
5. Compare: both cases move the same net `1000` units of `token_id` out of `user1`'s balance, but `fee_collector` credit differs (`fee_ceil(1000)` vs `0`), demonstrating the bypass.

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L267-284)
```rust
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
}
```
