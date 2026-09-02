### No vulnerability found for this question.

Analysis: The clamp `used.0.min(amount.0)` at [1](#0-0)  does exactly what the question asks—it bounds `used` to at most `amount.0` regardless of what the attacker-controlled FT contract returns from `ft_transfer_call`. Since `refund = amount.0.saturating_sub(used)` at [2](#0-1) , the identity `refund + used == amount.0` always holds by construction once `used` is clamped into `[0, amount.0]`. Even without the `.min(amount.0)` clamp, `saturating_sub` would already prevent underflow and force `refund` to `0` when `used` exceeds `amount.0`, so the balance-crediting path (`self.deposit` at [3](#0-2) ) can never credit more than `amount.0` total back to the sender.

The `amount.0` itself was already debited from the sender's Defuse balance before the promise chain was scheduled, via `self.withdraw(...)` in `internal_ft_withdraw` [4](#0-3) , and the actual `ft_transfer_call` invocation on the attacker's own fake token contract is made with exactly `withdraw.amount` (i.e., `amount.0`), never more [5](#0-4) . So the only value the FT contract's return can affect is how the fixed `amount.0` accounting splits between `used` (kept as spent) and `refund` (credited back)—the clamp only affects the informational `U128(used)` return value of `ft_resolve_withdraw`, not the actual internal balance mutation, which is bounded correctly. There is no path by which an inflated `used` return value from a malicious `ft_transfer_call` implementation causes the resolver to credit more than the `amount.0` originally debited, and since the token in question is the attacker's own deployed contract (`Nep141TokenId::new(token)`), no other party's funds or the Verifier's real custody are at risk regardless.

### Citations

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L60-74)
```rust
        self.withdraw(
            &owner_id,
            iter::once((
                Nep141TokenId::new(withdraw.token.clone()).into(),
                withdraw.amount,
            ))
            .chain(withdraw.storage_deposit.map(|amount| {
                (
                    Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                    amount.as_yoctonear(),
                )
            })),
            Some("withdraw"),
            force,
        )?;
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L141-150)
```rust
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
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L164-173)
```rust
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
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L183-183)
```rust
        let refund = amount.0.saturating_sub(used);
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L185-190)
```rust
            self.deposit(
                sender_id,
                [(Nep141TokenId::new(token).into(), refund)],
                Some(REFUND_MEMO),
            )
            .unwrap_or_else(|err| err.panic());
```
