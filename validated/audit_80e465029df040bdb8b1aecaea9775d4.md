## Analysis

The external report flags a fee-cap governance issue (NativeRouter widget fee up to 100%) — that class doesn't transfer directly since the analogous NEAR Intents fee-setter (`Contract::set_fee` in [1](#0-0)  ) is gated by `Role::DAO`/`Role::FeesManager`, which is explicitly out of scope for an unprivileged-attacker analog.

However, a genuine unprivileged fee-bypass exists in the same fee-charging mechanism, breaking the "fees owed vs fees collected" binding.

### Title
Protocol fee on MT/IMT `TokenDiff` intents can be fully bypassed by splitting a swap into unit-amount legs - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` intentionally waives the protocol fee for `Nep245`/`Imt` token deltas whenever the transferred `amount <= 1`, to avoid taxing NFT-like transfers. Because `DefuseIntents` allows batching an arbitrary number of `Intent::TokenDiff` entries inside a single signed `MultiPayload`, any unprivileged user can decompose a large `Nep245`/`Imt` balance change into many separate `TokenDiff` intents each moving exactly `1` unit, so every leg individually falls under the `amount <= 1` exemption and the fee charged is `0`, even though the aggregate value moved is large.

### Finding Description
`TokenDiff::execute_intent` computes the fee per intent as `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` [2](#0-1)  where `token_fee` is: [3](#0-2) 
For `TokenIdType::Nep245`/`Imt`, the real protocol `fee` is only applied `if amount > 1`; for `amount <= 1` it returns `Pips::ZERO`. This per-intent check operates on the delta of a single `TokenDiff` intent, not on the cumulative amount transferred by the signer. Since `DefuseIntents::execute_intent` iterates over an arbitrary `Vec<Intent>` and applies each one independently [4](#0-3) , a single signer (or a signer coordinating with a counterparty solver, as in the p2p swap pattern shown in the test suite [5](#0-4) ) can submit N `TokenDiff` intents, each with a `Nep245`/`Imt` delta of exactly `±1`, to move `N` units of the token while triggering the `amount <= 1` fee-exempt branch on every single leg. The equality that should hold — `fees_owed(total_amount_moved) == fees_collected` — is broken: `fees_owed(N) > 0` for the true fee schedule, but `fees_collected = 0` because each atomic leg is charged individually at `amount = 1`.

### Impact Explanation
This directly matches the Critical bucket "fees bypassed or over-collected": the protocol fee configured by `set_fee`/`FeesConfig` [6](#0-5)  can be circumvented entirely for any MT (`Nep245`)/IMT-denominated flow, letting solvers/swappers avoid paying the protocol its due fee on volume that should be fee-bearing, at the expense of the fee collector's expected revenue.

### Likelihood Explanation
No privileged role, relayer key, or special access is required — any account able to sign a `MultiPayload` with multiple `TokenDiff` intents can exploit this. The only limiting factor is NEAR gas/action limits per transaction, which still allow bypassing fees on a meaningful multiple of the token's `amount=1` unit within a single `execute_intents` call, and the exploit can be repeated across transactions without bound.

### Recommendation
Compute and cap the `Nep245`/`Imt` fee waiver based on the cumulative absolute delta the signer applies to a given token across the whole `DefuseIntents` batch (or globally require `fee_ceil` computation before per-intent decomposition), rather than exempting fees per individual intent leg with `amount <= 1`.

### Proof of Concept
1. Attacker (or attacker + colluding solver) holds a large balance of a `Nep245`/`Imt` token inside Defuse.
2. Attacker signs one `MultiPayload` containing `N` separate `Intent::TokenDiff` entries, each with `diff = {token_id: -1}` (or `+1` on the counterpart side), instead of one `TokenDiff` with `diff = {token_id: -N}`.
3. `DefuseIntents::execute_intent` processes each `TokenDiff` intent independently [7](#0-6) ; for each, `TokenDiff::token_fee` sees `amount == 1` and returns `Pips::ZERO` [8](#0-7) , so `fee_ceil(1) == 0` on every leg.
4. Total fee collected across the batch is `0`, whereas a single `TokenDiff` moving `N` units at once would have collected `protocol_fee.fee_ceil(N) > 0`.

### Citations

**File:** contracts/defuse/src/contract/fees.rs (L15-29)
```rust
#[near]
impl FeesManager for Contract {
    #[pause(name = "intents")]
    #[access_control_any(roles(Role::DAO, Role::FeesManager))]
    #[payable]
    fn set_fee(&mut self, #[allow(unused_mut)] mut fee: Pips) {
        assert_one_yocto();
        require!(self.fees.fee != fee, "same");
        mem::swap(&mut self.fees.fee, &mut fee);
        FeeChangedEvent {
            old_fee: fee,
            new_fee: self.fees.fee,
        }
        .emit();
    }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L69-78)
```rust
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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L206-217)
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

**File:** contracts/defuse/core/src/fees.rs (L10-16)
```rust
#[cfg_attr(feature = "borsh-schema", derive(::borsh::BorshSchema))]
#[cfg_attr(feature = "schemars-v0_8", derive(::schemars::JsonSchema))]
#[derive(Debug, Clone, Serialize, Deserialize, BorshSerialize, BorshDeserialize)]
pub struct FeesConfig {
    pub fee: Pips,
    pub fee_collector: AccountId,
}
```
