## Title
Eth-Implicit Wallet Contract pays the relayer fee unconditionally and without incrementing the nonce on the "address-check" path, allowing unlimited replay and fund drain - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The Wallet Contract's `inner_rlp_execute` sends the relayer's `fee` refund via a fire-and-forget `promise_batch_action_transfer` *before* the outcome of the user's intended action is known, and — for the `EOABaseTokenTransfer{ address_check: Some(_), .. }` path — deliberately skips incrementing the replay-protection `nonce`. Because the fee payment is not gated on (does not "check the return value of") the async registrar-lookup / downstream-action result, and the nonce is left unchanged, the same signed Ethereum transaction can be resubmitted to `rlp_execute` an unbounded number of times, draining `fee` from the wallet on every call.

### Finding Description
`inner_rlp_execute` processes the parsed action and, in the same synchronous call, unconditionally fires a Transfer promise to pay the relayer's fee: [1](#0-0) 

Note that:
- Nonce increment is explicitly **skipped** when the transaction kind is `EOABaseTokenTransfer{ address_check: Some(_), .. }` (i.e., whenever the async address-registrar check is required) — the comment says this is intentional "because the error is caused by a faulty relayer... An honest relayer may still be able to successfully send the user's intended transaction."
- The very next `if let` block matches on the *same* `EOABaseTokenTransfer{ fee, .. }` (and `ERC20Transfer{ fee, .. }`) variants and fires the fee-refund transfer **regardless of `address_check`**, i.e., regardless of whether the nonce was incremented.

This means for any transaction that requires the async registrar check, the contract:
1. Pays the relayer's fee immediately via an independent `promise_batch_create` + `promise_batch_action_transfer` (not chained via `.then()` to the outcome of the real action), and
2. Leaves `nonce` unchanged so that `expected_nonce` validation in `validate_tx_relayer_data` still succeeds on a repeat call. [2](#0-1) 

The subsequent async resolution in `address_check_callback` only revokes the relayer's access key if `env::signer_account_id() == current_account_id` (i.e., only when the relayer used a `FunctionCallPermission` key on the wallet itself); it never claws back the fee already sent, and never bans anyone if the relayer is a third-party account that simply calls the public `rlp_execute` method (the normal use case described in the design docs, where "an Ethereum-compatible wallet user sends a transaction to an RPC endpoint, which wraps it... as an `rlp_execute` call"): [3](#0-2) 

Since `rlp_execute` is a public, non-privileged method that anyone (any relayer holding the user's signed RLP bytes) can call, and since `has_in_flight_tx` only blocks *concurrent* calls (not sequential calls across blocks), the unchanged nonce lets the same signed transaction be resubmitted repeatedly in separate blocks, each time re-triggering the unconditional fee-refund transfer: [4](#0-3) 

This is the direct analog of the reported bug class: a downstream, funds-moving side effect (the relayer payment, analogous to the `emit` after an unchecked `transferFrom`) is performed without checking/gating on the actual result of the operation it is supposed to be compensating for, creating an inconsistent, exploitable state.

### Impact Explanation
Any party who obtains the user's signed RLP transaction bytes (which the user must share with *some* relayer to get the transaction delivered) can repeatedly call `rlp_execute` with the same `tx_bytes_b64`/`target`, each call producing a real NEAR `Transfer` receipt of `fee` yoctoNEAR out of the wallet account to themselves, since:
- the fee send is unconditional and un-gated on the eventual outcome, and
- the nonce is not incremented on this path, so `expected_nonce` continues to match on every replay.

This is a concrete theft-of-funds / replay vulnerability: the wallet's balance can be drained in increments of `fee` (bounded by the user's own `max_fee_per_gas * gas_limit`, but each replay is a fresh, independent drain) for as long as the wallet retains sufficient balance and the underlying signed transaction remains "faulty" from the registrar's perspective (which an attacker fully controls, since they choose whether to route through the address-check path at all).

### Likelihood Explanation
Likelihood is high for any wallet that uses third-party/public relayers (the documented, intended usage model for ETH-implicit accounts) rather than exclusively self-relaying via an owned `FunctionCallPermission` key. A relayer needs no special privilege — it only needs to receive one validly signed transaction from the user (the normal flow) and can then call the public `rlp_execute` method repeatedly.

### Recommendation
- Only send the relayer fee-refund after the transaction has been confirmed as protocol-valid (e.g., chain it via `.then()` off of the `address_check_callback` resolution instead of firing it eagerly and unconditionally in `inner_rlp_execute`).
- Always increment `nonce` once a transaction has been accepted for processing (even down the async `address_check` path), so that a given signed transaction cannot be resubmitted more than once regardless of how it is ultimately resolved.
- If nonce increment must be deferred pending the registrar-lookup result, gate the fee payment on that same deferred decision so fee and nonce state changes happen atomically together.

### Proof of Concept
1. User signs an Ethereum transaction (via their wallet) intended for an ERC-20/base-token transfer with `to` = address `A`, with a non-zero `max_fee_per_gas`/`gas_limit` (i.e., non-zero `fee`), and hands the raw bytes to a relayer to submit.
2. The relayer calls `rlp_execute(target="0x{hex(A)}{suffix}", tx_bytes_b64)`. Because `target` parses to `TargetKind::EthImplicit`, the transaction is classified `EthEmulation(EOABaseTokenTransfer{address_check: Some(A), fee})`.
3. In `inner_rlp_execute`, nonce is left unchanged (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:358-365`), but the `fee` refund transfer to the relayer is fired immediately and unconditionally (`:374-385`).
4. The subsequent registrar-lookup promise resolves in `address_check_callback`; regardless of the outcome (banned relayer / error / success), the fee transfer from step 3 has already been broadcast and cannot be reverted.
5. The relayer (or anyone with the same signed bytes) calls `rlp_execute` again with the identical `target`/`tx_bytes_b64`. Since `nonce` is unchanged, `validate_tx_relayer_data`'s nonce check still passes, and the fee is paid out again.
6. Repeat step 5 to drain the wallet's balance in `fee`-sized increments.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-128)
```rust
    #[payable]
    pub fn rlp_execute(
        &mut self,
        target: AccountId,
        tx_bytes_b64: String,
    ) -> PromiseOrValue<ExecuteResponse> {
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
        let current_account_id = env::current_account_id();
        let predecessor_account_id = env::predecessor_account_id();
        let result = inner_rlp_execute(
            current_account_id.clone(),
            predecessor_account_id,
            target,
            tx_bytes_b64,
            &mut self.nonce,
        );

        match result {
            Ok(promise) => {
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(Error::Relayer(_)) if env::signer_account_id() == current_account_id => {
                let promise = create_ban_relayer_promise(current_account_id);
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(e) => PromiseOrValue::Value(e.into()),
        }
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L130-192)
```rust
    /// Callback after checking if an address is contained in the registrar.
    /// This check happens when the target is another eth implicit account to
    /// confirm that the relayer really did check for a named account with that address.
    #[private]
    pub fn address_check_callback(
        &mut self,
        target: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
        let maybe_account_id: Option<AccountId> = match env::promise_result(0) {
            PromiseResult::Failed => {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Call to Address Registrar contract failed".into()),
                });
            }
            PromiseResult::Successful(value) => match serde_json::from_slice(&value) {
                Ok(x) => x,
                Err(_) => {
                    return PromiseOrValue::Value(ExecuteResponse {
                        success: false,
                        success_value: None,
                        error: Some("Unexpected response from account registrar".into()),
                    });
                }
            },
        };
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L358-385)
```rust
            if let TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
                address_check: Some(_),
                ..
            }) = &transaction_kind
            {
            } else {
                *nonce = nonce.saturating_add(1);
            }

            // If the action is an emulated base token or ERC-20 transfer with a non-zero fee then
            // create a promise to send the refund to the relayer. This allows any relayer
            // to safely serve base token transfers from any wallet without additional
            // on-boarding because the relayer will receive some compensation for sending
            // the transaction. Users should always verify the fee before signing a base token
            // transfer. Relayers should also verify the fee before sending to make sure the
            // user's signed transaction will refund enough to cover the relayer's gas costs.
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
