### Title
Wallet Contract pays relayer fee for `EOABaseTokenTransfer` before address-registrar validation, and skips nonce increment on relayer-fault, enabling unlimited fee-draining replay - (File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs)

### Summary
The ETH-implicit Wallet Contract's ERC20/base-token-transfer emulation pays out a relayer "fee" from the wallet's own balance *before* it has verified that the transaction is actually valid, and in one specific branch (`address_check: Some(_)`) it deliberately does **not** advance the account's nonce when the check later fails due to a faulty/malicious relayer. Because the nonce is unchanged, the exact same signed transaction can be resubmitted by the (malicious) relayer indefinitely, each time collecting the fee again while the underlying action never executes — draining the wallet's NEAR balance without the user's transfer ever completing.

### Finding Description
In `inner_rlp_execute` the fee refund to the relayer/predecessor is sent unconditionally as soon as the transaction is classified as `EOABaseTokenTransfer`/`ERC20Transfer` with a non-zero fee — this happens synchronously, before the actual action promise (or the address-registrar lookup that determines correctness) is even dispatched: [1](#0-0) 

For the specific case where the parsed target is another eth-implicit wallet contract (`TargetKind::EthImplicit`), the code marks `address_check: Some(address)` and — critically — skips incrementing the nonce, on the stated rationale that "we do not yet know if the registrar contract was needed": [2](#0-1) 

The fee, however, is paid out regardless of this pending check, in the same code block quoted above (lines 374-385), because the `if let ... EOABaseTokenTransfer { fee, .. } | ERC20Transfer { fee, .. }` match covers `address_check: Some(_)` as well as `None`.

Later, in `address_check_callback`, if the registrar confirms the target address actually belongs to a named (registered) account — meaning the relayer built the transaction incorrectly/maliciously — the contract only bans the relayer if the transaction was self-submitted (`signer_account_id() == current_account_id`). If it was submitted by an external relayer using a `FunctionCallPermission` key, the callback simply returns a failure `ExecuteResponse` **without banning the relayer and without incrementing the nonce**: [3](#0-2) 

Since the account-registrar state for a given address does not change from one call to the next, and the wallet's nonce was never advanced, the identical signed transaction (`tx_bytes_b64`) can be resubmitted via `rlp_execute` an unbounded number of times. Every resubmission:
1. Passes `validate_tx_relayer_data`'s nonce check because `expected_nonce` is unchanged: [4](#0-3) 
2. Triggers the unconditional fee transfer out of the wallet's balance to the (malicious) relayer's account.
3. Ultimately fails the registrar check again and returns an error, consuming none of the wallet owner's intended nonce/state, so the exploit is fully repeatable.

### Impact Explanation
A relayer holding only a `FunctionCallPermission` access key on the ETH-implicit wallet (an unprivileged, non-full-access key intentionally granted to let relayers pay gas, per `docs/DataStructures/Account.md`) can repeatedly call `rlp_execute` with the same relayer-crafted (or replayed) transaction to continuously extract the `fee` (a `NearToken` amount computed from `max_fee_per_gas * gas_limit` of the emulated Ethereum transaction) from the wallet's NEAR balance, without the user's intended base-token/ERC-20 transfer ever executing. This is a direct, repeatable theft of NEAR from the wallet owner's account, up to full balance drain, triggered purely by an "unprivileged" relayer signer rather than the account owner.

### Likelihood Explanation
The precondition is that the wallet owner has an active `FunctionCallPermission` key held by a relayer (the intended and documented use case for the Wallet Contract to let relayers pay gas), and that the relayer submits (or is tricked/incentivized into repeatedly submitting) a transaction whose `target` resolves to `TargetKind::EthImplicit` for an address that is registered in the address registrar. A malicious relayer fully controls transaction construction and can trivially engineer this condition, then loop calling `rlp_execute` (waiting for `has_in_flight_tx` to clear between calls) to repeatedly harvest the fee. No cryptographic forging is required since the relayer already possesses a valid signed transaction (or, since the fee is defined per Eth-tx fields controlled by the user’s original signature, a malicious relayer with access to the signed bytes can simply replay them).

### Recommendation
- Do not pay out the relayer fee until the underlying action's promise chain has resolved successfully (move fee payment into `rlp_execute_callback`/`address_check_callback` success branches, conditioned on `PromiseResult::Successful`), mirroring the "check actual result before paying" mitigation recommended for ERC-20 transfers.
- In `address_check_callback`, always increment the nonce (or otherwise permanently invalidate the transaction) once a relayer-fault error is confirmed, regardless of whether the signer equals the current account, so the same signed transaction cannot be replayed.
- Consider capping/rate-limiting fee refunds per nonce or requiring the fee to be escrowed only after a successful registrar check.

### Proof of Concept
1. Wallet owner (ETH-implicit account) grants a `FunctionCallPermission` key to a relayer, as documented in `docs/DataStructures/Account.md` (`Wallet Contract` section).
2. Relayer crafts (or captures) an RLP Ethereum transaction whose `to` address resolves, via `parse_target`/`validate_tx_relayer_data`, to `TargetKind::EthImplicit(address)` where `address` is already registered to a named account in the address registrar, and sets a non-zero `max_fee_per_gas * gas_limit` (i.e., non-zero `fee`).
3. Relayer calls `rlp_execute(target, tx_bytes_b64)`. `inner_rlp_execute` immediately schedules the fee transfer to the relayer (`lib.rs:374-385`) and does not increment the nonce (`lib.rs:358-365`) since `address_check: Some(_)`.
4. The registrar lookup resolves `Some(account_id)`; because `env::signer_account_id() != current_account_id` (relayer signer, not owner), `address_check_callback` returns a failure response without banning the relayer and without incrementing the nonce (`lib.rs:160-192`).
5. Relayer repeats step 3 with the identical `tx_bytes_b64` — the nonce check in `validate_tx_relayer_data` (`internal.rs:352-359`) still passes because `expected_nonce` never advanced — collecting the `fee` again each time, draining the wallet's NEAR balance with no bound and no completed transfer ever taking place.

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L352-359)
```rust
    let nonce = if tx.nonce <= U64_MAX {
        tx.nonce.low_u64()
    } else {
        return Err(Error::Relayer(RelayerError::InvalidNonce));
    };
    if nonce != expected_nonce {
        return Err(Error::Relayer(RelayerError::InvalidNonce));
    }
```
