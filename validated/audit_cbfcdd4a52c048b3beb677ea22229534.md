## #Q4605 Analysis Result

### Title
Wallet-contract pays relayer fee before nonce advance in the `EOABaseTokenTransfer{address_check: Some(_)}` registrar-lookup path, enabling unlimited fee draining via nonce-locked replay - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
When a relayer submits a user-signed Ethereum transaction whose `target` is another eth-implicit account and whose calldata is parseable as an ERC-20-style emulation, `inner_rlp_execute` fires the relayer fee-refund promise unconditionally, but defers the nonce increment until the registrar lookup resolves in `address_check_callback`. If the registrar confirms the address is actually a registered (named) account — meaning the relayer's chosen `target` was invalid — the nonce is deliberately left unchanged, yet the fee has already been paid. This lets the same signed payload be replayed indefinitely, draining the wallet's balance one fee-payment at a time.

### Finding Description
`inner_rlp_execute` decides whether to bump the nonce based on the parsed `transaction_kind`: [1](#0-0) 

This explicitly skips the nonce increment only for `EOABaseTokenTransfer { address_check: Some(_), .. }` — the case produced when the calldata parses as an ERC-20 emulation (`ERC20Balance`/`ERC20Transfer`/`ERC20TotalSupply`) but `target_kind` resolves to `TargetKind::EthImplicit`, per the case-III branch in `parse_rlp_tx_to_action`: [2](#0-1) 

`TargetKind::EthImplicit` is only reachable when the `target` account ID's own address matches the tx's `to` field and passes the `is_valid_target` check in `validate_tx_relayer_data`, which uses `ExecutionContext::current_account_suffix()` to build the expected `target` string: [3](#0-2) 

Immediately after the (skipped) nonce-increment decision, the fee-refund promise is created unconditionally for both `EOABaseTokenTransfer` and `ERC20Transfer`, ignoring the `address_check` field entirely (the `..` wildcard matches `Some` or `None`): [4](#0-3) 

This `promise_batch_action_transfer` is a standalone batch action (not chained to the subsequent registrar lookup), so it executes and pays the relayer (`context.predecessor_account_id`) out of the wallet's own balance regardless of what the registrar later reports.

The registrar result is only resolved afterward in `address_check_callback`. If the registrar finds the address IS a registered named account (i.e., the relayer's `target` choice was invalid/faulty), the code explicitly does **not** advance the nonce: [5](#0-4) 

and if the caller is not using the wallet's own access key (`env::signer_account_id() != current_account_id` — true for any ordinary relayer that calls `rlp_execute` directly as themselves, since it is a public method requiring no access key), no key is banned and `has_in_flight_tx` is left `false`. The exact same signed payload (same nonce) can therefore be resubmitted immediately, repeating the fee payment with no state change.

### Impact Explanation
An unprivileged attacker acting as a "relayer" can call `rlp_execute` on any user's wallet-contract account with a validly signed ERC-20-emulation transaction, deliberately picking `target` to be the eth-implicit account ID matching an address that is also registered in the address registrar. Every invocation:
1. Pays the attacker the transaction's signed fee out of the wallet contract's own NEAR balance, via `env::promise_batch_action_transfer`.
2. Leaves the nonce unchanged (because the registrar confirms the address is registered, triggering the "faulty relayer, don't advance nonce" branch), and leaves no access key to revoke.

Because the nonce never advances, the identical payload remains valid for resubmission, so the attacker can repeat this arbitrarily, draining the wallet contract's NEAR balance in fee-sized increments until exhausted. This is direct theft of user funds via wallet-contract meta-transaction authorization/economic-invariant bypass, matching Immunefi's High severity category for direct theft of user funds.

### Likelihood Explanation
- No special privileges are required: `rlp_execute` is a public, payable method callable by any account.
- The attacker only needs one validly user-signed RLP transaction whose calldata is ERC-20-emulation-shaped and whose intended target address happens to (or is chosen to) coincide with a registered account address — an attacker can also act as their own "helpful" relayer for their own wallet, or replay any such transaction they observe.
- The exploit is fully repeatable: each call resets `has_in_flight_tx` to `false` at the end, so there is no cooldown or single-use restriction stopping sequential replay.
- Total loss is bounded only by the wallet's NEAR balance and the number of replays the attacker performs (unlimited, sequential calls).

### Recommendation
Do not create the relayer fee-refund promise until it is certain the transaction has been accepted (i.e., until the nonce is guaranteed to advance). Specifically:
- Move the fee-refund `promise_batch_action_transfer` for the `EOABaseTokenTransfer{address_check: Some(_), ..}` case out of `inner_rlp_execute` and into `address_check_callback`, gated on the `None` (honest-relayer) branch where the nonce is actually incremented.
- Alternatively, make the fee payment a chained promise action that only executes after the registrar lookup succeeds and the nonce-advance branch is taken, rather than an independent `promise_batch_create` fired eagerly.

### Proof of Concept
Add a wallet-contract integration/unit test (in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests`) that:
1. Deploys the wallet contract, funds it, and constructs a validly-signed Ethereum transaction whose calldata is an ERC-20 `balanceOf`/`transfer` call, with `to` equal to some address `A`.
2. Sets `target` = the eth-implicit `AccountId` form of address `A` (i.e., `0x{hex(A)}`), triggering `TargetKind::EthImplicit` and the `address_check: Some` path.
3. Calls `rlp_execute(target, tx_bytes_b64)`, mocks the address-registrar `lookup` promise to return `Some(some_other_account_id)` (simulating the address being registered), and asserts:
   - The relayer (`predecessor_account_id`) balance increased by `tx_fee` after the call resolves.
   - `contract.nonce` is unchanged (still equal to `expected_nonce`).
4. Calls `rlp_execute` again with the exact same `tx_bytes_b64`/`target`, again mocking `lookup` to return `Some(...)`, and asserts the relayer is paid the fee a second time while the nonce still has not advanced — demonstrating repeated compensation without state progression, violating the "relayer compensation happens only alongside a nonce advance" invariant.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L160-173)
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L336-344)
```rust

```
