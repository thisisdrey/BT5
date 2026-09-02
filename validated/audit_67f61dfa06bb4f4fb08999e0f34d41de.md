No vulnerability found for this question.

**Analysis:** `Transfer::execute_intent` in [1](#0-0)  performs `internal_sub_balance` on the sender and `internal_add_balance` on the receiver synchronously as an atomic intra-ledger accounting operation — this is not a transfer of tokens into or out of the Verifier's custody, but a purely internal balance reassignment within Defuse's own `token_balances` map, as implemented in [2](#0-1) . The `NotifyOnTransfer` mechanism is a best-effort hook (analogous to a post-transfer callback) dispatched via `notify_on_transfer`, which is `.detach()`ed with no resolver, as shown in [3](#0-2) .

Since the balance mutation and the notify Promise are decoupled by design, the binding the question challenges (`sub(sender) == add(receiver)`) holds unconditionally regardless of whether the notify promise succeeds or fails due to gas exhaustion — there is nothing to "refund" because no external asset was ever pending confirmation. This is fundamentally different from the Deposit paths (`ft_resolve_deposit`/`mt_resolve_deposit`), which exist because deposits originate from *external* token contracts and the internal credit is provisional until the resolver confirms the external transfer actually occurred/didn't need reversal. For `Transfer`, both sides of the ledger move already fully settle within the same synchronous execution, so total internal supply is conserved and the signer's authorized transfer (1000 ft1 from sender to receiver_id) is exactly what is recorded — no value is created, destroyed, or misdirected beyond what the signer authorized. The scoped concern ("no refund path for Transfer notify failures") is confirmed intentional documented behavior (see the doc comments on `min_gas` at lines 74-79) and does not constitute unauthorized fund movement, a double-settlement, or a broken conservation invariant.

### Citations

**File:** contracts/defuse/core/src/intents/tokens.rs (L107-125)
```rust
        engine
            .state
            .internal_sub_balance(sender_id, self.tokens.clone())?;
        engine
            .state
            .internal_add_balance(self.receiver_id.clone(), self.tokens.clone())?;

        if let Some(mut notification) = self.notification {
            notification.min_gas = Some(
                notification
                    .min_gas
                    .unwrap_or(MT_ON_TRANSFER_GAS_DEFAULT)
                    .max(MT_ON_TRANSFER_GAS_MIN),
            );

            engine
                .state
                .notify_on_transfer(sender_id, self.receiver_id, self.tokens, notification);
        }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L147-195)
```rust
    fn internal_add_balance(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        let owner = self
            .accounts
            .get_or_create(owner_id)
            // we allow locked accounts to accept deposits and incoming deposits
            .as_inner_unchecked_mut();

        for (token_id, amount) in tokens {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }
            owner
                .token_balances
                .add(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }

        Ok(())
    }

    fn internal_sub_balance(
        &mut self,
        owner_id: &AccountIdRef,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        let owner = self
            .accounts
            .get_mut(owner_id)
            .ok_or_else(|| DefuseError::AccountNotFound(owner_id.to_owned()))?
            .get_mut()
            .ok_or_else(|| DefuseError::AccountLocked(owner_id.to_owned()))?;

        for (token_id, amount) in tokens {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            owner
                .token_balances
                .sub(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }

        Ok(())
    }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L242-263)
```rust
    #[inline]
    fn notify_on_transfer(
        &self,
        sender_id: &AccountIdRef,
        receiver_id: AccountId,
        tokens: Amounts,
        notification: NotifyOnTransfer,
    ) {
        let (token_ids, amounts) = tokens
            .iter()
            .map(|(token_id, amount)| (token_id.to_string(), U128(*amount)))
            .unzip();

        Self::notify_and_resolve_transfer(
            sender_id.to_owned(),
            receiver_id,
            token_ids,
            amounts,
            notification,
        )
        .detach();
    }
```
