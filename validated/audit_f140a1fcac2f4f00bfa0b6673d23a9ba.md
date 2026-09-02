Based on my analysis, this is confirmed as a valid vulnerability. The full trace confirms the exploit mechanism.

The binding broken: `attacker's_NFT_custody_claim (Verifier internal balance for core_token_id)` should equal `physical_NFT_custody_by_Verifier`. The exploit causes the Verifier to credit the attacker's internal balance for the NFT's `core_token_id` via `nft_resolve_withdraw`'s refund path [1](#0-0)  while the physical NFT has already been returned to the attacker by the underlying NFT contract's `nft_resolve_transfer` (external, standard NEP-171 behavior), triggered because `nft_on_transfer` returns `PromiseOrValue::Value(false)` synchronously without awaiting the detached `execute_intents` call [2](#0-1) .

The causal chain: `nft_on_transfer` credits the attacker's balance for `core_token_id`, then for `refund_if_fails: false` fires `execute_intents` via `.detach()` (fire-and-forget) and immediately returns `Value(false)` [3](#0-2) . Since this return happens without waiting on the detached promise, the NFT contract's own `nft_resolve_transfer` callback (attached to the original `nft_transfer_call`, requiring only one causal hop) resolves first, sees the "unused/false" result, and — since the NFT is still owned by the Verifier at that point — sends it back to the attacker. Only afterward does the detached `execute_intents` receipt process the signed `NftWithdraw`, debiting the attacker's internal balance via `internal_sub_balance` [4](#0-3)  and scheduling `do_nft_withdraw` → `nft_transfer` [5](#0-4) . Because the Verifier no longer owns the NFT (it was already sent back to the attacker), this `nft_transfer` call fails/panics at the NFT contract, and `nft_resolve_withdraw` sees the failed promise result and re-credits (refunds) the attacker's balance for that same `core_token_id` [6](#0-5) .

Net result: attacker ends up holding the physical NFT (returned via the external contract's standard refund logic) **and** an unbacked internal Verifier balance of 1 unit for that NFT's `core_token_id` (from the failed withdraw's refund), which they can transfer, trade, or attempt to redeem again inside the Verifier — a phantom claim the Verifier does not actually custody.

### Title
Reentrant unchecked `execute_intents` on NFT deposit lets attacker double-claim an NFT (physical refund + unbacked internal balance) - (File: contracts/defuse/src/contract/tokens/nep171/deposit.rs)

### Summary
When an attacker deposits an NFT via `nft_transfer_call` with `DepositAction::Execute{ refund_if_fails: false }` containing a signed `NftWithdraw` of the just-deposited token, `nft_on_transfer` fires the `execute_intents` call detached (unawaited) and immediately returns `Value(false)`. This causes the underlying NFT contract's own `nft_resolve_transfer` to race ahead and refund the physical NFT back to the attacker before the detached withdraw executes, causing the withdraw's outbound `nft_transfer` to fail against the NFT contract (since the Verifier no longer owns the token) and triggering `nft_resolve_withdraw`'s refund logic to re-credit the attacker inside the Verifier — leaving the attacker with both the physical NFT and an unbacked internal balance claim.

### Finding Description
Binding broken: `Verifier_internal_balance(core_token_id) == 1` iff `Verifier_physically_custodies_NFT(core_token_id)`. This invariant is violated.

Code path:
1. `nft_on_transfer` credits `receiver_id` (attacker) with 1 unit of `core_token_id` via `self.deposit(...)` [7](#0-6) .
2. With `refund_if_fails: false`, it fires `ext_intents::ext(current_account_id()).execute_intents(execute.execute_intents).detach()` — fire-and-forget, not awaited — then returns `PromiseOrValue::Value(false)` synchronously [3](#0-2) .
3. Because the return value is delivered without waiting for the detached promise, the underlying NFT contract's `nft_resolve_transfer` callback (attached directly to the original `nft_transfer_call`, one causal hop away) resolves first. Per standard NEP-171 semantics, seeing `used=false` and the token still owned by the Verifier, it transfers the NFT physically back to the attacker.
4. Later, the detached `execute_intents` receipt processes the signed `NftWithdraw`, calling `internal_sub_balance` (debiting the attacker's balance, no physical custody check) [4](#0-3) , then schedules `do_nft_withdraw` → outbound `nft_transfer` from the Verifier to an external receiver [8](#0-7) .
5. Since the Verifier no longer owns the NFT (already returned in step 3), this `nft_transfer` call fails on the NFT contract (unauthorized/not owner).
6. `nft_resolve_withdraw` observes the failed promise and re-credits (refunds) the attacker's balance for `core_token_id` inside the Verifier [6](#0-5) .

Root cause: the deposit-time `execute_intents` call for `refund_if_fails: false` is detached without any callback correlating its outcome with the deposit's own resolution, and `nft_on_transfer` returns a definitive synchronous `false` regardless of what the detached execution does. `nft_resolve_withdraw`'s refund logic assumes any failed outbound `nft_transfer` means the NFT was never taken from the Verifier, which is false here — it was taken by the external contract's own refund logic during the exploit's race.

Existing guards do not catch this: `MultiPayload::verify`, nonce/signature checks all succeed legitimately (the attacker validly signs a withdraw of their own balance), and no code path checks whether the Verifier actually still custodies the physical asset before crediting a refund.

### Impact Explanation
The attacker ends up with the physical NFT (via the external NFT contract's standard refund) **and** a phantom internal Verifier balance of 1 unit for that `core_token_id` (from `nft_resolve_withdraw`'s unconditional refund-on-failure). This phantom balance is a real, transferable/tradeable claim inside the Verifier's ledger that is not backed by any custodied asset — a refund/resolver credit that does not match what actually failed to settle, matching the Critical category "a refund or resolver credit that does not match what failed to settle" / "the Verifier owes more than it custodies." The attacker can repeat this once per NFT they own and can transfer the resulting phantom balance to other accounts within the Verifier, or leave it as unbacked debt that surfaces when anyone tries to redeem it later.

### Likelihood Explanation
Preconditions are minimal and fully attacker-controlled: attacker needs to own any NFT and its contract, sign a self-consistent `NftWithdraw` intent for that same token, and call `nft_transfer_call` with `refund_if_fails: false`. No special roles, no victim keys, and the race is deterministic given NEAR's causal receipt ordering (the direct `nft_on_transfer → nft_resolve_transfer` callback chain has fewer causal hops than `nft_on_transfer → detached execute_intents → do_nft_withdraw → nft_transfer → nft_resolve_withdraw`), making this reliably reproducible, not a low-probability race.

### Recommendation
For `DepositAction::Execute` with `refund_if_fails: false` on NFT/MT deposits (non-fungible / single-unit assets), do not return a synchronous `Value(false)`/`Value(0)` immediately; instead await the `execute_intents` promise (or otherwise correlate it) before deciding the NFT deposit's own accept/reject signal to the underlying token contract, so that the external contract's own resolver cannot race ahead of the Verifier's internal withdraw execution. Alternatively, disallow reentrant `execute_intents` that reference the very token/account currently mid-deposit until the deposit's own resolution flow has completed.

### Proof of Concept
`cargo test` (near-workspaces sandbox) plan:
1. Deploy an NFT issuer contract and mint a token to `attacker`.
2. `attacker` signs an `NftWithdraw` intent for that same `token_id`/`token` to an external `receiver_id`.
3. `attacker` calls `nft_transfer_call` on the NFT contract to the Verifier with:
   `msg = DepositMessage{ receiver_id: attacker, action: Some(DepositAction::Execute(ExecuteIntents{ execute_intents: [signed_nft_withdraw], refund_if_fails: false })) }`.
4. Wait for all receipts to finalize (`wait_until::<Final>`).
5. Assert: `nft_issuer_contract.token(token_id).owner_id == attacker` (physical NFT returned to attacker by the NFT contract's own resolver).
6. Assert: `Mt::mt_balance_of(defuse, attacker, core_token_id) == 1` (not `0` as would be expected if the withdraw had succeeded, and not backed by any custodied NFT) — demonstrating the Verifier's ledger claims to owe an NFT it does not custody, while the attacker also physically holds the same NFT.
7. Additionally assert that the outbound `nft_transfer` receipt from the Verifier (part of `do_nft_withdraw`) has a failing `ExecutionStatus`, confirming the refund in `nft_resolve_withdraw` fired due to the race rather than legitimate cancellation.

### Citations

**File:** contracts/defuse/src/contract/tokens/nep171/withdraw.rs (L122-156)
```rust
    pub fn do_nft_withdraw(withdraw: NftWithdraw) -> Promise {
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

        let p = ext_nft_core::ext_on(p)
            .with_attached_deposit(NearToken::from_yoctonear(1))
            .with_static_gas(min_gas)
            // distribute remaining gas here
            .with_unused_gas_weight(1);
        if let Some(msg) = withdraw.msg {
            p.nft_transfer_call(
                withdraw.receiver_id,
                withdraw.token_id,
                None,
                withdraw.memo,
                msg,
            )
        } else {
            p.nft_transfer(withdraw.receiver_id, withdraw.token_id, None, withdraw.memo)
        }
    }
```

**File:** contracts/defuse/src/contract/tokens/nep171/withdraw.rs (L168-193)
```rust
    ) -> bool {
        let used = if is_call {
            // `nft_transfer_call` returns true if token was successfully transferred
            match promise_result_checked_json::<bool>(0) {
                Ok(Ok(used)) => used,
                Ok(Err(_deserialization_err)) => false,
                // do not refund on failed `nft_transfer_call` due to
                // NEP-141 vulnerability: `nft_resolve_transfer` fails to
                // read result of `nft_on_transfer` due to insufficient gas
                Err(_) => true,
            }
        } else {
            // `nft_transfer` returns empty result on success
            promise_result_checked_void(0).is_ok()
        };

        if !used {
            self.deposit(
                sender_id,
                [(Nep171TokenId::new(token, token_id).into(), 1)],
                Some(REFUND_MEMO),
            )
            .unwrap_or_else(|err| err.panic());
        }

        used
```

**File:** contracts/defuse/src/contract/tokens/nep171/deposit.rs (L49-58)
```rust
        self.deposit(
            receiver_id.clone(),
            [(core_token_id.clone(), 1)],
            Some("deposit"),
        )
        .unwrap_or_else(|err| err.panic());

        let Some(action) = action else {
            return PromiseOrValue::Value(false);
        };
```

**File:** contracts/defuse/src/contract/tokens/nep171/deposit.rs (L76-90)
```rust
            DepositAction::Execute(execute) => {
                if !execute.execute_intents.is_empty() {
                    if execute.refund_if_fails {
                        self.execute_intents(execute.execute_intents);
                    } else {
                        ext_intents::ext(env::current_account_id())
                            .execute_intents(execute.execute_intents)
                            .detach();
                    }
                }

                PromiseOrValue::Value(false)
            }
        }
    }
```

**File:** contracts/defuse/core/src/engine/state/cached.rs (L273-287)
```rust
    fn nft_withdraw(&mut self, owner_id: &AccountIdRef, withdraw: NftWithdraw) -> Result<()> {
        self.internal_sub_balance(
            owner_id,
            std::iter::once((
                Nep171TokenId::new(withdraw.token.clone(), withdraw.token_id.clone()).into(),
                1,
            ))
            .chain(withdraw.storage_deposit.map(|amount| {
                (
                    Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                    amount.as_yoctonear(),
                )
            })),
        )
    }
```
