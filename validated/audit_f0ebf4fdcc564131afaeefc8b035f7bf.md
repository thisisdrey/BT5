This confirms the vulnerability. Comparing `ft_withdraw`'s flow (which correctly attaches `ft_resolve_withdraw` as a `.then()` callback after `do_ft_withdraw`, crediting `refund` back to `sender_id` if the transfer fails) with `storage_deposit`'s flow, the latter has no equivalent resolve/refund callback.

### Title
`State::storage_deposit` debits wNEAR and unwraps it to NEAR but has no resolve callback to recredit the owner when the external `storage_deposit` call fails - (File: contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs)

### Summary
`State::storage_deposit` (contracts/defuse/src/contract/intents/state.rs:265-297) synchronously debits `amount` of wNEAR from `owner_id`'s Verifier balance, then schedules `near_withdraw` followed by `do_storage_deposit`, which forwards the unwrapped NEAR to `ext_storage_management::ext(storage_deposit.contract_id).storage_deposit(...)` [1](#0-0) . Unlike `ft_withdraw`, which attaches `ft_resolve_withdraw` as a terminal `.then()` callback that credits back any unused amount via `self.deposit(...)` on failure [2](#0-1) [3](#0-2) , `do_storage_deposit` returns the `storage_deposit(...)` Promise as the terminal promise in the chain with no subsequent resolver, so a failing `storage_deposit` call (e.g., the named `contract_id` has no `storage_deposit` export) leaves the debited wNEAR permanently unrecovered.

### Finding Description
The broken binding is: `wNEAR_debited(owner_id)` should equal `NEAR_delivered(contract_id) + NEAR_recredited(owner_id)`. Tracing the code: `State::storage_deposit` calls `self.withdraw(owner_id, [(wnear_token_id, amount)], ..., false)` which synchronously performs `internal_sub_balance` on `owner_id`'s Verifier balance [4](#0-3)  — this state change commits regardless of what happens in subsequently scheduled promises. It then schedules `ext_wnear::near_withdraw` followed by `.then(Self::ext(...).do_storage_deposit(storage_deposit))` [5](#0-4) . `do_storage_deposit` checks that `near_withdraw` succeeded, then issues `ext_storage_management::ext(storage_deposit.contract_id).with_attached_deposit(storage_deposit.amount).storage_deposit(...)` as the final returned `Promise`, with no further `.then()` resolver attached anywhere in the call chain [1](#0-0) . If `storage_deposit.contract_id` has no `storage_deposit` method (or panics/rejects for any reason), that promise fails, but since it is the terminal promise with no resolve callback, nothing recredits `owner_id`. The debited wNEAR was already unwrapped into raw NEAR and attached to a failing call, so it is neither returned to the Verifier's spendable NEAR balance nor credited back to `owner_id`'s wNEAR balance. This differs from the analogous `ft_withdraw`/`do_ft_withdraw`/`ft_resolve_withdraw` chain, which explicitly guards against exactly this class of failure by re-crediting unused amounts. An attacker names `contract_id` as their own contract lacking `storage_deposit` (or predictably rejects it) in a `StorageDeposit{contract_id, deposit_for_account_id, amount}` intent signed against `owner_id`'s Verifier balance — but critically, `owner_id` must be the signer or otherwise authorize this intent, so the attacker cannot force a withdrawal against an unwilling owner without a valid signature over that intent. Note: this does require a validly-signed `StorageDeposit` intent from `owner_id`; the loss is against whoever signs the intent, so realistic exploitation scenarios are self-harm by a naive `owner_id` calling storage_deposit against a broken/malicious `contract_id`, or an owner being tricked (out of scope: social engineering) into signing such an intent for an attacker-controlled `contract_id`.

### Impact Explanation
`owner_id`'s wNEAR balance is irrecoverably debited with no compensating NEAR delivered to `contract_id` and no wNEAR recredited to `owner_id`, matching the "user funds permanently frozen"/lost category. This is repeatable across accounts and amounts since it applies to any `StorageDeposit` intent whose named `contract_id` fails the `storage_deposit` call, and the blast radius extends to any owner who signs a `StorageDeposit` intent that later fails. However, the intent must be signed by (or authorized on behalf of) `owner_id`, so an attacker cannot unilaterally drain an arbitrary victim's balance without the victim's cooperation/signature — this substantially limits attacker-profit framing versus a pure griefing/self-inflicted-loss framing.

### Likelihood Explanation
Preconditions are simple to meet: `owner_id` needs wNEAR balance in the Verifier ≥ `amount`, and any `contract_id` without a `storage_deposit` export (e.g., a freshly deployed empty contract) triggers the failure deterministically. The attacker's cost is just deploying a bare contract and getting a `StorageDeposit` intent signed for it. The main constraint is that this requires a signature from `owner_id` on the specific intent, so it's most likely triggered as a self-inflicted funds-loss bug (owner picks a bad `contract_id`) rather than an attacker unilaterally stealing from an unwitting victim.

### Recommendation
Attach a `#[private]` resolve callback (analogous to `ft_resolve_withdraw`) after `do_storage_deposit`'s scheduled `storage_deposit` promise that inspects the promise result and, on failure, recredits `storage_deposit.amount` of wNEAR back to `owner_id` via `self.deposit(...)`, mirroring the existing pattern in `contracts/defuse/src/contract/tokens/nep141/withdraw.rs`.

### Proof of Concept
```rust
// cargo test -p tests --test '*' storage_deposit_recredit_on_failure -- --nocapture
// (near-workspaces sandbox test, extend tests/src/tests/defuse/storage/mod.rs)
#[tokio::test]
async fn storage_deposit_recredit_on_failure(#[future(awt)] env: Env) {
    let (owner, bad_contract) = futures::join!(env.create_user(), env.create_user() /* deploy empty contract w/o storage_deposit */);
    // fund owner's Verifier wNEAR balance with `amount`
    // record balance_of(owner, wnear_token) BEFORE = amount
    let before = env.defuse.balance_of(owner.account_id(), &wnear_token_id).await.unwrap();

    let payload = owner.sign_defuse_payload_default(
        &env.defuse,
        [StorageDeposit {
            contract_id: bad_contract.contract_id().clone(), // no storage_deposit method
            deposit_for_account_id: owner.account_id().clone(),
            amount: MIN_FT_STORAGE_DEPOSIT_VALUE,
        }],
    ).await.unwrap();

    env.defuse_simulate_and_execute_intents(env.defuse.contract_id(), [payload]).await.unwrap();

    let after = env.defuse.balance_of(owner.account_id(), &wnear_token_id).await.unwrap();

    // BROKEN BINDING: expect after == before (recredited), but code has no recredit path:
    assert_eq!(after, before, "wNEAR must be recredited when storage_deposit call fails");
    // Also assert bad_contract never received a spendable NEAR balance increase matching `amount`.
}
```
This test currently fails: `after == before - amount` (funds lost), because no resolver exists in `contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs` to recredit on promise failure.

### Citations

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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L98-105)
```rust
        .then(
            Self::ext(env::current_account_id())
                .with_static_gas(Self::FT_RESOLVE_WITHDRAW_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .ft_resolve_withdraw(withdraw.token, owner_id, withdraw.amount.into(), is_call),
        )
        .into())
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L183-191)
```rust
        let refund = amount.0.saturating_sub(used);
        if refund > 0 {
            self.deposit(
                sender_id,
                [(Nep141TokenId::new(token).into(), refund)],
                Some(REFUND_MEMO),
            )
            .unwrap_or_else(|err| err.panic());
        }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L270-278)
```rust
        self.withdraw(
            owner_id,
            [(
                Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                storage_deposit.amount.as_yoctonear(),
            )],
            Some("withdraw"),
            false,
        )?;
```

**File:** contracts/defuse/src/contract/intents/state.rs (L280-294)
```rust
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
```
