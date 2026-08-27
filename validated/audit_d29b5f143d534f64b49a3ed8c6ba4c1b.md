### Title
Repeated relayer-fee refund drain via deferred nonce increment in `EOABaseTokenTransfer{address_check: Some(_)}` path - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
In `inner_rlp_execute`, the relayer fee refund (`env::promise_batch_action_transfer(refund_promise, *fee)`) is scheduled unconditionally as soon as the transaction is parsed as `EOABaseTokenTransfer`, before the asynchronous address-registrar lookup resolves. When `address_check: Some(address)` is present, the nonce increment is deliberately deferred to `address_check_callback`, and that callback only bans the caller when the caller used a wallet-owned access key. A relayer that calls `rlp_execute` directly with its own account (no delegated access key) is never banned when the callback fails, so it can resubmit the exact same user-signed transaction (same nonce) indefinitely, collecting a fresh `fee` refund every time.

### Finding Description
`parse_rlp_tx_to_action` computes `tx_fee` from the user-signed Ethereum transaction and produces `TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer { address_check, fee })` for both `address_check: None` and `address_check: Some(address)` cases [1](#0-0) .

In `inner_rlp_execute`, when `address_check: Some(_)`, the nonce increment is skipped ("we still do not know if the transaction has a relayer error"), but the fee-refund promise is created immediately afterward, unconditionally, in the same code block regardless of the later registrar outcome: [2](#0-1) 

The registrar lookup and its callback happen asynchronously afterward: [3](#0-2) 

In `address_check_callback`, if the registrar confirms the address is actually a registered named account (`maybe_account_id.is_some()`), the relayer is deemed faulty. The nonce is intentionally *not* incremented ("An honest relayer may still be able to successfully send the user's intended transaction"). Critically, the relayer's key is only revoked if `env::signer_account_id() == current_account_id` (i.e., the caller used a wallet-delegated access key); otherwise the call simply returns a failure value with no penalty: [4](#0-3) 

The same "no ban, no nonce bump" outcome also occurs on `PromiseResult::Failed` (e.g., a failed registrar call): [5](#0-4) 

Since `rlp_execute` is a fully open, unprivileged entry point (any account can call it and supply `target`/`tx_bytes_b64`) [6](#0-5) , an attacker acting as a relayer using its own ordinary account (not a delegated wallet access key) can:
1. Obtain a validly user-signed base-token-transfer transaction whose `tx.to` address is registered in the address registrar (this is public information intended to be relayable by anyone).
2. Call `rlp_execute` with `target` set to the eth-implicit-format `AccountId` matching that address (which passes `is_valid_target` in `validate_tx_relayer_data`, since only hash-format compatibility is checked, not registration) [7](#0-6) .
3. `inner_rlp_execute` fires the fee refund immediately, leaves the nonce unchanged, and schedules the registrar lookup.
4. `address_check_callback` finds the address is registered, does not ban the attacker (since `signer_account_id != current_account_id`), and returns a failure response without incrementing the nonce.
5. Because the nonce is unchanged, the attacker repeats steps 2–4 with the identical signed transaction across as many subsequent calls/blocks as desired, receiving a new `fee` transfer from the wallet's own balance each time.

The `has_in_flight_tx` guard only prevents concurrent in-flight transactions; it is reset to `false` at the top of every callback and does not prevent sequential resubmission with the same nonce [8](#0-7) . The existing test `test_relayer_invalid_address_target` only exercises this callback branch with `gas_price: 0` (zero fee) and with an access-key relayer that gets banned, so it does not cover the non-access-key, nonzero-fee repeat-refund scenario [9](#0-8) .

### Impact Explanation
This allows theft of user funds: a single user-signed nonce, intended to authorize at most one relayer-fee refund, can be replayed by an uncooperative/unbanned relayer to drain the wallet contract's own $NEAR balance by `fee` on every retry, with no cap other than the wallet's balance. This matches the "theft of user funds" bounty category.

### Likelihood Explanation
The attack requires: (1) a user-signed `EOABaseTokenTransfer` with nonzero `max_fee_per_gas`/`gas_limit` targeting an address that is registered in the address registrar, and (2) the attacker submitting `rlp_execute` using its own ordinary account rather than a wallet-delegated access key. Both preconditions are attacker-controllable without any privileged access — the attacker only needs to relay a legitimately obtainable signed payload and choose an appropriate `target`/caller identity. The attack is fully repeatable across many blocks at low cost (only gas plus the registrar lookup), while each iteration returns `fee` from the wallet.

### Recommendation
Do not create the relayer fee refund promise until the registrar check (when `address_check: Some(_)`) has actually resolved and confirmed the relayer behaved honestly. Move the fee-refund promise creation into `address_check_callback`'s success branch (where the nonce is also incremented), so a fee is only ever paid out together with the nonce being consumed, guaranteeing at most one refund per user-signed nonce.

### Proof of Concept
Integration test (extending `tests/relayer.rs`/`tests/emulation.rs`):
1. Deploy a wallet contract and an address registrar; register some throwaway named account, capturing its resulting eth-style address `A`.
2. Have the wallet owner sign an `EOABaseTokenTransfer` with `to = A`, nonzero `gas_price`/`gas_limit` (nonzero `fee`), `nonce = 0`.
3. From an attacker account with no delegated access key on the wallet, call `rlp_execute(target = "0x{A}{suffix}", tx_bytes_b64 = signed_tx)` multiple times (e.g., 3 times) in sequence.
4. After each call, assert: `result.success == false`, `error` mentions "Invalid target", `wallet_contract.get_nonce() == 0` (unchanged), and the attacker's account is not banned (no revoked key check needed since no key was used).
5. Assert that after N repeated calls, the wallet contract's $NEAR balance has decreased by approximately `N * fee` (beyond normal gas costs), and the attacker/relayer's balance has increased by approximately `N * fee`, demonstrating unbounded repeat-refund drain for a single nonce.

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L89-128)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L140-140)
```rust
        self.has_in_flight_tx = false;
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L141-148)
```rust
        let maybe_account_id: Option<AccountId> = match env::promise_result(0) {
            PromiseResult::Failed => {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Call to Address Registrar contract failed".into()),
                });
            }
```

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs (L211-237)
```rust
    let relayer_pk = wallet_contract.register_relayer(&worker).await?;

    // The user submits a transaction to interact with the NEP-141 contract.
    let transaction = aurora_engine_transactions::eip_2930::Transaction2930 {
        nonce: 0.into(),
        gas_price: 0.into(),
        gas_limit: 0.into(),
        to: Some(Address::from_array(token_address)),
        value: Wei::zero(),
        data: [
            crate::eth_emulation::ERC20_BALANCE_OF_SELECTOR.to_vec(),
            ethabi::encode(&[ethabi::Token::Address(wallet_address)]),
        ]
        .concat(),
        chain_id: CHAIN_ID,
        access_list: Vec::new(),
    };
    let signed_transaction = crypto::sign_transaction(transaction, &wallet_sk);

    // Relayer fails to set `target` correctly
    let result =
        wallet_contract.rlp_execute(register_output.unwrap().as_str(), &signed_transaction).await?;

    assert!(!result.success);
    assert_eq!(result.error.as_deref(), Some("Error: faulty relayer"));

    assert_revoked_key(&wallet_contract.inner, &relayer_pk).await;
```
