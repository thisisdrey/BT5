### Title
Wallet-contract pays relayer fee for `EOABaseTokenTransfer` (`address_check: Some`) before nonce advance, enabling unlimited fee-drain via replay of a single signed tx - (File: runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs, lib.rs)

### Summary
In `inner_rlp_execute` the relayer fee refund for an `EthEmulation::EOABaseTokenTransfer` is scheduled unconditionally whenever `fee` is non-zero, but the nonce increment for the `address_check: Some(_)` variant of that same transaction kind is deliberately deferred until the async `address_check_callback` resolves. Because the deferred-nonce path can permanently resolve to a "faulty relayer" outcome without banning the caller (when the caller is not using the wallet's own access key), the exact same signed RLP transaction can be resubmitted indefinitely, paying out the relayer fee every single time with the nonce never advancing.

### Finding Description
`internal::parse_rlp_tx_to_action` produces `TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer { address_check: Some(address), fee })` whenever the calldata parses as a known Ethereum-emulation selector (e.g. an ERC-20-style call) but the resolved `target` is an eth-implicit account other than the current one [1](#0-0) .

In `inner_rlp_execute`, the nonce increment is explicitly skipped for this specific variant (`address_check: Some(_)`), with the stated rationale that the outcome may reveal the *relayer* was at fault, not the user, so a different relayer should be allowed to retry with the same nonce: [2](#0-1) 

However, the very next block that schedules the relayer fee refund does **not** make this same distinction — it fires for `EOABaseTokenTransfer { fee, .. }` regardless of whether `address_check` is `Some` or `None`, i.e. before the registrar lookup that determines whether the relayer's target choice was even valid: [3](#0-2) 

The promise chain then queries the address registrar and lands in `address_check_callback`. If the registrar returns `Some(account_id)` (meaning the address is actually a registered named account, so the relayer's `target` choice was wrong), the code intentionally does **not** advance the nonce, and only bans the caller if it was using the wallet's own access key (`signer_account_id() == current_account_id`); otherwise it just returns an error response with no state change to the caller's rights: [4](#0-3) 

Since the registrar's answer for a given address is deterministic (it doesn't change between calls in this scenario), a relayer holding one validly-signed RLP transaction from the wallet owner can call `rlp_execute` repeatedly with the same `tx_bytes_b64`/`target`. Each call: (1) passes `validate_tx_relayer_data` because `nonce == expected_nonce` still holds, (2) immediately transfers the fee to `predecessor_account_id` (the caller), (3) resolves through the registrar callback to the same "faulty relayer" branch without banning (since the caller need not be using the wallet's access key), and (4) leaves the nonce unchanged, permitting another identical resubmission. `has_in_flight_tx` only prevents *concurrent* in-flight transactions, not sequential replay across separate top-level calls, so this can be repeated until the wallet's NEAR balance is exhausted.

### Impact Explanation
This breaks the intended invariant "relayer compensation happens only alongside a nonce advance." A dishonest relayer that merely possesses one legitimately user-signed meta-transaction can drain the wallet-contract's NEAR balance to itself in unbounded, repeated fee payouts, without ever completing the user's intended action and without the nonce ever moving. This is a direct theft-of-user-funds vector via the wallet-contract's meta-transaction/relayer-compensation mechanism.

### Likelihood Explanation
The attacker needs only a single validly-signed RLP transaction (as would normally be shared with any relayer in the standard gasless meta-transaction flow) whose `to` address happens to correspond to a registered NEAR account, and to submit it as `target = EthImplicit(address)` rather than the real account id. From then on, replay is fully deterministic, requires no special privilege (no access key on the wallet is needed to avoid the ban), and is repeatable at will — the only cost is gas for each `rlp_execute` + registrar-lookup call, which is typically much smaller than the fee (calibrated by the user's `max_fee_per_gas * gas_limit`), making each replay profitable.

### Recommendation
Do not schedule the relayer fee-refund promise for the `address_check: Some(_)` variant until the registrar callback confirms the target was actually invalid (i.e., move the fee-payment logic for this specific case into `address_check_callback`'s "not registered" branch, alongside the nonce increment, so fee payment and nonce advance are atomic).

### Proof of Concept
Add a unit/integration test in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests` that:
1. Deploys the wallet contract and pre-registers an address `A` in a mock address registrar so that `lookup(A)` returns `Some(some_account_id)`.
2. Signs one RLP transaction with `to = A` (an ERC-20-style selector) and calls `rlp_execute(target = EthImplicit(A), tx_bytes_b64)` from a non-owner predecessor account, with `fee > 0`.
3. Assert the fee transfer promise is scheduled to the predecessor immediately.
4. Simulate the `address_check_callback` with `Some(some_account_id)` and assert: nonce is unchanged, and (since predecessor != current_account_id / signer != current_account_id) no ban occurs.
5. Call `rlp_execute` again with the identical `tx_bytes_b64`/`target` and assert the fee is paid out a second time while the nonce still has not advanced — demonstrating unlimited repeatable drain.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L107-122)
```rust
        Ok((action, ParsableTransactionKind::EthEmulation(eth_emulation))) => {
            if let TargetKind::EthImplicit(address) = target_kind {
                // Even though the action was parsable, the target is another wallet contract,
                // so the action _must_ still be a base token transfer, but we need
                // to check if the target is not registered (otherwise the relayer is faulty).
                (
                    Action::Transfer { receiver_id: target.to_string(), yocto_near: 0 },
                    TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
                        address_check: Some(address),
                        fee: tx_fee,
                    }),
                )
            } else {
                (action, TransactionKind::EthEmulation(eth_emulation.into()))
            }
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L160-174)
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
