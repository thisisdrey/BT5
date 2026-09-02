## Title
`mt_resolve_withdraw` full-refund on length-mismatched `mt_batch_transfer_call` return double-credits sender when a malicious token contract genuinely transfers but returns a malformed-length array - (File: contracts/defuse/src/contract/tokens/nep245/withdraw.rs)

### Summary
`mt_resolve_withdraw`'s `is_call=true` branch treats any successfully-deserialized `Vec<U128>` whose length doesn't match `amounts.len()` as a total failure (`used = [0, 0, ...]`), unconditionally refunding the full withdrawn amount back to `sender_id` via `self.deposit`. Because `M` (the external NEP-245 token contract named in the withdrawal) is fully attacker-controlled, it can genuinely execute the transfer inside its own ledger while deliberately returning a JSON array of the wrong length, causing the Verifier to both let the assets leave custody and re-credit the sender internally.

### Finding Description
The broken binding: `amount_debited_from_sender_ledger == assets_that_actually_left_custody_at_M + amount_recredited_by_resolver`. For `t1`: debited=100, delivered=100, recredited should be 0; instead recredited=100 (double).

Code path:
1. `internal_mt_withdraw` (contracts/defuse/src/contract/tokens/nep245/withdraw.rs:60-125) debits the sender's `Nep245TokenId(M, t1/t2)` balances via `self.withdraw` (contracts/defuse/src/contract/tokens/mod.rs:76-128), decrementing both the account balance and `total_supplies`.
2. `do_mt_withdraw` (withdraw.rs:154-197) calls `M.mt_batch_transfer_call(receiver_id, [t1,t2], [100,200], ...)`. Since `M` is attacker-deployed, its implementation can genuinely move the underlying assets from the Verifier's account to `receiver_id` inside `M`'s own ledger, while returning a JSON array with the wrong number of elements (e.g., one element instead of two, or three instead of two) — still valid JSON, still within the byte budget enforced by `promise_result_checked_json_with_len` (crates/near/utils/src/promise.rs:55-62), which only bounds decode size, not array length.
3. In `mt_resolve_withdraw` (withdraw.rs:203-256):
```rust
match promise_result_checked_json_with_len::<Vec<U128>>(0, amounts.len()) {
    Ok(Ok(used)) if used.len() == amounts.len() => used,
    Ok(_) => vec![U128(0); amounts.len()],   // <-- length mismatch treated as "nothing used"
    Err(_) => amounts.clone(),
}
```
Since the returned array's length doesn't equal `amounts.len()`, this hits `Ok(_) => vec![U128(0); amounts.len()]`, i.e., `used = [0, 0]`.
4. `self.deposit(sender_id, ...)` (contracts/defuse/src/contract/tokens/mod.rs:18-74) then re-credits `refund = amount - used = [100, 200]` back to the sender's `Nep245TokenId(M, t1)`/`(M, t2)` balances and correspondingly increments `total_supplies` again.

Net effect: the sender's Defuse balance for `Nep245TokenId(M, t1)`/`(M, t2)` is restored to its pre-withdrawal value, while the tokens have already genuinely left the Verifier's custody at `M` and moved to `receiver_id`. The Verifier's internal ledger (and `total_supplies`) now claims backing that no longer exists on `M`.

Why existing guards don't help: the length-mismatch arm was designed as a defensive fallback (paired with the `Err(_) => amounts.clone()` comment about the "NEP-141 gas-starvation" bug), but its logic is the opposite of the `Err(_)` branch's philosophy — it assumes malformed-but-successfully-decoded output means "nothing happened," when for an untrusted, attacker-controlled token contract it can equally mean "everything happened, but I crafted a bogus return shape." Nothing in `mt_resolve_withdraw` cross-checks the actual on-chain balance change at `M` before crediting the refund.

### Impact Explanation
This double-credits the sender's internal Nep245 balance for the involved `TokenId`s without any real backing, inflating `total_supplies` beyond what the Defuse contract actually custodies at `M`. The attacker (or a counterparty who is later tricked into accepting this token via an intent swap) ends up holding a balance that cannot be fully honored on a future withdrawal, since the underlying tokens at `M` are already gone. This is the "refund or resolver credit that does not match what failed to settle" / "balance changes do not net to zero" Critical category — the Verifier's ledger for this `TokenId` now owes more than it custodies. The attack is repeatable per withdrawal call, across arbitrary token_ids/amounts, and the attacker only needs to control the NEP-245 contract named as the withdrawal `token`, which they are explicitly permitted to do (deploy their own MT contract).

### Likelihood Explanation
Preconditions: the attacker deploys a completely custom MT contract `M`; deposits (via `mt_on_transfer` from `M`) balances of `Nep245TokenId(M, t1/t2)` into their own Defuse account (freely achievable since `M` is their own contract); then issues `MtWithdraw{token: M, token_ids, amounts, msg: Some(...)}` and lets `M.mt_batch_transfer_call` genuinely move funds but return an array of the wrong length. This requires no privileged role, no signature forgery, and no race condition — just a custom contract implementation, which is well within the attacker's stated capabilities. It is fully deterministic and repeatable.

### Recommendation
In `mt_resolve_withdraw`'s length-mismatch arm, treat a malformed-but-successful response the same way as the `Err(_)` case (assume `used = amounts.clone()`, i.e. no refund) rather than assuming zero usage, consistent with the documented defensive rationale already used for `Err(_)`. Alternatively/additionally, verify actual token movement (e.g., via a balance check on `M`) before crediting any refund, or require the callee's return array to have the exact expected length to be honored at all, defaulting to "no refund" on any deviation.

### Proof of Concept
`cargo test` plan (near-workspaces sandbox):
1. Deploy a custom MT contract `M` whose `mt_batch_transfer_call(receiver_id, token_ids, amounts, ...)` genuinely subtracts from the caller's (Defuse contract's) balance in `M` and credits `receiver_id`, but returns a manually serialized `Vec<U128>` with one fewer element than `amounts.len()` (e.g. return `vec![U128(100)]` when two tokens were transferred).
2. In Defuse, deposit `Nep245TokenId(M, t1)=100` and `Nep245TokenId(M, t2)=200` to `user` via `mt_on_transfer` from `M`.
3. Sign and execute `MtWithdraw{token: M, receiver_id: <attacker>, token_ids:[t1,t2], amounts:[100,200], msg: Some("go")}`.
4. Assert on both sides of the binding:
   - `M`'s ledger shows `receiver_id` balance for `t1`=100 and `t2`=200 (tokens genuinely delivered — assets left custody).
   - Defuse's `mt_balance_of(user, Nep245TokenId(M,t1))` == 100 and `(M,t2)` == 200 (i.e., unchanged from pre-withdrawal — full refund credited on top of delivered assets).
   - `total_supplies` for these `TokenId`s in Defuse equals pre-withdrawal value, despite `M` no longer custodying those tokens for Defuse.
5. This demonstrates `sum-of-deltas != 0`: the Verifier's ledger balance is fully restored while the assets are irrevocably gone from custody at `M`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** contracts/defuse/src/contract/tokens/nep245/withdraw.rs (L154-197)
```rust
    #[private]
    pub fn do_mt_withdraw(withdraw: MtWithdraw) -> Promise {
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

        let p = ext_mt_core::ext_on(p)
            .with_attached_deposit(NearToken::from_yoctonear(1))
            .with_static_gas(min_gas)
            // distribute remaining gas here
            .with_unused_gas_weight(1);
        let amounts: Vec<U128> = withdraw.amounts.into_iter().map(U128).collect();
        if let Some(msg) = withdraw.msg {
            p.mt_batch_transfer_call(
                withdraw.receiver_id,
                withdraw.token_ids,
                amounts,
                None,
                withdraw.memo,
                msg,
            )
        } else {
            p.mt_batch_transfer(
                withdraw.receiver_id,
                withdraw.token_ids,
                amounts,
                None,
                withdraw.memo,
            )
        }
    }
```

**File:** contracts/defuse/src/contract/tokens/nep245/withdraw.rs (L216-225)
```rust
        let mut used = if is_call {
            // `mt_batch_transfer_call` returns successfully transferred amounts
            match promise_result_checked_json_with_len::<Vec<U128>>(0, amounts.len()) {
                Ok(Ok(used)) if used.len() == amounts.len() => used,
                Ok(_) => vec![U128(0); amounts.len()],
                // do not refund on failed `mt_batch_transfer_call` due to
                // NEP-141 vulnerability: `mt_resolve_transfer` fails to
                // read result of `mt_on_transfer` due to insufficient gas
                Err(_) => amounts.clone(),
            }
```

**File:** contracts/defuse/src/contract/tokens/nep245/withdraw.rs (L235-253)
```rust
        self.deposit(
            sender_id,
            token_ids
                .into_iter()
                .zip(amounts)
                .zip(&mut used)
                .filter_map(|((token_id, amount), used)| {
                    // update min during iteration
                    used.0 = used.0.min(amount.0);
                    let refund = amount.0.saturating_sub(used.0);
                    if refund > 0 {
                        Some((Nep245TokenId::new(token.clone(), token_id).into(), refund))
                    } else {
                        None
                    }
                }),
            Some(REFUND_MEMO),
        )
        .unwrap_or_else(|err| err.panic());
```

**File:** contracts/defuse/src/contract/tokens/mod.rs (L18-74)
```rust
    pub(crate) fn deposit(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
        memo: Option<&str>,
    ) -> Result<()> {
        let owner = self
            .storage
            .accounts
            .get_or_create(owner_id.clone())
            // deposits are allowed for locked accounts
            .as_inner_unchecked_mut();

        let mut mint_event = MtMintEvent {
            owner_id: owner_id.into(),
            token_ids: Vec::new().into(),
            amounts: Vec::new().into(),
            memo: memo.map(Into::into),
        };

        for (token_id, amount) in tokens {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            mint_event.token_ids.to_mut().push(token_id.to_string());
            mint_event.amounts.to_mut().push(amount);

            let total_supply = self
                .storage
                .state
                .total_supplies
                .add(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;
            match token_id {
                TokenId::Nep171(ref tid) => {
                    if total_supply > 1 {
                        return Err(DefuseError::NftAlreadyDeposited(tid.clone()));
                    }
                }
                TokenId::Nep141(_) | TokenId::Nep245(_) | TokenId::Imt(_) => {}
            }

            owner
                .token_balances
                .add(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }

        if !mint_event.amounts.is_empty() {
            MtEvent::MtMint([mint_event].as_slice().into())
                .check_refund()?
                .emit();
        }

        Ok(())
    }
```

**File:** crates/near/utils/src/promise.rs (L54-62)
```rust
#[inline]
pub fn promise_result_checked_json_with_len<T: MaxJsonLength<Args = (usize, ())>>(
    result_idx: u64,
    length: usize,
) -> PromiseJsonResult<T> {
    let max_len = T::max_json_length_root((length, ()));
    let value = env::promise_result_checked(result_idx, max_len)?;
    Ok(serde_json::from_slice::<T>(&value))
}
```
