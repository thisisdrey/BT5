### No vulnerability found for this question.

Analysis confirms the premise is factually incorrect for the specific target (`Deltas`/`sub_add` in `deltas.rs`).

`Deltas<S>::internal_add_balance` / `internal_sub_balance` and the underlying `TransferMatcher`/`TokenTransferMatcher::sub_add` mutate the in-memory ledger synchronously, as plain Rust function calls inside the single execution of `execute_signed_intent` → `DefuseIntents::execute_intent`, which iterates `self.intents` with a simple sequential `for` loop [1](#0-0) . Every subsequent intent in the batch reads `self.state.balance_of(...)` and writes through `internal_add_balance`/`internal_sub_balance` against the state already mutated by all preceding intents in the same call stack [2](#0-1) . `Deltas::finalize()` is only invoked after this synchronous loop completes for the whole `MultiPayload` batch, and it rejects any batch whose deltas don't net to zero (`InvariantViolated::UnmatchedDeltas`/`Overflow`) [3](#0-2) [4](#0-3) .

The "promises created by different intents execute concurrently" warning refers to the actual NEAR cross-contract `Promise`s scheduled by intents like `FtWithdraw`, `MtWithdraw`, and `StorageDeposit` (e.g. `do_ft_withdraw`, `do_mt_withdraw`, `do_storage_deposit`) [5](#0-4) [6](#0-5) . Those are dispatched to external NEP-141/171/245 contracts (or `wnear`) and are only resolved by separate later receipts (`ft_resolve_withdraw`, `mt_resolve_withdraw`) which re-credit failed amounts as refunds [7](#0-6) . This is explicitly documented as a warning on `StorageDeposit` itself, instructing callers not to rely on promise ordering when a subsequent intent's success depends on an earlier `Promise`'s completion [8](#0-7) . Crucially, this concurrency applies only to the fired `Promise` objects (external effects), not to the Verifier's internal balance ledger tracked by `Deltas`, which is fully computed and validated before any of those promises are even scheduled to run.

Since `Deltas` never observes a "not-yet-settled" balance from a sibling intent's promise — the ledger state transition for the whole batch is deterministic, sequential Rust code executed atomically within one receipt — the claimed invariant break (`state each intent acts on == state produced by all preceding intents`) does not exist for this file/mechanism. No reachable path lets an attacker make one intent's balance check race against another intent's promise resolution within the same `Deltas` instance.

### Citations

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L136-164)
```rust
    fn internal_add_balance(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        for (token_id, amount) in tokens {
            self.state
                .internal_add_balance(owner_id.clone(), [(token_id.clone(), amount)])?;
            if !self.deltas.deposit(owner_id.clone(), token_id, amount) {
                return Err(DefuseError::BalanceOverflow);
            }
        }
        Ok(())
    }

    fn internal_sub_balance(
        &mut self,
        owner_id: &AccountIdRef,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        for (token_id, amount) in tokens {
            self.state
                .internal_sub_balance(owner_id, [(token_id.clone(), amount)])?;
            if !self.deltas.withdraw(owner_id.to_owned(), token_id, amount) {
                return Err(DefuseError::BalanceOverflow);
            }
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

**File:** contracts/defuse/core/src/engine/mod.rs (L113-118)
```rust
    #[inline]
    fn finalize(self) -> Result<Transfers> {
        self.state
            .finalize()
            .map_err(DefuseError::InvariantViolated)
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L117-151)
```rust
    #[private]
    pub fn do_ft_withdraw(withdraw: FtWithdraw) -> Promise {
        let min_gas = withdraw.min_gas();
        let p = if let Some(storage_deposit) = withdraw.storage_deposit {
            require!(
                promise_result_checked_void(0).is_ok(),
                "near_withdraw failed",
            );

            ext_storage_management::ext(withdraw.token)
                .with_attached_deposit(storage_deposit)
                .with_static_gas(STORAGE_DEPOSIT_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .storage_deposit(Some(withdraw.receiver_id.clone()), None)
        } else {
            Promise::new(withdraw.token)
        };

        let p = ext_ft_core::ext_on(p)
            .with_attached_deposit(NearToken::from_yoctonear(1))
            .with_static_gas(min_gas)
            // distribute remaining gas here
            .with_unused_gas_weight(1);
        if let Some(msg) = withdraw.msg {
            p.ft_transfer_call(
                withdraw.receiver_id,
                withdraw.amount.into(),
                withdraw.memo,
                msg,
            )
        } else {
            p.ft_transfer(withdraw.receiver_id, withdraw.amount.into(), withdraw.memo)
        }
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L154-194)
```rust
#[near]
impl FungibleTokenWithdrawResolver for Contract {
    #[private]
    fn ft_resolve_withdraw(
        &mut self,
        token: AccountId,
        sender_id: AccountId,
        amount: U128,
        is_call: bool,
    ) -> U128 {
        let used = if is_call {
            // `ft_transfer_call` returns successfully transferred amount
            match promise_result_checked_json::<U128>(0) {
                Ok(Ok(used)) => used.0.min(amount.0),
                Ok(Err(_deserialize_err)) => 0,
                // do not refund on failed `ft_transfer_call` due to
                // NEP-141 vulnerability: `ft_resolve_transfer` fails to
                // read result of `ft_on_transfer` due to insufficient gas
                Err(_) => amount.0,
            }
        } else {
            // `ft_transfer` returns empty result on success
            if promise_result_checked_void(0).is_ok() {
                amount.0
            } else {
                0
            }
        };

        let refund = amount.0.saturating_sub(used);
        if refund > 0 {
            self.deposit(
                sender_id,
                [(Nep141TokenId::new(token).into(), refund)],
                Some(REFUND_MEMO),
            )
            .unwrap_or_else(|err| err.panic());
        }

        U128(used)
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

**File:** contracts/defuse/core/src/intents/tokens.rs (L463-473)
```rust
/// Make [NEP-145](https://nomicon.io/Standards/StorageManagement#nep-145)
/// `storage_deposit` for an `account_id` on `contract_id`.
/// The `amount` will be subtracted from user's NEP-141 `wNEAR` balance.
/// NOTE: the `wNEAR` will not be refunded in any case.
///
/// WARNING: use this intent only if paying storage deposit is not a prerequisite
/// for other intents to succeed. If some intent (e.g. `ft_withdraw`) requires storage deposit,
/// then use `storage_deposit` field of corresponding intent instead of adding a separate
/// `StorageDeposit` intent. This is due to the fact that intents that fire `Promise`s
/// are not guaranteed to be executed sequentially, in the order of the provided intents in
/// `DefuseIntents`.
```
