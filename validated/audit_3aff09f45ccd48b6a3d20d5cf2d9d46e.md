Based on my analysis of `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs` and `internal.rs`, I found a state-consistency issue in the fee/nonce sequencing of the `EOABaseTokenTransfer` "address_check" path that is analogous to the ERC4626 bug class (an accounting side-effect committed before the outcome of an unresolved async step is known, allowing the same signed input to be reprocessed and the side-effect paid out repeatedly).

### Title
Relayer fee is paid before address-registrar verification completes, while nonce is withheld, allowing repeated fee extraction on the same signed transaction - (File: runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs, runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs)

### Summary
In `inner_rlp_execute`, when a transaction is classified as `EOABaseTokenTransfer { address_check: Some(address), fee }`, the relayer fee-refund promise is dispatched immediately and unconditionally (if `fee != 0`), before the async registrar lookup (`address_check_callback`) determines whether the transaction is actually valid. Because the nonce is deliberately *not* incremented for this transaction kind (to allow legitimate retries after relayer faults), a relayer that repeatedly submits the same signed transaction whenever the callback resolves to the "Invalid target" branch can collect the fee refund on each submission without the nonce ever advancing.

### Finding Description
`inner_rlp_execute` (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:330-410`) explicitly skips the nonce increment for `EOABaseTokenTransfer { address_check: Some(_), .. }`: [1](#0-0) 

Immediately after, the fee-refund promise is created eagerly and unconditionally for both `EOABaseTokenTransfer` and `ERC20Transfer` kinds, regardless of the address-check outcome that has not yet been resolved: [2](#0-1) 

The transaction then proceeds to `address_registrar.lookup(address).then(ext.address_check_callback(...))` (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:412-432`). In `address_check_callback`, if the registrar resolves `Some(account_id)` (meaning the target really is a named account) and the caller is not the wallet itself, the call is rejected as `"Invalid target: target is address corresponding to existing named account_id"` — but this happens *after* the fee has already been paid, and *without* incrementing the nonce: [3](#0-2) 

Because the nonce is unchanged, the identical signed Ethereum transaction remains valid for resubmission (its `tx.nonce` still equals `expected_nonce`, checked in `validate_tx_relayer_data`, `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs:352-359`). The only guard against concurrent resubmission is the `has_in_flight_tx` flag, which is reset to `false` once each attempt's callback resolves (`self.has_in_flight_tx = false;` at `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:140`), so nothing prevents a relayer from resubmitting the same transaction on the next receipt and collecting the fee again.

This mirrors the ERC4626 root cause structurally: an accounting effect (minting shares / here, paying a fee) is performed based on a check that is finalized asynchronously, and the "has this input already been consumed" bookkeeping (`_totalSupply` there, `nonce` here) is updated inconsistently with respect to the side-effect, permitting the side-effect to recur on a logically-unconsumed input.

### Impact Explanation
A malicious or compromised relayer can drain a user's eth-implicit wallet balance by resubmitting the same valid signed transaction whenever it can force the address-check branch to resolve as "Invalid target" (e.g., by choosing a `target` for which the address happens to already be registered to a different named account while not itself being the wallet's own signer). Each resubmission triggers another fee-refund transfer out of the wallet contract's balance without ever consuming the user's nonce, i.e., repeated unauthorized fund extraction (theft of funds) from an ordinary user's wallet contract, reachable purely by an unprivileged relayer submitting transactions — no special privileges required.

### Likelihood Explanation
This requires: (1) a transaction whose target maps to `TargetKind::EthImplicit(address)` with the address genuinely registered to a *different* existing named account in the address registrar (a state a relayer does not control directly but can potentially discover or wait for), and (2) `fee != 0` for the transaction. This is a narrower, relayer-dependent condition rather than something any signer can trivially trigger at will, so likelihood is moderate — it depends on registrar state at call time. I was not able to fully verify from the available code whether an attacker-controlled relayer can force this exact "registered to someone else" outcome, or whether other invariants elsewhere (not visible to me from the indexed content) prevent repeat submissions of the exact same signed payload in practice.

### Recommendation
Do not dispatch the fee-refund promise until after the async address-check callback confirms the transaction target is valid, or extend the "in-flight" bookkeeping to preclude fee payment prior to nonce commitment. Alternatively, move fee payment to inside `address_check_callback`'s success branch (the `None` branch, where the nonce is also incremented), so the fee and the nonce increment are atomic with respect to the same execution path.

### Proof of Concept
Not independently executed; derived purely from static code-path analysis of `inner_rlp_execute` → `address_check_callback` in `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs` and `internal.rs`. A concrete PoC would require deploying the wallet contract plus an address-registrar mock in a sandbox test and repeatedly calling `rlp_execute` with a target address that resolves via the registrar to `Some(existing_account)` while `env::signer_account_id() != current_account_id`, observing the fee transfer recurring on each call while `get_nonce` remains unchanged. I did not have execution access to confirm this in a live sandbox test.

### Citations

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L358-365)
```rust
            if let TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
                address_check: Some(_),
                ..
            }) = &transaction_kind
            {
            } else {
                *nonce = nonce.saturating_add(1);
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L374-385)
```rust
            if let TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
                fee,
                ..
            })
            | TransactionKind::EthEmulation(EthEmulationKind::ERC20Transfer { fee, .. }) =
                &transaction_kind
            {
                if !fee.is_zero() && context.predecessor_account_id != context.current_account_id {
                    let refund_promise = env::promise_batch_create(&context.predecessor_account_id);
                    env::promise_batch_action_transfer(refund_promise, *fee);
                }
            }
```
