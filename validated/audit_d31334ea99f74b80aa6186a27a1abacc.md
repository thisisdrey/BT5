Confirmed: `DefuseIntents.intents` is a `Vec<Intent>` [1](#0-0) , and `DefuseIntents::execute_intent` iterates them, dispatching each `TokenDiff` independently within one signature-verified `execute_signed_intent` call [2](#0-1) . This confirms the exploit path is reachable within a single `MultiPayload`/signature.

### Title
Fee bypass via leg-splitting of NEP-245 `TokenDiff` intents - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::execute_intent` computes the protocol fee independently per intent using `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`, where `amount` is that single intent's `delta.unsigned_abs()`. For `TokenId::Nep245`/`Imt`, `token_fee` returns `Pips::ZERO` whenever `amount <= 1`. An unprivileged signer can therefore split one logical trade of `N` units of a NEP-245 token into `N` separate `TokenDiff` intents (each `delta == -1`) inside one `Vec<Intent>` of a single signed `MultiPayload`, causing zero fee to be collected on every leg, while an equivalent single `TokenDiff` with `delta = -N` would incur a nonzero `Pips::fee_ceil(fee, N)`.

### Finding Description
The broken binding: `sum(fees_collected credited to fee_collector for token T across the call)` should equal `Pips::fee_ceil(protocol_fee, N)` where `N` is the net negative delta of `T` for the signer across the call, but in the exploit the left side is `0` while the right side is nonzero when `protocol_fee > 0` and `N > 1`.

Root cause is in `TokenDiff::execute_intent`: [3](#0-2) 
Fee is computed per-`TokenDiff` intent, not aggregated per signer/token across the whole `DefuseIntents` list or across the whole `MultiPayload` batch. The exemption logic is: [4](#0-3) 
which zeroes the fee for `Nep245`/`Imt` whenever the single intent's `|delta| <= 1`.

Exploit flow: the attacker (as one party to a NEP-245 trade of size `N`) signs one `MultiPayload` whose `DefuseIntents.intents` field contains `N` `TokenDiff` intents, each with `diff = {T: -1, U: +m}` (the counterparty side supplies matching `+1`/`-m` legs, either in the same batch or their own signed payloads passed in the same `execute_intents` call). Each leg is validated as a `TokenId::Nep245` `TokenDiff` with `amount == 1`, so `token_fee` returns `Pips::ZERO` on every leg, and `fees_collected` for token `T` is `0` on each of the `N` calls to `internal_add_balance(fee_collector, ...)`.

Existing guards do not prevent this:
- `MultiPayload::verify`, nonce/salt checks, and `assert_one_yocto`/`#[pause]` guard signature and replay validity, not fee aggregation.
- The batch-level invariant enforced by `TransferMatcher::finalize` in `contracts/defuse/core/src/engine/state/deltas.rs` only checks that deposits and withdrawals of each token net to zero across the whole batch (`InvariantViolated::UnmatchedDeltas`) [5](#0-4) ; it does not recompute or validate fees, so a batch that nets to zero via 1-unit legs passes the invariant while collecting zero total fee.
- There is no code path that sums same-signer, same-token negative deltas across multiple `TokenDiff` intents (or across multiple `MultiPayload`s in the `Vec<MultiPayload>` passed to `execute_intents`) before calling `token_fee`.

### Impact Explanation
`fee_collector` permanently loses protocol-fee revenue on any NEP-245 (or `imt`-feature) trade that can be decomposed into unit-sized legs, matching the explicitly listed Critical impact category "protocol fees bypassed or over-collected." This is repeatable on every trade of a NEP-245 asset with amount > 1, for any pair of counterparties, with no bound other than the number of intents the attacker is willing to pack into a batch (gas/size limits aside, which are out of scope). No user funds are stolen from other accounts; the loss is solely foregone protocol fee revenue, but this is exactly the impact category called out as Critical in the rules.

### Likelihood Explanation
Requires only two unprivileged accounts (or a self-trade construction) capable of holding NEP-245 balances and signing `DefusePayload`s — no privileged role, relayer key, or upgrade access is needed. The attacker cost is simply constructing `N` `TokenDiff` intents instead of 1 inside the `intents` vector of a `MultiPayload` (or across a `Vec<MultiPayload>` submitted together to `execute_intents`), and it works every time `protocol_fee > 0`. Feasibility is high and fully deterministic; it is not probabilistic and does not depend on network conditions.

### Recommendation
Aggregate negative deltas per `(signer_id, token_id)` across all `TokenDiff` intents within a `DefuseIntents` execution (and ideally across the whole `execute_signed_intents` batch) before applying `token_fee`/`fee_ceil`, rather than evaluating the `amount <= 1` exemption on each individual intent's `delta` in isolation. Alternatively, remove or tighten the `amount <= 1` fee exemption for `Nep245`/`Imt` tokens so it only applies to genuinely non-fungible (unique, non-decomposable) transfers, not to fungible-balance-style multi-token assets that can be freely split into unit legs.

### Proof of Concept
`cargo test` plan (add to `contracts/defuse/core/src/intents/token_diff.rs` or `tests/src/tests/defuse/intents/token_diff.rs`, using the existing `Env` harness with `Pips::ONE_PERCENT` fee and a NEP-245/MT token):
1. Set up two users, a NEP-245 token `T`, and a NEP-141 (or second NEP-245) token `U`; deposit balances so `user1` holds `N` units of `T` (e.g., `N = 100`) and `user2` holds enough of `U`.
2. Case A (aggregate): sign a single `MultiPayload` for `user1` with one `TokenDiff{diff: {T: -100, U: +closure}}` and a matching `user2` `TokenDiff`; execute via `execute_intents`; assert `fees_collected` for `T` (from `TokenDiffEvent`) equals `Pips::ONE_PERCENT.fee_ceil(100)` (nonzero).
3. Case B (split): sign a single `MultiPayload` for `user1` whose `DefuseIntents.intents` contains 100 `TokenDiff` intents, each `{T: -1, U: +closure_of(-1)}`, matched by corresponding `user2` legs; execute via `execute_intents`; assert `fees_collected` for `T` across all 100 emitted `TokenDiffEvent`s sums to `0`.
4. Assert `0 != Pips::ONE_PERCENT.fee_ceil(100)` from step 2, demonstrating the binding violation, and confirm both cases pass `TransferMatcher::finalize` (no `InvariantViolated`) so both are valid, settled batches under `execute_intents`.

### Citations

**File:** contracts/defuse/core/src/intents/mod.rs (L30-37)
```rust
pub struct DefuseIntents {
    /// Sequence of intents to execute in given order. Empty list is also
    /// a valid sequence, i.e. it doesn't do anything, but still invalidates
    /// the `nonce` for the signer
    /// WARNING: Promises created by different intents are executed concurrently and does not rely on the order of the intents in this structure
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub intents: Vec<Intent>,
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
