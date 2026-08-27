## Title
Wallet Contract relayer fee refund is paid out before the eth-implicit target's registrar validation completes, enabling unbounded replay-drain of the wallet's balance - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The NEAR Wallet Contract (`WalletContract::rlp_execute` / `inner_rlp_execute`) pays a relayer "fee" refund out of the wallet's own balance *before* it has confirmed — via the address-registrar cross-contract callback — that the relayer submitted a valid `target`. When that later check fails, the contract deliberately does **not** advance the replay-preventing nonce (so an honest relayer can retry), but it also never reverses or gates the fee payment that was already dispatched. This lets the same signed transaction be resubmitted to `rlp_execute` over and over, each time re-triggering the fee transfer, draining the wallet's balance without the nonce protection ever engaging — the same bug class as the reported liquidation issue: a side-effect (funds sent) occurs on a path whose accompanying state update (marking the operation "done"/incrementing nonce) is skipped.

### Finding Description
`inner_rlp_execute` (`lib.rs:330-473`) parses the RLP eth-transaction and, on success, decides whether to bump the replay-prevention `nonce` immediately: [1](#0-0) 

For `EOABaseTokenTransfer { address_check: Some(_), .. }` the nonce bump is intentionally **skipped**, because the contract must first ask the address registrar whether `target` is actually a named account (i.e. whether the relayer picked the wrong `target`). That check happens asynchronously in `address_check_callback`: [2](#0-1) 

If the registrar confirms `target`'s address maps to an existing named account and the caller is *not* using the wallet's own access key, `address_check_callback` simply returns an error response — no nonce update, no ban, no other state change: [3](#0-2) 

However, before this validation is even scheduled, `inner_rlp_execute` unconditionally dispatches the relayer's fee refund for any `EOABaseTokenTransfer`/`ERC20Transfer` with a non-zero fee — this code path is reached for the `address_check: Some(_)` case exactly the same as the `None` case, since the match only inspects `fee`, not `address_check`: [4](#0-3) 

That refund is a real `Promise::transfer` moving `fee` yoctoNEAR out of the wallet contract's own balance to `context.predecessor_account_id` (the caller of `rlp_execute`) immediately, well before the registrar lookup / callback resolves.

Because (a) the fee payment already executed and (b) the nonce was deliberately left unchanged for this branch (by design, to allow an "honest relayer" retry), nothing prevents an attacker who has obtained one validly-signed RLP transaction (signed by the real wallet owner, e.g. observed once in the mempool or handed to a relayer) from calling `rlp_execute` with that same `tx_bytes_b64` repeatedly. Each call:
1. Passes `validate_tx_relayer_data`'s nonce check (`internal.rs:352-359`) since the nonce was never incremented.
2. Immediately fires the fee-refund promise to the caller.
3. Triggers the registrar lookup, which again resolves "target is an existing named account", again returning an error with no state mutation.

This loop can be repeated indefinitely (bounded only by the wallet's balance and the caller's own gas cost), draining the wallet's NEAR balance via the `fee` field of one single signed transaction.

### Impact Explanation
This is concrete theft of funds from an eth-implicit account's wallet contract: an attacker can repeatedly extract the `fee` amount from the wallet's balance using a single previously-seen signed Ethereum transaction, with no accompanying state change (nonce bump or ban) ever occurring to stop the replay. This directly parallels the reported bug class — a side effect (fund transfer / collateral movement) happens on a code path where the necessary bookkeeping state update was omitted, enabling repeated/unauthorized withdrawal of funds that belong to the account owner.

### Likelihood Explanation
The path is reachable by any account calling the wallet contract's public, unprivileged `rlp_execute` method with a `tx_bytes_b64` payload matching the `EOABaseTokenTransfer{address_check: Some(_), fee != 0}` classification (an emulated base-token transfer targeting another eth-implicit-looking account, where the registrar ultimately resolves to a named account). The signed Ethereum transaction only needs to be valid once (owner-signed); it does not need to be a fresh signature per replay attempt, since the whole point of the exploit is resubmitting the identical bytes. No special privileges, races, or unlikely preconditions are required beyond obtaining one such transaction and repeatedly invoking the public method.

### Recommendation
Do not dispatch the relayer fee-refund promise until the target/address validation has fully resolved (i.e., only credit the fee from within `address_check_callback`'s success branch, after the registrar confirms the target was valid), or otherwise gate/reverse the fee transfer symmetrically with the nonce-advance decision so that a failed/faulty-relayer resolution can never produce a real balance transfer.

### Proof of Concept
1. Wallet owner signs one Ethereum transaction (via their Secp256k1 key) representing a base-token transfer whose `to` field resolves (through `parse_target`) to `TargetKind::EthImplicit(address)`, with a non-zero `max_fee_per_gas * gas_limit` (fee), and `target` chosen by an actual/simulated relayer.
2. Attacker observes/obtains this `tx_bytes_b64` (e.g. it was submitted once and failed the registrar check because the address is actually a named account).
3. Attacker repeatedly calls `wallet_contract.rlp_execute(target, tx_bytes_b64)` from their own account (not needing any access key on the wallet).
4. Each call: `inner_rlp_execute` → `nonce` unchanged (`lib.rs:358-365`) → fee-refund promise dispatched to attacker (`lib.rs:374-385`) → registrar lookup/`address_check_callback` rejects with no state change (`lib.rs:160-173`).
5. Repeat step 3 to drain the wallet contract's NEAR balance by `fee` per call.

### Citations

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L367-385)
```rust
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
