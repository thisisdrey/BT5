#No vulnerability found for this question.

**Rationale:** The `target` parameter is not independently attacker-controllable in a way that could hijack a signed AddKey/DeleteKey transaction. `validate_tx_relayer_data` enforces that `target` must correspond exactly to the `to` address embedded in the *signed* Ethereum transaction (either directly for `TargetKind::EthImplicit`/`CurrentAccount`, or via `account_id_to_address` hashing for `TargetKind::OtherNearAccount`) [1](#0-0) . Since `to` is part of the RLP-encoded payload that is signed by the wallet owner's private key (verified via `tx.address.raw() != context.current_address` check that recovers the signer from the ECDSA signature) [2](#0-1) , a relayer cannot alter `to` without invalidating the signature, and thus cannot redirect an actually-intended self-AddKey transaction to a third-party `target`.

For the `TargetKind::EthImplicit` branch to trigger for a `SelfNearNativeAction` (AddKey/DeleteKey selector), the user's own signed transaction must have `to` set to a different wallet's address in the first place — meaning the user never signed a self-targeted AddKey to begin with. In that scenario, per Ethereum EOA semantics, arbitrary calldata sent to another EOA is meaningless and the transaction is correctly treated as a plain value transfer of `tx.value`, which is exactly the documented intent in the code comments [3](#0-2) . No AddKey/DeleteKey action is ever executed against the wrong account, and no value beyond what the user explicitly signed into `tx.value` is moved [4](#0-3) . This is intentional, documented fallback behavior mirroring real Ethereum semantics, not an authorization bypass or fund-diversion bug reachable by an unprivileged relayer.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L88-95)
```rust
            if let TargetKind::EthImplicit(_) = target_kind {
                // The calldata was parseable as a Near native action where the target
                // should be the current account, but the target is some other wallet contract.
                // This is technically allowed under the Ethereum standard for base token transfers
                // (where any calldata can be used when sending tokens to another EOA), so we
                // assume such a transfer must have been the user's intent. No address check is
                // required in this case because no Near account other than the current account
                // can be the receiver of these actions.
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L159-165)
```rust
    validate_tx_value(&tx)?;

    // Call to `low_u128` here is safe because of the validation done in `validate_tx_value`
    let near_action = action
        .try_into_near_action(tx.value.raw().low_u128().saturating_mul(MAX_YOCTO_NEAR.into()))?;

    Ok((near_action, transaction_kind))
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L324-326)
```rust
    if tx.address.raw() != context.current_address {
        return Err(Error::Relayer(RelayerError::InvalidSender));
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L336-350)
```rust
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
```
