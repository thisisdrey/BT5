### Title
Protocol fee bypass on NEP-245/IMT `TokenDiff` intents via leg-splitting exploiting the `amount <= 1` zero-fee carve-out — (`contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` waives the fee entirely for `Nep245`/`Imt` tokens whenever the *single intent's* `|delta|` is `<= 1`, but the fee is computed independently for every `TokenDiff` intent rather than on the netted, aggregate delta of that token across the batch. An attacker can therefore split one large trade (`delta = -1000`) into 1000 signed `TokenDiff` intents each with `delta = -1` on the same NEP-245 `token_id`, each qualifying for the zero-fee branch, while the `TransferMatcher` invariant only requires deposits/withdrawals of the token to net to zero across the whole batch — it does not re-derive or enforce the fee that would have applied to the aggregated amount.

### Finding Description
The broken binding, as stated by the question, is:

`fees_credited_to_fee_collector(T) == Pips::fee_ceil(fee, |Σ negative_delta(T) across batch|)`

In reality the code computes:

`fees_credited_to_fee_collector(T) == Σ over each TokenDiff intent i: Pips::fee_ceil(fee, |delta_i(T)|) when |delta_i(T)| taken alone`

which is **not** equal to `Pips::fee_ceil(fee, |Σ delta_i(T)|)` when `T` is NEP-245/IMT and the intents are split so each leg has `|delta_i| <= 1`.

Code path:
- `TokenDiff::execute_intent` iterates the diff of a *single* intent and, for each negative delta, computes `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` where `amount = delta.unsigned_abs()` is the per-intent magnitude, not any batch-wide aggregate: [1](#0-0) 
- `TokenDiff::token_fee` explicitly zeroes the fee for `Nep245`/`Imt` when `amount <= 1`: [2](#0-1) 
- `DefuseIntents::execute_intent` simply loops over every intent in the payload and calls `execute_intent` on each independently — there is no aggregation of deltas per token before fee computation: [3](#0-2) 
- `Engine::execute_signed_intents` processes every signed payload in the batch the same way, then only calls `finalize()` at the end: [4](#0-3) 
- The only batch-wide check is `TransferMatcher::finalize`, which requires that, per token, total withdrawals equal total deposits (including whatever fee deposits were made to `fee_collector`) — it does not know or enforce what the fee *should* have been for the netted amount, it just requires deposits == withdrawals: [5](#0-4) 

Because the fee is a deposit credited to `fee_collector` and is added to the same `TransferMatcher` ledger that must balance, an attacker (or attacker + colluding/self-controlled counterparty account) can pre-compute, off-chain, a set of 1000 `TokenDiff` legs of `delta = -1` each (instead of one `delta = -1000` intent) and a matching set of `+1`-legs elsewhere in the batch so the ledger balances to zero — all without ever paying the `fee_ceil(fee, 1000)` that a single `delta = -1000` intent would incur, since each leg's own `token_fee` call sees `amount == 1` and returns `Pips::ZERO`.

Existing guards do not stop this:
- `MultiPayload::verify`, nonce/signature checks, and `#[pause]` guard authenticity/replay, not fee correctness.
- `TransferMatcher::finalize` only enforces net-zero movement of tokens system-wide; it has no concept of "what fee should have applied to this netted amount" and is agnostic to how many intents produced the deltas — confirmed by the existing `invariant_violated` test which shows finalize checks only exact numeric matching of deposits vs withdrawals, not fee correctness: [6](#0-5) 
- The existing `swap_many`/`swap_p2p` tests validate the correct fee-aware `closure_delta` math for a *single* intent's magnitude, but there is no test or code path re-checking fee correctness at the netted, per-token, per-batch level: [7](#0-6) 

### Impact Explanation
This is a protocol-fee bypass: the fee_collector under-collects revenue proportional to the size of the trade being split, while the full economic transfer (up to `1000` units of the NEP-245/IMT token in the example) still occurs between the parties in the batch. This matches the Critical category "protocol fees bypassed or over-collected" from the rules. It is fully repeatable: any two cooperating signers (or a single attacker controlling two of their own accounts) can apply this to any NEP-245 or IMT `token_id`, any number of times, for any aggregate amount, by simply choosing to submit `N` legs of `delta = ±1` instead of one leg of `delta = ±N`. The blast radius is every NEP-245/IMT token traded through `TokenDiff`, and the loss is borne entirely by the protocol's fee_collector, not by any individual user's authorized signed amount.

### Likelihood Explanation
Preconditions are minimal and entirely within an unprivileged attacker's control: they need only sign multiple small `TokenDiff` intents instead of one large one and submit them together (or across multiple `execute_intents`/`simulate_intents` calls that eventually settle in the same or coordinated batches) — no special role, no relayer key, no victim key. The cost is purely gas/tx-size for `N` intents instead of `1`, which for typical trade sizes is a modest linear overhead, and NEP-245/IMT tokens are a first-class supported token type in this contract, not a corner case. This is straightforward and highly likely to be discovered/exploited by any protocol fee-conscious market maker or solver optimizing for lowest fees.

### Recommendation
Compute and enforce the NEP-245/IMT zero-fee carve-out (and the fee itself) against the *aggregated* negative delta per `(signer, token_id)` — or better, per `token_id` across the whole batch as tracked in `TransferMatcher` — rather than per individual `TokenDiff` intent. Concretely, accumulate per-token negative deltas across all intents belonging to the same batch/signer before calling `token_fee`/`fee_ceil`, and only apply the `amount <= 1` exemption if the aggregated magnitude (not the single intent's) is `<= 1`. Alternatively, remove or tighten the `Nep245`/`Imt` `amount <= 1` special case so it cannot be abused to launder fee-bearing volume through minimal-amount legs.

### Proof of Concept
`cargo test` plan (near-workspaces sandbox, e.g. added to `tests/src/tests/defuse/intents/token_diff.rs`):
1. Set up `Env` with `fee = Pips::ONE_PERCENT` (nonzero), deploy an NEP-245 (`Mt`) token, deposit `1000` units of `token_id T` to `user1`, deposit an offsetting token (e.g. FT) balance to `user2` sufficient to receive `T` in return.
2. **Baseline**: `user1` signs a single `TokenDiff { diff: {T: -1000, ft: closure_delta(ft, +X, fee)} }`, matched with `user2`'s complementary intent; execute via `env.defuse_execute_intents`. Assert `fee_collector`'s balance of `T` equals `Pips::ONE_PERCENT.fee_ceil(1000)` (i.e. non-zero).
3. **Exploit**: Reset state (fresh users/tokens with same balances). `user1` signs 1000 separate `TokenDiff` intents, each `{T: -1, ft: 0-adjusted-leg}`, and `user2` signs matching legs (`+1` each), assembled so `TransferMatcher::finalize` nets to zero for both `T` and `ft`. Execute the batch via `env.defuse_execute_intents`.
4. Assert: `fee_collector`'s balance of `T` after the split-leg batch is `0`, while `user2` (or whichever party received `T`) ends up with the full `1000` units — i.e., `fees_collected(split) == 0 != Pips::ONE_PERCENT.fee_ceil(1000) == fees_collected(single)`, proving the fee bypass for identical aggregate economic transfer.

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

**File:** tests/src/tests/defuse/intents/token_diff.rs (L24-93)
```rust
#[rstest]
#[tokio::test]
async fn swap_p2p(
    #[values(Pips::ZERO, Pips::ONE_BIP, Pips::ONE_PERCENT)] fee: Pips,
    #[with(Env::builder().fee(fee))]
    #[future(awt)]
    env: Env,
) {
    let (user1, user2, ft1, ft2) = futures::join!(
        env.create_user(),
        env.create_user(),
        env.create_token(),
        env.create_token()
    );

    let ft1_token_id = TokenId::from(Nep141TokenId::new(ft1.contract_id().clone()));
    let ft2_token_id = TokenId::from(Nep141TokenId::new(ft2.contract_id().clone()));

    env.initial_ft_storage_deposit(
        vec![user1.account_id(), user2.account_id()],
        vec![ft1.contract_id(), ft2.contract_id()],
    )
    .await;

    test_ft_diffs(
        &env,
        [
            AccountFtDiff {
                account: &user1,
                init_balances: std::iter::once((ft1.contract_id(), 100)).collect(),
                diff: [TokenDeltas::default()
                    .with_apply_deltas([
                        (ft1_token_id.clone(), -100),
                        (
                            ft2_token_id.clone(),
                            TokenDiff::closure_delta(&ft2_token_id, -200, fee).unwrap(),
                        ),
                    ])
                    .unwrap()]
                .into(),
                result_balances: std::iter::once((
                    ft2.contract_id(),
                    TokenDiff::closure_delta(&ft2_token_id, -200, fee).unwrap(),
                ))
                .collect(),
            },
            AccountFtDiff {
                account: &user2,
                init_balances: std::iter::once((ft2.contract_id(), 200)).collect(),
                diff: [TokenDeltas::default()
                    .with_apply_deltas([
                        (
                            ft1_token_id.clone(),
                            TokenDiff::closure_delta(&ft1_token_id, -100, fee).unwrap(),
                        ),
                        (ft2_token_id.clone(), -200),
                    ])
                    .unwrap()]
                .into(),
                result_balances: std::iter::once((
                    ft1.contract_id(),
                    TokenDiff::closure_delta(&ft1_token_id, -100, fee).unwrap(),
                ))
                .collect(),
            },
        ]
        .into(),
    )
    .await;
}
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L303-345)
```rust
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
```
