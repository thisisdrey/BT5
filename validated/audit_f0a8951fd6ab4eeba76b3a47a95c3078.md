### Title
`native_withdraw` debits wNEAR with no resolver/refund path if `near_withdraw` or the NEAR transfer fails - ([File: contracts/defuse/src/contract/tokens/nep141/native.rs])

### Summary
`native_withdraw` (implemented in `contracts/defuse/src/contract/intents/state.rs`) synchronously subtracts the user's internal wNEAR balance via `self.withdraw(...)` and then fires an asynchronous, **detached** promise chain (`near_withdraw` → `do_native_withdraw`) with **no resolver callback at all**, unlike `ft_withdraw`/`nft_withdraw`/`mt_withdraw`, which each attach a `*_resolve_withdraw` callback that inspects the promise result and re-credits (`self.deposit`) any unused amount. If either the `near_withdraw` call or the final `Promise::new(receiver_id).transfer(amount)` in `do_native_withdraw` fails, the debited wNEAR is never returned to the user.

### Finding Description
The invariant under test is: `wNEAR debited from an account == NEAR that reached the named receiver, or was returned to that account`.

Code path, `native_withdraw` in [1](#0-0) :
1. `self.withdraw(...)` performs `internal_sub_balance` synchronously and irreversibly on the ledger for the user's wNEAR.
2. It then schedules `ext_wnear::near_withdraw(...)` and chains `.then(Self::ext(...).do_native_withdraw(withdraw))`, and finally calls `.detach()` — the resulting `Promise` is discarded, meaning **no callback exists to react to the eventual success or failure of this chain**.

`do_native_withdraw`, [2](#0-1) :
```
#[private]
pub fn do_native_withdraw(withdraw: NativeWithdraw) -> Promise {
    require!(
        promise_result_checked_void(0).is_ok(),
        "near_withdraw failed",
    );
    Promise::new(withdraw.receiver_id).transfer(withdraw.amount)
}
```
- If `near_withdraw` on the wNEAR contract fails (e.g., wNEAR contract paused, insufficient registered balance at the token level due to a race with another concurrent withdrawal of the same token in the same batch, or any other transient failure), the `require!` panics and the entire chain aborts.
- Even if `near_withdraw` succeeds, the terminal action `Promise::new(receiver_id).transfer(amount)` can itself fail (e.g., `receiver_id` does not exist, or is otherwise unable to receive the transfer) and there is still no callback to detect this and credit the user back.

In contrast, every other withdraw path (`ft_withdraw`, `nft_withdraw`, `mt_withdraw`) chains a resolver (`ft_resolve_withdraw`, `nft_resolve_withdraw`, `mt_resolve_withdraw`) that reads the promise result and calls `self.deposit(sender_id, ..., refund)` for whatever was not delivered — see e.g. [3](#0-2) . `native_withdraw` has no such analogue (`grep` for `native_resolve`/`NativeWithdrawResolver` returns no matches in the repo), so the refund mechanism that exists everywhere else for this exact class of failure is simply absent for the native-NEAR path.

The attacker does not even need to control a malicious FT/NFT/MT contract for this specific defect — it is inherent in the `native_withdraw` intent handling itself, triggered by any `MultiPayload` batch (or a deposit `msg` that triggers a nested `native_withdraw` intent) that names a receiver which cannot accept the final NEAR transfer, or by causing the `near_withdraw` call to fail (e.g., via a same-batch double withdrawal of the token that stresses the wNEAR contract's own logic). Because the internal ledger debit in step 1 is committed independently of, and prior to, the success of the cross-contract unwrap/transfer, there is no guard (`checked_*`, callback, or otherwise) preventing the divergence: the wNEAR is gone from the Verifier's internal accounting, but the corresponding NEAR never lands with the receiver and is never returned to the withdrawing account.

### Impact Explanation
User wNEAR balance is permanently destroyed from the Verifier's internal ledger with no compensating NEAR delivered to the named receiver and no refund credited back to the signer's account. This is unrecoverable without a privileged action (e.g. `UnrestrictedWithdrawer`/DAO manually crediting the account), matching "Critical - user funds permanently frozen." It is repeatable per attempt/account/receiver and does not require any privileged role, matching the described unprivileged attacker profile.

### Likelihood Explanation
The attacker only needs a signed `NativeWithdraw` intent (their own) naming a receiver that will make the terminal `Promise::new(receiver_id).transfer(amount)` fail (e.g., a syntactically valid but non-existent named account, since implicit accounts are auto-created by any transfer but a malformed/non-existent named account is not), or a batch that stresses the `near_withdraw` call on the wNEAR contract to fail. No special privileges, roles, or victim keys are required — the attacker can execute this against their own account to demonstrate/repro the loss, and it is directly reachable through `execute_intents`.

### Recommendation
Add a `native_resolve_withdraw` callback analogous to `ft_resolve_withdraw`/`nft_resolve_withdraw`/`mt_resolve_withdraw` that inspects the result of the `near_withdraw` → `do_native_withdraw` chain and calls `self.deposit(owner_id, [(wnear_token_id, amount)], Some(REFUND_MEMO))` whenever the final transfer (or the intermediate `near_withdraw`) did not succeed, instead of `.detach()`-ing the promise with no result handling.

### Proof of Concept
`cargo test` plan (near-workspaces sandbox, extending the existing `tests/src/tests/defuse/intents/native_withdraw.rs`):
1. Deposit wNEAR for a user via `ft_transfer_call` + `DepositMessage`, establishing internal balance `B0` for `wnear_token_id`.
2. Sign and execute a `NativeWithdraw` intent whose `receiver_id` is a syntactically valid NEAR account ID that does not exist on-chain and cannot receive the transfer (or otherwise force `do_native_withdraw`'s `Promise::new(receiver_id).transfer(...)` / the `near_withdraw` call to fail).
3. Assert LHS: `mt_balance_of(defuse, user, wnear_token_id) == B0 - amount` (ledger was debited).
4. Assert RHS: `env.account(receiver_id).amount` did not increase by `amount` (delivery failed), AND `mt_balance_of(defuse, user, wnear_token_id)` was never re-credited back to `B0` (no refund occurred), proving `B0 - amount != NEAR delivered + NEAR refunded`.
5. Confirm no `native_resolve_withdraw`-style event/refund log is emitted, in contrast to the refund event emitted by `ft_resolve_withdraw` in an equivalent failing `ft_withdraw` test.

### Citations

**File:** contracts/defuse/src/contract/intents/state.rs (L212-240)
```rust
    fn native_withdraw(&mut self, owner_id: &AccountIdRef, withdraw: NativeWithdraw) -> Result<()> {
        self.withdraw(
            owner_id,
            [(
                Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                withdraw.amount.as_yoctonear(),
            )],
            Some("withdraw"),
            false,
        )?;

        ext_wnear::ext(self.wnear_id.clone())
            .with_attached_deposit(NearToken::from_yoctonear(1))
            .with_static_gas(NEAR_WITHDRAW_GAS)
            // do not distribute remaining gas here
            .with_unused_gas_weight(0)
            .near_withdraw(U128(withdraw.amount.as_yoctonear()))
            .then(
                // do_native_withdraw only after unwrapping NEAR
                Self::ext(env::current_account_id())
                    .with_static_gas(Self::DO_NATIVE_WITHDRAW_GAS)
                    // do not distribute remaining gas here
                    .with_unused_gas_weight(0)
                    .do_native_withdraw(withdraw),
            )
            .detach();

        Ok(())
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/native.rs (L11-19)
```rust
    #[private]
    pub fn do_native_withdraw(withdraw: NativeWithdraw) -> Promise {
        require!(
            promise_result_checked_void(0).is_ok(),
            "near_withdraw failed",
        );

        Promise::new(withdraw.receiver_id).transfer(withdraw.amount)
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
