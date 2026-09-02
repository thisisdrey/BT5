Based on tracing the code, the claimed vulnerability does not hold.

**Binding to verify:** `amount_transferred == value_delivered_to_receiver_side_effects + value_refunded_to_sender`

**Trace:**

1. `internal_mt_batch_transfer` in `contracts/defuse/src/contract/tokens/nep245/core.rs` first moves `amount` from sender into `receiver_id`'s internal balance via `add`, before `mt_on_transfer` is even called: [1](#0-0) 

2. Later, if the receiver spends part of that balance during a nested call inside `mt_on_transfer` (e.g. via another `internal_sub_balance`/withdraw), the receiver's `token_balances` for that token drops accordingly, *before* `mt_resolve_transfer` runs.

3. In `mt_resolve_transfer`, the refund from the (possibly malformed) callback response defaults to the full `amounts` only as an *upper bound*, but it is then explicitly capped by the receiver's **current** balance: [2](#0-1) 

4. This means: `refund = min(response_refund, amount, current_receiver_balance)`. If the receiver already spent `Y` of the transferred `amount` before the resolver runs, `current_receiver_balance` reflects that reduction, so `refund <= amount - Y`. The subsequent `sub`/`add` pair moves exactly that capped `refund`, not the full `amount`: [3](#0-2) 

So `amount == (amount - refund) [delivered/spent by receiver] + refund [returned to sender]` holds by construction — the cap by `receiver_balance` is exactly the guard that prevents the double credit the question hypothesizes. This is a deliberate, documented design choice (see the comment at lines 55-66) to allow refunds even from now-locked accounts, while still bounding the refund by what the receiver actually still holds.

The `Lock::as_inner_unchecked_mut()` bypass on locked accounts (lines 67, 89) only affects whether a refund can be *routed through* a locked account — it does not remove the balance cap that prevents refunding more than what remains. The "attacker's receiver spends via nested call, then returns malformed JSON to force full refund" scenario is neutralized because the balance-based cap (line 74) reflects the nested spend, so the "full default refund" is clamped down to whatever is actually left in the receiver's ledger entry.

No vulnerability found for this question.

### Citations

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

**File:** contracts/defuse/src/contract/tokens/nep245/resolver.rs (L72-78)
```rust
            let receiver_balance = receiver.token_balances.amount_for(&token_id);
            // refund maximum what we can
            refund.0 = refund.0.min(receiver_balance);
            if refund.0 == 0 {
                // noting to refund
                continue;
            }
```

**File:** contracts/defuse/src/contract/tokens/nep245/resolver.rs (L80-95)
```rust
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

            // update as used amount in-place
            amount.0 -= refund.0;
```
