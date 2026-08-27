### Title
Unconditional relayer fee payout regardless of underlying transfer success in the ETH-implicit Wallet Contract - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The Wallet Contract's `inner_rlp_execute` schedules a relayer fee payment via a standalone `promise_batch_create`/`promise_batch_action_transfer` pair that is *not chained* to the underlying base-token or ERC-20 (`ft_transfer`) transfer promise. The fee is fired in parallel and is paid out to the relayer even when the actual transfer that the fee is supposed to compensate ultimately fails.

### Finding Description
In `inner_rlp_execute`, once a transaction is parsed as `EOABaseTokenTransfer` or `ERC20Transfer` with a non-zero `fee`, the contract immediately creates an independent promise to pay the relayer: [1](#0-0) 

This transfer promise is created via `env::promise_batch_create`/`env::promise_batch_action_transfer` directly — it is not `.then()`-chained after the actual transfer action, and its outcome is never inspected. Meanwhile, the actual value-moving action (a native `Transfer` for `EOABaseTokenTransfer`, or the `ft_transfer` call for `ERC20Transfer`) is dispatched separately through `action_to_promise`/`nep_141_storage_balance_callback`, whose result is checked only in `rlp_execute_callback`: [2](#0-1) 

Because the fee-payment promise and the transfer promise are sibling promises (not parent/child), a `PromiseResult::Failed` outcome on the transfer branch is only used to refund the *caller's attached deposit* (`caller_deposit`), never to claw back or gate the relayer fee. The relayer therefore collects payment independent of whether the ERC-20/base-token transfer it was supposed to relay actually succeeds.

This matches the report's bug class ("unchecked ERC20 transfer operation" — an operation proceeds/pays out as if a token transfer succeeded without verifying its actual outcome), here manifested as the wallet paying an unprivileged relayer regardless of whether the corresponding `ft_transfer` (an ERC-20-equivalent NEP-141 transfer) or native transfer it accompanies is executed successfully.

### Impact Explanation
This does not directly cause fund loss to the token contract or violate NEP-141 supply invariants, since `ft_transfer` panics atomically on insufficient balance (unlike Solidity ERC-20s that can silently return `false`), so the token side is self-consistent. However, the ETH-implicit account owner's $NEAR balance is unconditionally debited for the relayer fee even in scenarios where the intended action never completes (e.g., `ft_transfer` fails because of a receiver-side panic, insufficient token balance, or a malicious/failing token contract as `target`). Since the fee amount and target are attacker-controlled (an untrusted relayer supplies the RLP-encoded transaction and forwards it), a relayer could construct/replay a transaction whose value-moving action is designed to fail while the fee leg still succeeds, repeatedly draining small amounts of $NEAR from the wallet owner without ever delivering the requested transfer. This is a fund-loss vector on the wallet owner's NEAR balance, though bounded per-transaction by the signed fee amount, and mitigated by the one-in-flight-transaction invariant and by nonce incrementing rules.

### Likelihood Explanation
Reachable purely by unprivileged/ordinary users: any relayer executing `rlp_execute` on an ETH-implicit account can choose the `target` (e.g., a token contract that always panics for a given receiver, or one it controls) and set a nonzero fee. No validator or protocol privilege is required, and the path is exercised in normal ETH-implicit-wallet operation (see `EthEmulationKind::ERC20Transfer`/`EOABaseTokenTransfer` and their fee handling). The requirement that the fee be attacker/relayer-controlled and that a failing target be craftable somewhat limits practical scale, but the mechanism itself is systemic to every fee-bearing emulated transfer.

### Recommendation
Chain the fee-payment promise after the transfer action instead of firing it independently, or gate the fee transfer inside `rlp_execute_callback`/`nep_141_storage_balance_callback` behind a check of `PromiseResult::Successful` for the underlying transfer, mirroring the existing `caller_deposit` refund-on-failure logic. Alternatively, batch the fee-transfer as part of the same promise chain that performs the `Transfer`/`ft_transfer`, so a failure of the primary action also prevents (or refunds) the relayer fee.

### Proof of Concept
1. A relayer submits an `rlp_execute` transaction on behalf of an ETH-implicit wallet, encoding an `ERC20Transfer` (or `EOABaseTokenTransfer`) action with a non-zero fee.
2. `inner_rlp_execute` immediately schedules `promise_batch_action_transfer(refund_promise, *fee)` to the relayer's account (`context.predecessor_account_id`) — see lines 381-384 above.
3. Separately, the corresponding `ft_transfer` (via `nep_141_storage_balance_callback` → `action_to_promise`) is scheduled against a token contract that will panic for the given `receiver_id`/`args` (e.g., receiver blacklisted, or insufficient token balance).
4. The token-transfer promise resolves to `PromiseResult::Failed`; `rlp_execute_callback` only refunds the `caller_deposit` (attached NEAR deposit), not the fee already paid out in step 2.
5. Net effect: the relayer is paid the fee even though the intended ERC-20 transfer never took place, and the wallet owner's NEAR balance is reduced with no compensating action performed — repeatable across multiple transactions/nonces. [3](#0-2)

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-317)
```rust
        match env::promise_result(0) {
            PromiseResult::Failed => {
                // The cross-contract call failed, refund the caller if needed
                if let Some(CallerDeposit { account_id, yocto_near }) = caller_deposit {
                    let refund_promise = env::promise_batch_create(&account_id);
                    env::promise_batch_action_transfer(
                        refund_promise,
                        NearToken::from_yoctonear(yocto_near.into()),
                    );
                }

                ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Failed Near promise".into()),
                }
            }
            PromiseResult::Successful(value) => {
                ExecuteResponse { success: true, success_value: Some(value), error: None }
            }
        }
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L366-385)
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
