### No vulnerability found for this question.

The code already reads the receiver's balance live at resolve time via `self.accounts.get_mut(&receiver_id)` and `receiver.token_balances.amount_for(&token_id)` [1](#0-0) , and clamps the refund to that live balance with `refund.0 = refund.0.min(receiver_balance)` before performing `sub`/`add` [2](#0-1) . If the attacker's `mt_on_transfer` implementation moves the balance out via a nested `execute_intents` call before returning the refund array, that state change is fully committed by the time `mt_resolve_transfer` executes as the callback, since `mt_resolve_transfer` only runs after the `mt_on_transfer` promise resolves. So `receiver_balance` reflects the post-drain state (e.g. 0), and the clamp forces `refund.0` to that same reduced value, not the originally deposited amount. There is no separate "snapshot at deposit time" used anywhere in this function — the only balance ever read is the current one at line 72. This satisfies the conservation binding: `refund_credited == min(requested_refund, amount_deposited, live_receiver_balance_at_resolve)`, so the attacker cannot obtain both the moved-out value and a nonzero refund on the same deposited amount.

### Citations

**File:** contracts/defuse/src/contract/tokens/nep245/resolver.rs (L52-92)
```rust
            let Some(receiver) = self
                .accounts
                .get_mut(&receiver_id)
                // NOTE: refunds from locked accounts are allowed to prevent
                // senders from loss of funds.
                //
                // Receiver's account might have been locked between
                // `mt_transfer_call()` and `mt_resolve_transfer()`, so that
                // outgoing transfers are no longer allowed for this account.
                // But here we distinguish between regular transfers and
                // refunds, despite it would lead to `mt_transfer` event
                // emitted with `old_owner_id` being the locked account.
                //
                // Locked receivers still won't be able to transfer funds in
                // `<receiver_id>::on_mt_transfer()` implementation.
                .map(Lock::as_inner_unchecked_mut)
            else {
                // receiver doesn't have an account, so nowhere to refund from
                return amounts;
            };
            let receiver_balance = receiver.token_balances.amount_for(&token_id);
            // refund maximum what we can
            refund.0 = refund.0.min(receiver_balance);
            if refund.0 == 0 {
                // noting to refund
                continue;
            }

            // withdraw refund
            receiver
                .token_balances
                .sub(token_id.clone(), refund.0)
                .unwrap();
            // deposit refund
            self.accounts
                .get_or_create(previous_owner_id)
                // refunds are allowed for locked accounts
                .as_inner_unchecked_mut()
                .token_balances
                .add(token_id, refund.0)
                .unwrap();
```
