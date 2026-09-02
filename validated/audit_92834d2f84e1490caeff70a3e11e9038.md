### Title
`storage_deposit` intents debit the full wNEAR amount but any excess refunded by NEP-145 token contracts is captured by the Defuse contract, not the user - (File: `contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs`)

### Summary
The `StorageDeposit` intent (and the `storage_deposit` field on `FtWithdraw`/`NftWithdraw`/`MtWithdraw`) debits a user's internal wNEAR balance for the *exact* amount specified, unwraps that amount to native NEAR, and attaches all of it as `attached_deposit` to a `storage_deposit()` call on the target NEP-141/NEP-171/NEP-245 contract. Per the NEP-145 standard, if the attached deposit exceeds what is actually required (e.g., the account is already registered, or the amount exceeds `storage_balance_bounds().max`), the excess is refunded by the target token contract to its `predecessor_account_id` — which in this call chain is the Defuse contract itself, not the end user. Because the user's internal ledger was already debited for the full amount before the promise resolves, any such refund is captured by the Defuse contract and never credited back to the user, mirroring the WETHGateway "excess repayment stuck" bug class: value debited from the user does not equal value delivered (registration) plus value refunded (to the user).

### Finding Description
The relevant call chain is:

1. `StorageDeposit::execute_intent` → `state.storage_deposit(owner_id, self)` [1](#0-0) 
2. `Contract::storage_deposit` subtracts `storage_deposit.amount` (in full) from the owner's internal wNEAR balance via `self.withdraw(...)`, then calls `wnear.near_withdraw(amount)` followed by `do_storage_deposit(storage_deposit)`: [2](#0-1) 
3. `do_storage_deposit` attaches the *entire* unwrapped NEAR amount to `ext_storage_management::ext(contract_id).storage_deposit(Some(deposit_for_account_id), None)`: [3](#0-2) 

The Defuse contract (not the user) is the caller/predecessor of this cross-contract `storage_deposit()` call. Standard NEP-145 implementations refund any deposit in excess of the required registration/top-up bound (for example, when the target account is already registered, or the attached amount exceeds `storage_balance_bounds().max`) directly to the caller — here, the Defuse contract — via a native NEAR transfer. This refund is never captured or credited back to the user's internal ledger by Defuse: there is no resolver/callback in `storage_deposit.rs` that inspects the `storage_deposit()` return value or forwards any refund to the user.

Meanwhile, the user's internal wNEAR balance was already permanently debited for the *full* specified amount in step 2, regardless of how much was actually consumed by the registration. This breaks the invariant that "value debited == value delivered + value refunded to the same party." Excess ends up as NEAR balance retained by the Defuse contract itself rather than being returned to the user who paid for it — structurally identical to the WETHGateway issue where excess ETH sent for `repay()` stayed in the gateway contract instead of being returned to the caller.

Note that the top-level `StorageDeposit` intent's doc comment does state "the wNEAR will not be refunded in any case," which is an explicit, documented acknowledgment of this exact behavior for that specific intent. However, the same unrefunded-excess mechanism also applies to the `storage_deposit` field embedded in `FtWithdraw`, `NftWithdraw`, and `MtWithdraw`, whose doc comments only warn about non-refund "in case of fail," not about the no-refund-on-excess/already-registered case: [4](#0-3) [5](#0-4) . For those code paths, users are not warned that overpaying (or paying for an already-registered account) results in permanent loss of the excess to the Defuse contract rather than a refund.

### Impact Explanation
Any excess wNEAR a user allocates via the `storage_deposit` mechanism (whether through the standalone `StorageDeposit` intent or the `storage_deposit` field of withdraw intents) that is refunded by the destination token contract is captured by the Defuse contract's native NEAR balance instead of being returned to the user. This is a direct, unauthorized value loss for the user: funds are debited from their custody-tracked balance but never delivered to them nor to the intended destination — they accrue to the platform contract. This satisfies the "value debited versus value delivered plus refunded" custody-binding violation described in the rules, and results in concrete value loss for the paying account.

### Likelihood Explanation
This requires no privileged access — any account signing a `StorageDeposit`, `FtWithdraw`, `NftWithdraw`, or `MtWithdraw` intent with a `storage_deposit` amount that is even slightly larger than strictly required (or where the target account is already storage-registered on the destination token contract) triggers the loss. Overestimating storage deposit amounts is a common and encouraged practice (to guarantee sufficiency across variable storage costs), making this easy to trigger unintentionally, and repeatable by any user.

### Recommendation
For `do_storage_deposit` (and equivalently for the `storage_deposit` field handling in `FtWithdraw`/`NftWithdraw`/`MtWithdraw`), chain a resolver callback that inspects the `Promise` return value of `storage_deposit()` (which returns the resulting `StorageBalance`, from which the actually-consumed portion can be derived) or otherwise computes `unused = attached_deposit - actually_required`, and credits/refunds `unused` back to the user's internal wNEAR balance (or forwards it as native NEAR to the user) rather than letting it be silently retained by the Defuse contract.

### Proof of Concept
1. User A calls `ft_on_transfer`/deposits wNEAR into Defuse, giving them an internal wNEAR balance of `N`.
2. `receiver_id` is an account already registered (has storage balance) on target FT contract `T` (or the user simply overestimates the required deposit).
3. User A signs and submits a `FtWithdraw` intent (or standalone `StorageDeposit` intent) specifying `storage_deposit = X` yoctoNEAR, where `X` is greater than `T`'s minimum registration requirement (or `receiver_id` is already registered so the whole `X` is unneeded).
4. `Contract::storage_deposit`/`internal_ft_withdraw` debits `X` in full from User A's internal wNEAR balance, unwraps `X` NEAR, and attaches all of it to `T::storage_deposit(receiver_id, None)`.
5. `T` (implementing standard NEP-145 semantics) refunds the unused portion of `X` to its predecessor — the Defuse contract's account — via a native `Promise::transfer`.
6. User A's internal ledger remains debited by the full `X`; the refunded excess NEAR is now held by the Defuse contract's account balance and is never returned to User A. No resolver/callback exists in `storage_deposit.rs` to capture or forward this refund back to User A. [3](#0-2)

### Citations

**File:** contracts/defuse/core/src/intents/tokens.rs (L143-153)
```rust
    /// Message to pass to `ft_transfer_call`. Otherwise, `ft_transfer` will be used.
    /// NOTE: No refund will be made in case of insufficient `storage_deposit`
    /// on `token` for `receiver_id`
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub msg: Option<String>,

    /// Optionally make `storage_deposit` for `receiver_id` on `token`.
    /// The amount will be subtracted from user's NEP-141 `wNEAR` balance.
    /// NOTE: the `wNEAR` will not be refunded in case of fail
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub storage_deposit: Option<NearToken>,
```

**File:** contracts/defuse/core/src/intents/tokens.rs (L238-248)
```rust
    /// Message to pass to `nft_transfer_call`. Otherwise, `nft_transfer` will be used.
    /// NOTE: No refund will be made in case of insufficient `storage_deposit`
    /// on `token` for `receiver_id`
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub msg: Option<String>,

    /// Optionally make `storage_deposit` for `receiver_id` on `token`.
    /// The amount will be subtracted from user's NEP-141 `wNEAR` balance.
    /// NOTE: the `wNEAR` will not be refunded in case of fail
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub storage_deposit: Option<NearToken>,
```

**File:** contracts/defuse/core/src/intents/tokens.rs (L487-510)
```rust
impl ExecutableIntent for StorageDeposit {
    #[inline]
    fn execute_intent<S, I>(
        self,
        owner_id: &AccountIdRef,
        engine: &mut Engine<S, I>,
        intent_hash: [u8; 32],
    ) -> Result<()>
    where
        S: State,
        I: Inspector,
    {
        engine
            .inspector
            .on_event(DefuseEvent::StorageDeposit(Cow::Borrowed(
                [MaybeIntentEvent::new_intent(
                    AccountEvent::new(owner_id, Cow::Borrowed(&self)),
                    intent_hash,
                )]
                .as_slice(),
            )));

        engine.state.storage_deposit(owner_id, self)
    }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L265-297)
```rust
    fn storage_deposit(
        &mut self,
        owner_id: &AccountIdRef,
        storage_deposit: StorageDeposit,
    ) -> Result<()> {
        self.withdraw(
            owner_id,
            [(
                Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                storage_deposit.amount.as_yoctonear(),
            )],
            Some("withdraw"),
            false,
        )?;

        ext_wnear::ext(self.wnear_id.clone())
            .with_attached_deposit(NearToken::from_yoctonear(1))
            .with_static_gas(NEAR_WITHDRAW_GAS)
            // do not distribute remaining gas here
            .with_unused_gas_weight(0)
            .near_withdraw(U128(storage_deposit.amount.as_yoctonear()))
            .then(
                // do_storage_deposit only after unwrapping NEAR
                Self::ext(env::current_account_id())
                    .with_static_gas(Self::DO_STORAGE_DEPOSIT_GAS)
                    // do not distribute remaining gas here
                    .with_unused_gas_weight(0)
                    .do_storage_deposit(storage_deposit),
            )
            .detach();

        Ok(())
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs (L13-26)
```rust
    #[private]
    pub fn do_storage_deposit(storage_deposit: StorageDeposit) -> Promise {
        require!(
            promise_result_checked_void(0).is_ok(),
            "near_withdraw failed",
        );

        ext_storage_management::ext(storage_deposit.contract_id)
            .with_attached_deposit(storage_deposit.amount)
            .with_static_gas(STORAGE_DEPOSIT_GAS)
            // do not distribute remaining gas here
            .with_unused_gas_weight(0)
            .storage_deposit(Some(storage_deposit.deposit_for_account_id), None)
    }
```
