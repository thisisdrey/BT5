### Title
Splitting a `TokenDiff` delta into unit chunks on `Nep245`/`Imt` tokens bypasses `token_fee` and lets colluding accounts move the full balance fee-free - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` returns `Pips::ZERO` for `Nep245`/`Imt` token IDs whenever the per-intent `amount <= 1`, and `execute_intent` computes the fee independently for each `TokenDiff` intent in the batch rather than on the net delta a signer applies to a given `TokenId`. An attacker controlling two accounts (or one colluding counterparty) can split a `-1_000_000` delta into `1_000_000` separate `TokenDiff` intents of `delta = -1` each, matched in the same `execute_intents` batch by a single `+1_000_000` counterparty intent, so the zero-sum invariant is satisfied with `fees_collected = 0` instead of `Pips::fee_ceil(protocol_fee, 1_000_000)`.

### Finding Description
The broken binding: legitimate unsplit accounting requires `fees_collected[T] == Pips::fee_ceil(protocol_fee, 1_000_000)` for a `-1_000_000` delta on Nep245 token `T`; under the split attack `sum(fees_collected[T]) == 0` while the same `1_000_000` units of `T` still move fully from signer A to counterparty B.

Code path: in `TokenDiff::execute_intent` [1](#0-0) , each `(token_id, delta)` pair in a single intent's `diff` computes its own fee via `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`. `token_fee` special-cases NFTs/MTs: `TokenIdType::Nep245 | TokenIdType::Imt if amount > 1` charges the fee, but `amount <= 1` returns `Pips::ZERO` [2](#0-1) . This decision is made per-intent, not on the signer's aggregate exposure to that `TokenId` across the whole `MultiPayload` batch.

The only cross-intent check is the global zero-sum invariant enforced at the end of `execute_signed_intents` via `Engine::finalize` → `Deltas::finalize` → `TransferMatcher::finalize`, which only requires that raw deltas (plus fee credits) for each `TokenId` sum to zero across the entire batch, or the whole transaction reverts with `InvariantViolated::UnmatchedDeltas` [3](#0-2) [4](#0-3) . This is confirmed by the existing `invariant_violated` unit test, which shows a batch reverts entirely (balances unchanged) when the netted deltas across the whole batch (which is what includes any fee credit to `fee_collector`) do not sum to zero [5](#0-4) . Nothing in this check re-derives what the fee "should have been" for an unsplit trade; it only verifies that whatever fee actually got credited plus the parties' deltas net to zero.

Exploit: signer A holds `>= 1_000_000` of a Nep245 `TokenId`. A signs `1_000_000` `TokenDiff` intents, each `{diff: {T: -1}}`, in one or more `MultiPayload`s included in the same `execute_intents(signed)` call. Each such intent invokes `token_fee(T, 1, protocol_fee)`, which returns `Pips::ZERO` because `amount <= 1`, so `fees_collected` per intent is `0`; the loop in `execute_intent` sums these across the `1_000_000` calls to `0`. A colluding second account B (which can also be controlled by the attacker) signs one `TokenDiff` `{diff: {T: +1_000_000}}`. The batch nets to zero for token `T` (`-1_000_000 + 1_000_000 + 0 fee-credit = 0`), so `TransferMatcher::finalize` succeeds and the whole batch commits: A loses `1_000_000` of `T`, B gains the full `1_000_000` of `T`, and `fee_collector` gains nothing. Had A instead sent one un-split `TokenDiff{T: -1_000_000}`, `token_fee` would hit the `amount > 1` branch and charge `protocol_fee`, forcing `fee_collected[T] = Pips::fee_ceil(protocol_fee, 1_000_000) > 0` to be credited to the fee collector, and B's counterparty delta would have had to be computed via `TokenDiff::closure_delta` (which subtracts the fee) to keep the batch balanced [6](#0-5) , so B would receive strictly less than `1_000_000`.

No existing guard prevents this: `MultiPayload::verify`, nonce/salt checks, and `#[pause]` only gate authentication/replay, not per-`TokenId` fee aggregation; the invariant check in `TransferMatcher::finalize` is agnostic to whether the zero-fee outcome resulted from a legitimate NFT/MT single-unit transfer or from an artificially split large trade.

### Impact Explanation
The `fee_collector` is fully bypassed for arbitrary-sized Nep245/Imt token transfers: any amount can be moved between two colluding (or one self-controlled two-account) parties at zero protocol fee simply by chunking the same net delta into unit-sized `TokenDiff` intents within one `execute_intents` call. This is repeatable per token, per batch, with no upper bound other than gas/intent-count limits, and directly matches the "protocol fees bypassed" Critical category — value (the fee) that should go to the fee collector never leaves the trading pair's control.

### Likelihood Explanation
Preconditions are minimal and fully within an unprivileged attacker's control: two accounts (can be the attacker's own), sufficient balance of a Nep245/Imt token in one account, and the ability to sign and submit `MultiPayload`s to `execute_intents`/`simulate_intents`. No relayer key, DAO role, or victim key is required. The only cost is the extra intents (and the corresponding NEAR gas/tx size for `1_000_000` intents in one call, or fewer batches with fewer split units if `amount > 1` threshold only requires values of 1 - though the same trick works at smaller scale, e.g. splitting into chunks of exactly 1 each time fee should apply).

### Recommendation
Compute `token_fee` based on the signer's net aggregate delta per `TokenId` across the entire batch/payload set (or disallow multiple `TokenDiff` intents from the same signer touching the same `TokenId` within one `execute_intents` call), rather than evaluating the `amount <= 1` fee-exemption independently per intent.

### Proof of Concept
`cargo test` plan (sandbox, `tests/src/tests/defuse/intents/token_diff.rs`):
1. Deploy defuse with `protocol_fee = Pips::ONE_PERCENT` (or any `> 0`), an Nep245 token contract, accounts A and B.
2. Deposit `1_000_000` of token `T` (Nep245) to A.
3. Case 1 (unsplit): sign `TokenDiff{T: -1_000_000}` for A and `TokenDiff{T: closure_delta(T, -1_000_000, fee)}` for B; execute; assert `fee_collector` balance for `T` equals `Pips::fee_ceil(protocol_fee, 1_000_000)` (> 0) and B's balance for `T` equals `closure_delta` value (< 1_000_000).
4. Case 2 (split): sign `1_000_000` `TokenDiff{T: -1}` intents for A and one `TokenDiff{T: 1_000_000}` for B, all included in one `execute_intents(signed)` call; execute; assert `fee_collector` balance for `T` remains `0` and B's balance for `T` equals the full `1_000_000`.
5. Assert the two cases diverge: `fee_collector_balance_case1 > 0 == fee_collector_balance_case2`, proving the FEES binding fails for the split case while the unsplit case correctly charges `Pips::fee_ceil(protocol_fee, 1_000_000)`.

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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L159-165)
```rust
    /// Returns closure for delta that should be given in a single
    /// [`TokenDiff`] to successfully execute [`TokenDiff`] with given
    /// `delta` on the same token assuming given `fee`.
    #[inline]
    pub fn closure_delta(token_id: &TokenId, delta: i128, fee: Pips) -> Option<i128> {
        Self::closure_supply_delta(token_id, Self::supply_delta(token_id, delta, fee)?, fee)
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

**File:** contracts/defuse/core/src/engine/mod.rs (L113-118)
```rust
    #[inline]
    fn finalize(self) -> Result<Transfers> {
        self.state
            .finalize()
            .map_err(DefuseError::InvariantViolated)
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

**File:** tests/src/tests/defuse/intents/token_diff.rs (L277-373)
```rust
#[rstest]
#[tokio::test]
async fn invariant_violated(#[future(awt)] env: Env) {
    let (user1, user2, ft1, ft2) = futures::join!(
        env.create_user(),
        env.create_user(),
        env.create_token(),
        env.create_token(),
    );

    let ft1_token_id = TokenId::from(Nep141TokenId::new(ft1.contract_id().clone()));
    let ft2_token_id = TokenId::from(Nep141TokenId::new(ft2.contract_id().clone()));

    env.initial_ft_storage_deposit(
        vec![user1.account_id(), user2.account_id()],
        vec![ft1.contract_id(), ft2.contract_id()],
    )
    .await;

    // deposit
    futures::try_join!(
        env.defuse_ft_deposit_to(ft1.contract_id(), 1000, user1.account_id(), None),
        env.defuse_ft_deposit_to(ft2.contract_id(), 2000, user2.account_id(), None)
    )
    .expect("Failed to deposit tokens");

    let signed = try_join_all([
        user1.sign_defuse_payload_default(
            &env.defuse,
            [TokenDiff {
                diff: TokenDeltas::default()
                    .with_apply_deltas([
                        (ft1_token_id.clone(), -1000),
                        (ft2_token_id.clone(), 2000),
                    ])
                    .unwrap(),
                memo: None,
                referral: None,
            }],
        ),
        user1.sign_defuse_payload_default(
            &env.defuse,
            [TokenDiff {
                diff: TokenDeltas::default()
                    .with_apply_deltas([
                        (ft1_token_id.clone(), 1000),
                        (ft2_token_id.clone(), -1999),
                    ])
                    .unwrap(),
                memo: None,
                referral: None,
            }],
        ),
    ])
    .await
    .unwrap();

    assert_eq!(
        env.defuse
            .simulate_intents(MultiPayloadArgs { signed: &signed })
            .await
            .unwrap()
            .invariant_violated
            .unwrap()
            .into_unmatched_deltas(),
        Some(TokenDeltas::new(
            std::iter::once((ft2_token_id.clone(), 1)).collect()
        ))
    );

    env.defuse_execute_intents(env.defuse.contract_id(), signed)
        .await
        .unwrap_err();

    // balances should stay the same
    assert_eq!(
        env.contract::<Mt>(env.defuse.contract_id())
            .mt_batch_balance_of(MtBatchBalanceOfArgs {
                account_id: user1.account_id(),
                token_ids: &[ft1_token_id.to_string(), ft2_token_id.to_string()],
            })
            .await
            .unwrap(),
        [U128(1000), U128(0)]
    );

    assert_eq!(
        env.contract::<Mt>(env.defuse.contract_id())
            .mt_batch_balance_of(MtBatchBalanceOfArgs {
                account_id: user2.account_id(),
                token_ids: &[ft1_token_id.to_string(), ft2_token_id.to_string()],
            })
            .await
            .unwrap(),
        [U128(0), U128(2000)]
    );
}
```
