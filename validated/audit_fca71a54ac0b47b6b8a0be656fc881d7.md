### Title
Permanent freeze of ETH-implicit wallet funds via un-recoverable `has_in_flight_tx` flag when the relayer-ban promise batch fails - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The Wallet Contract that backs every ETH-implicit account gates all execution behind a `has_in_flight_tx` boolean. When a faulty relayer is detected, the contract fires a single unchained `Promise` batch (`DeleteKey` + `function_call_weight("ban_relayer")`) that is supposed to revoke the bad key and reset the flag. If that batch fails for any reason, there is no `.then()` callback to reset the flag, so `has_in_flight_tx` is permanently stuck at `true` and the wallet can never execute `rlp_execute` again, freezing any NEAR held by the account forever.

### Finding Description
`rlp_execute` refuses to do anything while `has_in_flight_tx` is `true`: [1](#0-0) 

When a faulty relayer is detected (either directly in `rlp_execute` or in `address_check_callback`), the contract creates a "ban" promise and immediately marks the account busy, but crucially returns the promise *directly*, without chaining a `.then()` callback that could ever reset the flag on failure: [2](#0-1) [3](#0-2) 

The "ban" promise itself is a single batch containing `delete_key(pk)` followed by a weighted `function_call_weight` to `ban_relayer`, which is the *only* code path in the whole contract that ever resets `has_in_flight_tx` back to `false`: [4](#0-3) [5](#0-4) 

Because `delete_key` and `function_call_weight` are two actions in the *same* action receipt, they are atomic: if either action fails, the whole receipt fails and none of the state-resetting logic in `ban_relayer` ever runs. Two concrete ways to make that batch fail with a legitimate, unprivileged transaction:
- Attach insufficient prepaid gas to the `rlp_execute` call that triggers the "faulty relayer" branch. `function_call_weight` distributes only the *unused* gas of the receipt by weight; if little/no gas remains after parsing/registrar-lookup logic runs, `ban_relayer`'s function call receives ~0 gas and fails with "out of gas" even though `delete_key` itself succeeds.
- Any other transient failure of the `DeleteKey` or weighted `FunctionCall` action (e.g. exceeding max gas burnt per receipt at exactly this point) has the same effect.

Once this happens, `has_in_flight_tx` is left at `true` permanently: there is no external method, no owner full-access key (ETH-implicit accounts can never get one, per design), and no `DeleteAccount` capability for these accounts, so nothing can ever flip the flag back. Every subsequent call to `rlp_execute` — from any relayer, including the legitimate account owner — immediately short-circuits with `"transaction already in progress"` and never reaches contract logic again.

### Impact Explanation
This permanently and irrecoverably freezes the entire NEAR balance (and any assets reachable only through `rlp_execute`, e.g. NEP-141 tokens) held by the affected ETH-implicit account. Since ETH-implicit accounts have no other way to authorize actions (no full-access key can ever be added, and the account cannot be deleted), the funds are locked forever with no admin or protocol-level remedy — directly analogous to the Malt finding, where an intended "recovery/escape" path (the relayer-ban recovery mechanism here) itself reverts and permanently locks user funds instead of restoring normal operation.

### Likelihood Explanation
Reaching the "faulty relayer" branch only requires a permissionless third party acting as a relayer (this is explicitly a permissionless role by design — "any relayer to safely serve base token transfers from any wallet") to submit a malformed/invalid target `rlp_execute` transaction. Controlling the attached prepaid gas on that transaction (a normal, unprivileged parameter any caller sets) is sufficient to starve the internal ban-promise batch of gas. No validator, protocol, or contract-owner privilege is required — an ordinary NEAR transaction from any account can trigger this against any ETH-implicit account it targets (including its own, if the attacker is the account owner acting as a self-relayer, or a hostile relayer targeting a victim's wallet).

### Recommendation
Chain the relayer-ban promise with a `.then()` callback (mirroring the `rlp_execute_callback` pattern already used elsewhere) so that regardless of whether `delete_key`/`ban_relayer` succeeds or fails, a subsequent private callback method unconditionally resets `has_in_flight_tx = false`. Additionally, ensure the `function_call_weight` gas allocation to `ban_relayer` has a guaranteed minimum static gas reservation independent of "weighted leftover gas," so it cannot be starved by low prepaid gas on the originating transaction.

### Proof of Concept
1. Deploy/derive an ETH-implicit account and its Wallet Contract as in `test_wallet_contract_interaction` (`integration-tests/src/tests/features/wallet_contract.rs`).
2. As an unprivileged relayer, craft an RLP-encoded Ethereum transaction whose `target`/payload deterministically causes `inner_rlp_execute` to return `Err(Error::Relayer(_))` (e.g. an invalid `target` mismatch, as exercised by `test_relayer_invalid_target` in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs`).
3. Submit the wrapping NEAR `FunctionCall` to `rlp_execute` with `env::signer_account_id() == current_account_id` (i.e., using the wallet's own access key granted to the relayer) and with prepaid gas set just high enough to run the parsing/error path but leave (near) zero unused gas for the resulting `delete_key` + `function_call_weight("ban_relayer")` batch.
4. Observe: `delete_key` executes and revokes the relayer key, but the weighted `ban_relayer` call fails for lack of gas, so the whole receipt fails; `has_in_flight_tx` remains `true` (it was never reset because `rlp_execute`/`address_check_callback` return the ban promise without a `.then()` callback).
5. Any further call to `rlp_execute` on this account, from any relayer with any valid key, immediately returns `"Error: transaction already in progress, please try again later."` forever, with no way to reset the flag — permanently freezing the account's funds.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L94-105)
```rust
        // To ensure user actions are executed in the desired order,
        // having multiple transactions in flight at the same time is
        // not allowed.
        if self.has_in_flight_tx {
            return PromiseOrValue::Value(ExecuteResponse {
                success: false,
                success_value: None,
                error: Some(
                    "Error: transaction already in progress, please try again later.".into(),
                ),
            });
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L121-127)
```rust
            Err(Error::Relayer(_)) if env::signer_account_id() == current_account_id => {
                let promise = create_ban_relayer_promise(current_account_id);
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(e) => PromiseOrValue::Value(e.into()),
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L160-192)
```rust
        let current_account_id = env::current_account_id();
        let promise = if maybe_account_id.is_some() {
            // We intentionally do not increment the nonce in this case because the
            // error is caused by a faulty relayer, not the user. An honest relayer
            // may still be able to successfully send the user's intended transaction.
            if env::signer_account_id() == current_account_id {
                create_ban_relayer_promise(current_account_id)
            } else {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Invalid target: target is address corresponding to existing named account_id".into()),
                });
            }
        } else {
            // We must increment the nonce at this point to prevent replay of the transaction.
            // Recall that the nonce was not incremented in `inner_rlp_execute` in the case that
            // the registrar contract was called (i.e. in the case we end up inside this callback).
            self.nonce = self.nonce.saturating_add(1);
            let ext =
                WalletContract::ext(current_account_id).with_static_gas(RLP_EXECUTE_CALLBACK_GAS);
            match action_to_promise(target, action)
                .map(|p| p.then(ext.rlp_execute_callback(caller_deposit)))
            {
                Ok(p) => p,
                Err(e) => {
                    return PromiseOrValue::Value(e.into());
                }
            }
        };
        self.has_in_flight_tx = true;
        PromiseOrValue::Promise(promise)
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L319-327)
```rust
    #[private]
    pub fn ban_relayer(&mut self) -> ExecuteResponse {
        self.has_in_flight_tx = false;
        ExecuteResponse {
            success: false,
            success_value: None,
            error: Some("Error: faulty relayer".into()),
        }
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L503-512)
```rust
fn create_ban_relayer_promise(current_account_id: AccountId) -> Promise {
    let pk = env::signer_account_pk();
    Promise::new(current_account_id).delete_key(pk).function_call_weight(
        "ban_relayer".into(),
        Vec::new(),
        NearToken::from_yoctonear(0),
        Gas::from_tgas(1),
        GasWeight(1),
    )
}
```
