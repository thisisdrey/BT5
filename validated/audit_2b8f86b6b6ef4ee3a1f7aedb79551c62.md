## Title
Fee refund is transferred to the relayer before eth-target validation completes, enabling unbounded fund drain from an eth-implicit wallet - (File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs)

## Summary
`WalletContract::rlp_execute` unconditionally dispatches the relayer "fee" transfer for `EOABaseTokenTransfer`/`ERC20Transfer` kinds immediately inside `inner_rlp_execute`, before the async address-registrar validation that determines whether the transaction target is actually legitimate. When that validation later fails (a faulty/malicious `target`), the nonce is intentionally *not* advanced, allowing the exact same signed payload to be resubmitted indefinitely while the fee keeps being paid out with no forward progress — a direct analog of the reported "transfer must be last" reentrancy/ordering class.

## Finding Description
In `inner_rlp_execute`, once the RLP payload is parsed, the code determines whether nonce advancement should be delayed for the address-check flow, and separately (independently) schedules the fee payment to the caller: [1](#0-0) 

Note that the fee-payment `match` on `TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer { fee, .. })` uses `..`, so it fires regardless of whether `address_check` is `Some` (i.e. regardless of whether the target still needs asynchronous verification via the address registrar). This `refund_promise` is a *separate, unchained* promise batch — it is not `.then()`-attached to the main action or to the address-check callback, so it executes independently of whatever the rest of the transaction does.

When `address_check: Some(address)` is produced (target resolves to another eth-implicit account while the payload parses as an ERC-20-style emulation), the actual validity of the target is only confirmed later, asynchronously, via a cross-contract call to the address registrar and its callback: [2](#0-1) 

If that registrar lookup finds that a named account actually exists at that address (i.e. the caller supplied a "faulty" `target`), the transaction is rejected as a relayer error, and — critically — the nonce is *not* incremented if the caller is not the wallet's own signer: [3](#0-2) 

The comment at `inner_rlp_execute` explicitly documents the intentional delay of nonce incrementing for this exact case: [4](#0-3) 

Because (a) the fee transfer already fired before validation, and (b) the nonce never advances on this failure path, the same signed eth-transaction bytes (`tx_bytes_b64`) can be resubmitted to `rlp_execute` by *any* caller repeatedly with the same `target`, re-triggering the fee payout every time without the underlying action ever completing. `target` is a parameter supplied directly by whoever calls `rlp_execute`, not something the user signs inside the RLP transaction — only `tx.to` is signed and separately cross-checked for internal consistency in `validate_tx_relayer_data`, not against the registrar: [5](#0-4) 

The `fee` amount itself is derived purely from the user's signed `max_fee_per_gas * gas_limit`, so it can represent a meaningful amount of NEAR: [6](#0-5) 

This is architecturally the same bug class as the reported Solidity issue: a balance-affecting `transfer` (here, `env::promise_batch_action_transfer` for the "fee") is issued before all validation/state-transition steps for the surrounding operation are guaranteed to complete, allowing an attacker to collect the transfer's benefit even when the surrounding operation is subsequently determined invalid and does not make forward progress (no nonce advance = replayable).

## Impact Explanation
Any account that can obtain a copy of a user's previously-signed RLP transaction (these are relayed/broadcast to be gasless-submitted by arbitrary relayers, by design) can call `rlp_execute` repeatedly against the wallet, deliberately (or opportunistically, if the target address later becomes a registered named account) choosing a `target` that routes through the vulnerable `address_check: Some(...)` path. Each call pays out the `fee` amount from the wallet to the caller's account while never advancing the wallet's nonce, so the same call can be repeated without limit until the wallet's NEAR balance is exhausted. This is a concrete theft-of-funds vector against the wallet owner, reachable from an ordinary, unprivileged NEAR account with no special access key, node, or protocol-level capability.

## Likelihood Explanation
The attacker only needs: (1) a previously broadcast/known signed RLP transaction from the target wallet (relayer networks are explicitly designed so third parties can submit these), and (2) the ability to invoke `rlp_execute` with an attacker-chosen `target` parameter, which is always possible since `target` is not signed by the user and not validated against the registrar until after the fee is already paid. No race condition or timing precision is required — the vulnerable path is a normal, deterministic control-flow branch (`maybe_account_id.is_some()` in `address_check_callback`) reachable via one extra cross-contract hop.

## Recommendation
Do not fire the fee `refund_promise` in `inner_rlp_execute` until the transaction is known to be valid and about to execute. Concretely:
- For the `address_check: Some(address)` case, defer scheduling of the fee-refund promise until inside `address_check_callback`'s success branch (i.e., only after `maybe_account_id.is_none()` is confirmed), chaining it before/with the actual action promise rather than issuing it eagerly in `inner_rlp_execute`.
- More generally, ensure any balance-transferring side effect tied to relaying/refunding is only dispatched once all required async validation for that specific transaction path has completed, mirroring the recommendation to place `transfer` calls at the very end of the validated code path.

## Proof of Concept
1. User `alice` (an eth-implicit account) signs an RLP transaction whose `data` matches an ERC-20 selector (e.g. `ft_transfer`-style `ERC20_TRANSFER_SELECTOR`) targeting an eth-implicit address `0xdead...` that is not yet a registered named account, with a non-trivial `max_fee_per_gas * gas_limit` (i.e. a meaningful `fee`).
2. This signed transaction becomes known to an attacker (e.g. broadcast to a public relayer mempool, as intended by the wallet's design).
3. Attacker calls `alice`'s wallet's `rlp_execute(target = "0xdead...", tx_bytes_b64)`.
4. `inner_rlp_execute` computes `TransactionKind::EthEmulation(EthEmulationKind::ERC20Transfer{..}) ` / or the `EOABaseTokenTransfer{address_check: Some(0xdead...), fee}` variant, and immediately fires `env::promise_batch_action_transfer(refund_promise, fee)` to the attacker's account (the `predecessor_id`), per lines 374-385 of `lib.rs`.
5. The registrar lookup subsequently resolves and — if a named account exists at that address (attacker can arrange this ahead of time via the registrar, if registration is permissionless, or simply wait for it to become true) — `address_check_callback` returns a failure without banning the attacker (since `env::signer_account_id() != current_account_id`) and without incrementing `self.nonce`.
6. Attacker resubmits the identical `rlp_execute` call with the same `tx_bytes_b64`/`target` indefinitely, collecting `fee` NEAR each time, draining `alice`'s wallet balance.

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L350-385)
```rust
            // Increment nonce for all cases where the registrar contract is not needed
            // to prevent replay of those transactions. For transactions that go through
            // the registrar we still do not know if the transaction has a relayer error
            // or not, therefore we must delay incrementing the nonce.
            //
            // Note: relayers with access keys cannot use this delay to needlessly spend
            // the users tokens because only one transaction is allowed to be in-flight
            // at a time.
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L412-432)
```rust
    let promise = match transaction_kind {
        TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
            address_check: Some(address),
            ..
        }) => {
            let callback_gas = ADDRESS_CHECK_CALLBACK_GAS.saturating_add(action.gas());
            let ext = WalletContract::ext(current_account_id).with_static_gas(callback_gas);
            let address_registrar = {
                let account_id = ADDRESS_REGISTRAR_ACCOUNT_ID
                    .trim()
                    .parse()
                    .unwrap_or_else(|_| env::panic_str("Invalid address registrar"));
                ext_registrar::ext(account_id).with_static_gas(REGISTRAR_LOOKUP_GAS)
            };
            let address = format!("0x{}", hex::encode(address));
            address_registrar.lookup(address).then(ext.address_check_callback(
                target,
                action,
                caller_deposit,
            ))
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L54-64)
```rust
    // Compute the fee based on the user's Ethereum transaction.
    // This is sent as a refund to the relayer in the case of an emulated base token
    // transfer or ERC-20 transfer. The reason for this refund is that it allows a
    // user with $NEAR to use a relayer service from their wallet immediately without
    // additional on-boarding.
    let tx_fee = {
        // Limit the cost by `VALUE_MAX` since we will convert this to a $NEAR amount.
        // The call to `low_u128` is safe because `VALUE_MAX` is the largest accepted value.
        let wei_amount = tx.max_fee_per_gas.saturating_mul(tx.gas_limit).min(VALUE_MAX).low_u128();
        NearToken::from_yoctonear(wei_amount.saturating_mul(MAX_YOCTO_NEAR as u128))
    };
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L318-368)
```rust
fn validate_tx_relayer_data<'a>(
    tx: &NormalizedEthTransaction,
    target: &'a AccountId,
    context: &ExecutionContext,
    expected_nonce: u64,
) -> Result<TargetKind<'a>, Error> {
    if tx.address.raw() != context.current_address {
        return Err(Error::Relayer(RelayerError::InvalidSender));
    }

    if tx.chain_id != Some(CHAIN_ID) {
        return Err(Error::Relayer(RelayerError::InvalidChainId));
    }

    let to = tx.to.ok_or(Error::User(UserError::EvmDeployDisallowed))?.raw();

    let target_kind = parse_target(target, context.current_address);

    // valid targets satisfy `to == target` or `to == hash(target)`
    let is_valid_target = match target_kind {
        TargetKind::CurrentAccount if to == context.current_address => {
            target == &context.current_account_id
        }
        TargetKind::EthImplicit(address) if to == address => {
            target.as_str()
                == format!("0x{}{}", hex::encode(address), context.current_account_suffix())
        }
        _ => to == account_id_to_address(target),
    };

    if !is_valid_target {
        return Err(Error::Relayer(RelayerError::InvalidTarget));
    }

    let nonce = if tx.nonce <= U64_MAX {
        tx.nonce.low_u64()
    } else {
        return Err(Error::Relayer(RelayerError::InvalidNonce));
    };
    if nonce != expected_nonce {
        return Err(Error::Relayer(RelayerError::InvalidNonce));
    }

    // Relayers must attach at least as much gas as the user requested.
    let gas_limit = if tx.gas_limit < U64_MAX { tx.gas_limit.as_u64() } else { u64::MAX };
    if env::prepaid_gas().as_gas() < gas_limit.saturating_mul(GAS_MULTIPLIER) {
        return Err(Error::Relayer(RelayerError::InsufficientGas));
    }

    Ok(target_kind)
}
```
