### Title
Protocol fees bypassed by splitting a large `Nep245`/`Imt` transfer into unit-sized `TokenDiff` intents - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` skips the protocol fee entirely whenever the per-intent delta magnitude for a `Nep245` or `Imt` token is `<= 1`, and `TokenDiff::execute_intent` computes and collects fees strictly per individual intent rather than on any aggregate transfer. An attacker who splits a large multi-token (MT) value transfer into N separate unit-delta `TokenDiff` intents (e.g. two self-controlled accounts each submitting N intents of `delta = -1` / `delta = +1` on the same `TokenId`) pays zero fee on the whole trade, whereas a single `TokenDiff` moving the same net amount would owe `fee_ceil(amount)`.

### Finding Description
The broken binding is: `fee_owed_on_net_transfer(N) == sum_of_fees_collected_over_N_unit_intents`. For a single `TokenDiff` with `delta = -N` on a `Nep245`/`Imt` token, `Self::token_fee(token_id, N, protocol_fee).fee_ceil(N)` is computed once with `amount = N > 1`, so the `Nep245 | Imt if amount > 1 => {}` branch falls through to return the nonzero `fee` [1](#0-0) . If instead the same net movement is expressed as N separate `TokenDiff` intents each with `delta = -1` (matched by counterparty `delta = +1` intents), every call to `token_fee` uses `amount = 1`, which falls into `TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO` [2](#0-1) , so `fee_ceil(1)` is `0` for every intent and the total fee collected is `0`.

The fee is computed and deposited independently inside each call to `execute_intent`, with no cross-intent aggregation of deltas on the same `TokenId`: `fees_collected` starts fresh per intent and is added to `fee_collector` immediately after that single intent [3](#0-2) . Because `TokenDiff.diff` is a `BTreeMap<TokenId, i128>`, a single intent can carry only one delta per `TokenId`, but nothing stops a signer from listing N separate `TokenDiff` intents (each with a distinct/unit delta on the same `TokenId`) inside the `intents: Vec<Intent>` of one signed `DefuseIntents` payload [4](#0-3) , all executed sequentially by `DefuseIntents::execute_intent` [5](#0-4) . Each individual `TokenDiff` is still balanced correctly against the invariant (net supply delta across the whole batch is zero, as verified by `Engine::finalize`) [6](#0-5) , so none of the existing guards (`verify`, `has_public_key`, `verify_intent_nonce`, nonce commit, invariant check) detect or prevent this, since they only enforce signature/nonce validity and zero-sum balance, not fee correctness across multiple intents on the same token.

Attacker payload: one or two signed `MultiPayload`s (self-trade across two attacker-controlled accounts, or with a colluding counterparty) whose `DefuseIntents.intents` contains N `TokenDiff` entries, each `{"nep245:mt.near:xyz": "-1"}` / `{"nep245:mt.near:xyz": "1"}`, executed via `execute_intents`/`simulate_intents`.

### Impact Explanation
The `fee_collector` account is under-credited: for a real net MT transfer of N units it receives `0` instead of `fee_ceil(N)` that a single `TokenDiff` of the same net amount would have paid. This is a direct, repeatable protocol-fee bypass on any `Nep245` (or `Imt`) token that is used with fungible-like (large-quantity) balances, matching the "protocol fees bypassed entirely" Critical category. Blast radius covers every MT token with `token_fee` reaching the `amount > 1` branch and scales with however large the attacker's MT holdings are; it is repeatable across accounts, tokens, and batches at no cost beyond ordinary transaction/gas fees.

### Likelihood Explanation
The attacker needs no special role — only control of two of their own Verifier accounts (or a colluding counterparty) and MT balance to trade, both attacker-controlled preconditions explicitly allowed by scope. Constructing N unit `TokenDiff` intents inside one or two signed payloads is straightforward (no additional signatures needed beyond the two payload signers), making this cheap and fully feasible, limited only by per-transaction gas/intent-count practicalities which are excluded from scope but don't prevent moderate-N exploitation.

### Recommendation
Base the `Nep245`/`Imt` fee-skip threshold on the actual per-`TokenId` supply/semantics (e.g., only skip fee if the token's total supply for that `TokenId` is provably `1`, i.e., truly NFT-like), or aggregate deltas per `TokenId` across all `TokenDiff` intents in a batch (and ideally per signer across a payload) before evaluating the `amount > 1` condition, so an attacker cannot subdivide a large trade to repeatedly hit the zero-fee path.

### Proof of Concept
`cargo test` (in `contracts/defuse/core` or `tests/`) that:
1. Deploys/uses a `Nep245` MT token and gives attacker account `A` a balance of `N` (`N` large, e.g. 1000) of `TokenId` `T`, and account `B` sufficient balance in the counter-token.
2. Path 1 (baseline): `A` and `B` each sign one `TokenDiff` intent with `delta = -N` / `delta = +N` (± counter-leg) on `T`; execute; assert `fee_collector` balance for `T` equals `Pips::fee_ceil` of `N` under configured `protocol_fee` (nonzero).
3. Path 2 (exploit): `A` and `B` each sign a payload containing N separate `TokenDiff` intents with `delta = -1` / `delta = +1` on `T` (paired to balance the invariant); execute; assert final MT balances of `A`/`B` match Path 1 (same net transfer of `N` units) but `fee_collector` balance for `T` is `0`.
4. Assert the divergence: `fee_collector_path1_balance != fee_collector_path2_balance` for equal net token movement, proving the fee bypass.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L56-101)
```rust
        let protocol_fee = engine.state.fee();
        let mut fees_collected: Amounts = Amounts::default();

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
        }

        engine.inspector.on_event(DefuseEvent::TokenDiff(
            [MaybeIntentEvent::new_intent(
                AccountEvent::new(
                    signer_id,
                    TokenDiffEvent {
                        diff: Cow::Borrowed(&self),
                        fees_collected: fees_collected.clone(),
                    },
                ),
                intent_hash,
            )]
            .as_slice()
            .into(),
        ));

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

**File:** contracts/defuse/core/src/intents/mod.rs (L28-37)
```rust
#[cfg_attr(feature = "schemars-v0_8", derive(::schemars::JsonSchema))]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DefuseIntents {
    /// Sequence of intents to execute in given order. Empty list is also
    /// a valid sequence, i.e. it doesn't do anything, but still invalidates
    /// the `nonce` for the signer
    /// WARNING: Promises created by different intents are executed concurrently and does not rely on the order of the intents in this structure
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub intents: Vec<Intent>,
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

**File:** contracts/defuse/core/src/engine/mod.rs (L113-118)
```rust
    #[inline]
    fn finalize(self) -> Result<Transfers> {
        self.state
            .finalize()
            .map_err(DefuseError::InvariantViolated)
    }
```
