### Title
`mt_resolve_withdraw` refunds the full withdrawn amount based on an untrusted, attacker-controlled `token` contract's malformed `used` response even though that same contract already delivered the assets to `receiver_id` - ([File: contracts/defuse/src/contract/tokens/nep245/withdraw.rs])

### Summary
`mt_resolve_withdraw` decides whether to refund a withdrawal back to the sender purely by inspecting the JSON shape of the promise result returned by the external `token` MT contract's `mt_batch_transfer_call`. Because `token` and `receiver_id` can both be controlled by the same unprivileged attacker, the attacker's own contract can genuinely deliver the tokens to `receiver_id` in its internal ledger while deliberately returning a `Vec<U128>` whose length does not match `amounts.len()`, forcing the Verifier's `Ok(_) => vec![U128(0); amounts.len()]` branch and a full refund of the already-debited internal balance.

### Finding Description
The broken binding: for a withdrawal of amount `D` (already synchronously debited from `sender_id`'s internal Defuse balance and from `total_supplies` in `Contract::withdraw`, [1](#0-0) ), the Verifier's refund `R` computed in `mt_resolve_withdraw` and the amount `A` actually delivered to `receiver_id` as recorded by the external `token` contract's own ledger must satisfy `R + A == D`. Instead, when `token` is attacker-controlled, the attacker can make both `R == D` and `A == D` simultaneously.

Path:
1. `internal_mt_withdraw` calls `self.withdraw(...)`, which synchronously subtracts `amounts` from `sender_id`'s internal balance and from `total_supplies` for `TokenId::Nep245(token, token_id)`, before any cross-contract call happens: [2](#0-1) .
2. It then schedules `do_mt_withdraw`, which calls `token.mt_batch_transfer_call(receiver_id, token_ids, amounts, ..., msg)` — an ordinary NEAR cross-contract call into whatever contract the attacker specified as `token`: [3](#0-2) .
3. `mt_resolve_withdraw` reads the promise result of that call and, whenever it parses as valid JSON but with the wrong length, treats the whole transfer as if nothing was used, refunding the entire `amounts` back into `sender_id`'s internal balance: [4](#0-3) .

Because `token` is an arbitrary account supplied by the withdrawer (no whitelist/registration is enforced on which MT contracts can be used for `mt_withdraw`), and `mt_batch_transfer_call`'s actual token movement inside `token`'s own contract state is entirely decided by `token`'s own code, an attacker who deploys `token` can:
- Execute a genuine internal transfer crediting `receiver_id` (an account the attacker also controls) inside `token`'s ledger.
- Return an intentionally mis-sized `Vec<U128>` (e.g. one element too many/few) from that same call.

`mt_resolve_withdraw`'s `Ok(_) => vec![U128(0); amounts.len()]` branch cannot distinguish "the call succeeded with garbage output because the token is buggy/non-compliant" from "the call succeeded, tokens were delivered, and the mis-shaped output is a deliberate lie" — both are treated identically as "nothing used, refund everything." The `Err(_) => amounts.clone()` branch's comment even documents that the authors are aware external token behavior can't be trusted for the failed-promise case, but the same trust assumption is missing for the `Ok(_)`-but-wrong-length case.

The result: `sender_id`'s Defuse-internal balance and `total_supplies` for `TokenId::Nep245(token, token_id)` are restored to `D` (as if the withdrawal never happened), while `receiver_id` (also attacker-controlled) simultaneously and irreversibly holds `A = D` worth of real balance inside `token`'s own ledger. No existing guard (`token_ids.len() == amounts.len()` `require!`, `promise_result_checked_json_with_len`, `#[private]`) checks that the *content* of a well-formed-but-wrong-length success response is consistent with what the external contract actually did — they only validate JSON shape, not economic truth, which is exactly what an attacker-controlled `token` contract can falsify.

### Impact Explanation
This breaks the Verifier's core solvency invariant for the specific `TokenId::Nep245(token, *)` namespace: internal balance/`total_supplies` no longer track real custody, since a refund is credited without a matching failure. If any other account also holds internally-tracked balance for that same `TokenId` (e.g., a victim who was induced to deposit from the same `token` contract, believing it has value), the attacker's fabricated balance can be used in ordinary intents (transfers/swaps settled by `TransferMatcher::finalize`) to extract real assets from that victim, since the Verifier treats the phantom balance as legitimate spendable balance. This matches the Critical category "a refund or resolver credit that does not match what failed to settle" / "a batch whose balance changes do not net to zero so the Verifier owes more than it custodies." The attacker gains a duplicated, unbacked balance credit repeatable on every withdrawal against any `token` contract the attacker deploys and controls.

### Likelihood Explanation
Preconditions are fully within an unprivileged attacker's capability per the rules: deploy and control an MT (`nep245`) contract to use as `token`, hold Defuse balance for that token type (via ordinary deposit, e.g. `mt_transfer_call` into Defuse), and call `mt_withdraw`/`mt_force_withdraw`-equivalent path (`internal_mt_withdraw`) with `msg` set (`is_call = true`) and `receiver_id` set to an account they also control. No relayer key, DAO role, or victim key is required. The attack is deterministic and repeatable on every withdrawal call. The only limiting factor is that the fabricated balance's real-world value depends on other users trusting/holding the same `TokenId`, but the internal accounting break itself is unconditional and immediate.

### Recommendation
Do not treat a well-formed-but-wrong-length success response from `mt_batch_transfer_call` as full failure with a blanket refund. Either: (a) refuse to withdraw/refund at all and require `used.len() == amounts.len()` strictly, aborting the whole flow (panicking or leaving funds "stuck" pending manual resolution) rather than crediting an unconditional refund on ambiguous output, since a full refund is only safe when it is *provable* that the external transfer did not occur; or (b) require withdrawals to declared/whitelisted token contracts with a well-defined trust model, so that malformed responses can be attributed to bugs rather than malice; or (c) track the debited amount as "pending" rather than immediately available for refund until the external call's outcome can be corroborated in a way that can't be forged.

### Proof of Concept
`cargo test` (near-workspaces sandbox) plan:
1. Deploy a malicious NEP-245 "EvilToken" contract implementing `mt_batch_transfer_call`: on invocation it (a) subtracts `amounts` from the Verifier's account and adds to `receiver_id`'s account in EvilToken's own internal ledger (a real transfer within EvilToken), then (b) returns a `Vec<U128>` of length `amounts.len() + 1` (or `0`) instead of `amounts.len()`.
2. Deposit `D = 1000` of `TokenId::Nep245(evil_token, "x")` into the Verifier for `attacker` (via a normal `mt_transfer_call`/deposit flow), giving the Verifier a matching real balance of `1000` on EvilToken.
3. As `attacker`, call `mt_withdraw` with `token = evil_token`, `receiver_id = attacker_receiver` (also attacker-controlled), `amounts = [1000]`, `msg = Some(...)`.
4. After the promise chain resolves, assert:
   - Verifier-internal balance of `attacker` for `TokenId::Nep245(evil_token, "x")` == `1000` (refunded via `mt_resolve_withdraw`'s `Ok(_) => vec![U128(0); ...]` branch).
   - EvilToken's own ledger shows `attacker_receiver` balance == `1000` (delivered).
   - Assert `(Verifier refund total) + (delivered total per EvilToken's ledger) > D`, i.e. `1000 + 1000 > 1000`, proving the double credit and violation of `R + A == D`.

### Citations

**File:** contracts/defuse/src/contract/tokens/mod.rs (L99-117)
```rust
        for (token_id, amount) in token_amounts {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            burn_event.token_ids.to_mut().push(token_id.to_string());
            burn_event.amounts.to_mut().push(amount);

            owner
                .token_balances
                .sub(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;

            self.storage
                .state
                .total_supplies
                .sub(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }
```

**File:** contracts/defuse/src/contract/tokens/nep245/withdraw.rs (L59-87)
```rust
impl Contract {
    pub(crate) fn internal_mt_withdraw(
        &mut self,
        owner_id: AccountId,
        withdraw: MtWithdraw,
        force: bool,
    ) -> Result<PromiseOrValue<Vec<U128>>> {
        if withdraw.token_ids.len() != withdraw.amounts.len() || withdraw.token_ids.is_empty() {
            return Err(DefuseError::InvalidIntent);
        }

        self.withdraw(
            &owner_id,
            withdraw
                .token_ids
                .iter()
                .cloned()
                .map(|token_id| Nep245TokenId::new(withdraw.token.clone(), token_id))
                .map(Into::into)
                .zip(withdraw.amounts.iter().copied())
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

**File:** contracts/defuse/src/contract/tokens/nep245/withdraw.rs (L216-253)
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
        } else {
            // `mt_batch_transfer` returns empty result on success
            if promise_result_checked_void(0).is_ok() {
                amounts.clone()
            } else {
                vec![U128(0); amounts.len()]
            }
        };

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
