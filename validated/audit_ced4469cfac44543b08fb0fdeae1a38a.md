### Title
Relayer fee refund paid before nonce advance / action success in `address_check` flow enables repeated wallet drain - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
When a `Transfer`-style emulated action targets another eth-implicit account (`EOABaseTokenTransfer { address_check: Some(address), fee }`), `inner_rlp_execute` unconditionally schedules a relayer fee refund based only on `fee != 0` and `predecessor != current`, independent of whether `address_check` is set, while it explicitly *skips* the nonce increment for this case, deferring it to `address_check_callback`. If the registrar lookup in that callback returns `Some(account_id)` (the address is actually a registered/named account, i.e. the relayer used the wrong `target`), the callback treats it as a faulty-relayer condition and returns without ever incrementing the nonce — yet the fee was already sent to the relayer/caller. Because the transaction's nonce is unchanged, the exact same signed RLP transaction can be resubmitted indefinitely, draining the wallet's fee amount on each call.

### Finding Description
In `inner_rlp_execute` [1](#0-0) , the nonce-increment guard only special-cases `EOABaseTokenTransfer { address_check: Some(_), .. }` (deferring nonce advance to the callback), but the very next block that creates the relayer-fee refund promise matches on `EOABaseTokenTransfer { fee, .. }` (or `ERC20Transfer { fee, .. }`) **without checking `address_check`**. Thus for any base-token transfer to another eth-implicit account with a non-zero `fee` and a non-self predecessor, the fee is paid out to `predecessor_account_id` immediately, before it is known whether the address-registrar lookup will succeed or fail.

The registrar lookup happens via `address_check_callback` [2](#0-1) . If the resolved address maps to an existing named account (`maybe_account_id.is_some()`), the code treats this as a faulty-relayer scenario and explicitly documents "we intentionally do not increment the nonce in this case" [3](#0-2) . When the caller is not using a wallet access key (`env::signer_account_id() != current_account_id`, i.e., the relayer submitted the transaction under their own account as `predecessor`), the callback simply returns an error `ExecuteResponse` with no key ban and no nonce change.

Because `validate_tx_relayer_data` requires only that the transaction's nonce equal `expected_nonce` [4](#0-3) , and the nonce is never advanced on this path, the identical signed RLP bytes remain valid for resubmission. The `has_in_flight_tx` guard only prevents concurrent in-flight calls, not sequential replay after the promise chain resolves (it is reset to `false` at the top of `address_check_callback` [5](#0-4) ), so an attacker can call `rlp_execute` again with the same `tx_bytes_b64` and receive the fee again.

### Impact Explanation
Each call the wallet contract transfers `fee` yoctoNEAR out of its own balance to `predecessor_account_id` via `env::promise_batch_action_transfer` [6](#0-5) , funded from the wallet contract account itself. Since the underlying action never advances state (nonce unchanged, transfer never actually executes because the registrar path aborts), an attacker who obtains one legitimately-signed transaction with a non-zero fee whose `target` happens to resolve to a registered named account can resubmit it repeatedly to drain the wallet's NEAR balance one `fee` increment at a time — direct theft of user funds via the wallet-contract's meta-transaction/relayer-compensation mechanism.

### Likelihood Explanation
Exploitation requires (1) a validly-signed RLP transaction from the wallet owner with `fee != 0` whose relayer-chosen `target` is an eth-implicit-style account whose address is also registered under a named account in the address registrar, and (2) the attacker acting as `predecessor_account_id` (the direct caller of `rlp_execute`, e.g. any relayer who received/observed this signed transaction). No special privileges, access keys, or validator/node access are needed — any unprivileged account can call `rlp_execute` on the target wallet contract with the previously obtained signed bytes. The condition (target address also registered) is a normal/likely occurrence for wallets that register a preferred named account, making the scenario realistically reachable, and the drain is fully repeatable since state (nonce) never advances on this path.

### Recommendation
Gate the relayer-fee refund promise on the same condition used to defer the nonce increment: only create the fee-refund promise when `address_check` is `None`, or otherwise attach/chain the fee-refund logic to occur only after the registrar lookup confirms the target is *not* a registered named account (i.e., move the fee payment into the `None` branch of `address_check_callback`, alongside the nonce increment), enforcing the invariant that relayer compensation only ever happens together with a nonce advance.

### Proof of Concept
Add a Rust unit/integration test in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests.rs` (or a runtime-test-loop test) that:
1. Deploys a wallet contract for an eth-implicit account with a known nonce, and configures/mocks the address registrar to return `Some(some_account_id)` for a chosen eth address.
2. Constructs and signs an RLP `Transfer` transaction with `to` = the eth-implicit form of that address, non-zero `max_fee_per_gas * gas_limit` (nonzero `fee`), and correct `nonce`.
3. Calls `rlp_execute` as `predecessor_account_id != current_account_id`, drives the promise chain (`address_check_callback` with mocked `PromiseResult::Successful` returning `Some(account_id)`), and asserts: (a) a fee-transfer promise/receipt was created to `predecessor_account_id`, (b) `get_nonce()` is unchanged after the call.
4. Resubmit the exact same `tx_bytes_b64` a second time and assert the fee is paid again while the nonce remains unchanged — demonstrating unbounded repeatable drain, violating the intended invariant "relayer compensation happens only alongside a nonce advance."

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L134-192)
```rust
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
