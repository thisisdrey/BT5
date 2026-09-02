`No vulnerability found for this question.`

**Why the claimed binding does not break:**

The equality the question implies is broken is: `refund_credited_to_sender == amount_actually_still_held_by_receiver`. Tracing `mt_resolve_transfer` in `contracts/defuse/src/contract/tokens/nep245/resolver.rs`:

```
let receiver_balance = receiver.token_balances.amount_for(&token_id);   // line 72, read live at execution time
refund.0 = refund.0.min(receiver_balance);                              // line 74
...
receiver.token_balances.sub(token_id.clone(), refund.0).unwrap();       // line 83
``` [1](#0-0) 

`receiver_balance` is read fresh, inside `mt_resolve_transfer`'s own atomic execution, immediately before the `sub`. There is no possibility of state changing between the read at line 72 and the `sub` at line 83 within that single function call — NEAR contract calls execute to completion without interleaving with other receipts. So no TOCTOU exists *inside* `mt_resolve_transfer`.

The scenario the question describes requires the attacker's `mt_on_transfer` callee to trigger a *separate* receipt (a nested `execute_intents` call) that spends the receiver's balance, and for that spend to interleave with `mt_resolve_transfer`'s execution across receipts. But regardless of the relative ordering of that nested spend versus the resolve callback:

- If the nested spend executes **before** `mt_resolve_transfer`, the `receiver_balance` read at line 72 already reflects the reduced balance, so `refund.0.min(receiver_balance)` caps the refund to whatever is left (possibly 0) — the resolver cannot credit tokens that are no longer there.
- If the nested spend executes **after** `mt_resolve_transfer`, the resolver already reduced the receiver's balance via `sub` at line 83 before the nested spend runs, so the nested spend itself will fail with `DefuseError::BalanceOverflow` (via `Amounts::sub`'s `checked_sub` returning `None`) since insufficient balance remains, as seen in `internal_mt_batch_transfer` and the generic `internal_sub_balance` implementations, which all use the same checked, no-underflow `Amounts::sub`. [2](#0-1) [3](#0-2) 

In either interleaving, conservation holds: the resolver can never move (credit) more tokens back to the sender than the receiver currently, verifiably, still holds, and any attempted double-spend by the receiver's nested call fails outright due to the checked-arithmetic balance guard rather than succeeding silently. This is precisely the purpose of the comment "refund maximum what we can" at line 73 — it is a deliberate defense against exactly this reentrant-drain scenario, not an overlooked gap. [4](#0-3) 

No unbacked credit or double-count of the token balance is possible through this path; the described attack does not produce a state where `sum of token_balances changes for the token across the call != 0`.

### Citations

**File:** contracts/defuse/src/contract/tokens/nep245/resolver.rs (L51-84)
```rust
            refund.0 = refund.0.min(amount.0);
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
```

**File:** contracts/defuse/core/src/amounts.rs (L77-82)
```rust
    pub fn sub(&mut self, k: T::K, amount: u128) -> Option<T::V>
    where
        T::V: CheckedSub<u128>,
    {
        self.checked_apply(k, |a| a.checked_sub(amount))
    }
```

**File:** contracts/defuse/src/contract/tokens/nep245/core.rs (L182-196)
```rust
            self.accounts
                .get_mut(sender_id)
                .ok_or_else(|| DefuseError::AccountNotFound(sender_id.to_owned()))?
                .get_mut_maybe_forced(force)
                .ok_or_else(|| DefuseError::AccountLocked(sender_id.to_owned()))?
                .token_balances
                .sub(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;
            self.accounts
                .get_or_create(receiver_id.to_owned())
                // locked accounts are allowed to receive incoming transfers
                .as_inner_unchecked_mut()
                .token_balances
                .add(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
```
