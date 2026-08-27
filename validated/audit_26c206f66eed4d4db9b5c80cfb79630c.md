## Title
Wallet Contract's security-critical anti-fraud check unconditionally trusts a single external, key-controlled Address Registrar account, enabling fund misdirection/freezing if that account is compromised - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`, `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs`)

### Summary
The eth-implicit Wallet Contract — a single global contract shared by every ETH-implicit account on NEAR — hardcodes a trust dependency on an external, independently-deployed "Address Registrar" account (`ADDRESS_REGISTRAR_ACCOUNT_ID`, baked in at compile time as `address-map.near`). The wallet contract calls this account's `lookup` method and treats the *unauthenticated cross-contract call result* as ground truth to decide whether a relayer has correctly routed funds to a legitimate named NEAR account, rather than to a throw-away eth-implicit shell address. This is structurally identical to the reported bug class: a contract's accounting/security decision depends on state owned and mutable by a separate, independently-keyed account rather than being embedded in (or cryptographically bound to) the contract itself.

### Finding Description
`WalletContract::rlp_execute` decodes a user-signed Ethereum transaction and determines the NEAR `target` account supplied by the (untrusted) relayer [1](#0-0) . For base-token transfers whose target parses as an `EthImplicit` address, `parse_rlp_tx_to_action` schedules an `address_check` against the Address Registrar before finalizing the transfer [2](#0-1) .

The actual cross-contract call is made in `inner_rlp_execute`, which hardcodes the registrar's account id and forwards its answer, via `address_check_callback`, as the sole authority on whether the destination address collides with an existing named account: [3](#0-2) 

`address_check_callback` then makes the fund-routing decision purely from that callback result: if the registrar says the address is *not* registered, the contract proceeds to transfer funds to `target` (an eth-implicit account, i.e. one that only a matching secp256k1 private key can control) instead of the real named account; if it says the address *is* registered but the relayer didn't route there, the relayer is banned: [4](#0-3) 

Crucially, `ADDRESS_REGISTRAR_ACCOUNT_ID` is just a regular NEAR account holding a deployed `AddressRegistrar` contract with no owner-privileged mutation methods exposed in its public interface, but it is still a normal account that can be re-deployed/controlled by whoever holds its access key(s) [5](#0-4) . Nothing in the Wallet Contract cryptographically verifies that the registrar's response is authentic beyond "some contract at that account id returned an `Option<AccountId>`" — this is the exact analog of the ABC Labs finding, where a downstream program (`folio`) computed critical accounting from data in an account owned/controlled by a different, independently-keyed authority (`dtfs`), rather than data it owns or can cryptographically verify.

### Impact Explanation
If the key(s) controlling `address-map.near` are compromised (or if that account is ever redeployed with malicious logic), an attacker can make `lookup` always return `None`. Because the Wallet Contract is the *single global contract* used by all ETH-implicit accounts (deployed once via `GlobalContractIdentifier`/`eth_wallet_global_contract_hash`, see `runtime/runtime/src/actions.rs:247-261`), this single point of compromise defeats the anti-fraud/collision check for every ETH-implicit account on the network simultaneously. In combination with a colluding or malicious relayer, this lets an attacker consistently redirect legitimate base-token transfers away from their intended named-account recipient into throwaway eth-implicit addresses that (by construction) are hashes of that named account and correspond to no known private key — resulting in permanent loss/freezing of user funds network-wide, without the relayer being banned as the honest-relayer check intends. This satisfies the "permanent freezing of funds" bar.

### Likelihood Explanation
Exploitation requires compromising the access key(s) of the `address-map.near` deployment account (or a colluding party controlling it) plus a relayer willing to route transactions incorrectly — an insider/compromise scenario analogous to the original report's threat model (key compromise of a privileged, non-protocol-enforced account). It does not require any protocol-level bug, chunk-producer collusion, or validator misbehavior — an ordinary relayer/user flow through the public `rlp_execute` entrypoint is sufficient once the registrar is compromised.

### Recommendation
Do not let a single, externally-keyed account be the sole source of truth for a security-critical decision made by a globally shared contract. Options include: making the registrar's mapping tamper-evident/append-only and enforced at the protocol level (not just convention in a regular contract with a regular access key), removing full-access keys from the registrar account after deployment (or governing it via a DAO/multisig with strict method allow-lists), or moving the "is this address a named account" check to a mechanism the Wallet Contract itself can verify cryptographically rather than trusting an arbitrary cross-contract call result.

### Proof of Concept
Conceptual PoC (matches the exploit scenario structure of the original report):
1. Deploy the Wallet Contract as the network's global ETH-implicit contract, which hardcodes `ADDRESS_REGISTRAR_ACCOUNT_ID = "address-map.near"` [6](#0-5) .
2. Alice registers her named account `alice.near` in the registrar so that its address is on record, as exercised in `test_relayer_invalid_address_target` [7](#0-6) .
3. Mallory compromises the key controlling `address-map.near` and redeploys it (or otherwise forces `lookup` to return `None` for `alice.near`'s address).
4. A user intending to send ETH-emulated funds to `alice.near` (via her collision address) is routed by a relayer straight into a throwaway eth-implicit account matching that address, per the `address_check: None` path in `address_check_callback` [8](#0-7) , instead of being flagged and the relayer banned as in the honest-registrar case, permanently misrouting the funds.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L26-28)
```rust
const MICRO_NEAR: u128 = 10_u128.pow(18);
const ADDRESS_REGISTRAR_ACCOUNT_ID: &str = std::include_str!("ADDRESS_REGISTRAR_ACCOUNT_ID");
/// This storage deposit value is the one used by the standard NEP-141 implementation,
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L89-114)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L134-189)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L412-431)
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs (L188-208)
```rust
    // Deploy a NEP-141 contract and register its address.
    // Registering should prevent a lazy relayer from setting the target incorrectly.
    let token_contract = nep141::Nep141::deploy(&worker).await?;
    let register_output: Option<String> = address_registrar
        .call("register")
        .args_json(serde_json::json!({
            "account_id": token_contract.contract.id().as_str()
        }))
        .max_gas()
        .deposit(NearToken::from_millinear(1))
        .transact()
        .await?
        .json()?;
    let token_address: [u8; 20] =
        hex::decode(register_output.as_ref().unwrap().strip_prefix("0x").unwrap())?
            .try_into()
            .unwrap();
    assert_eq!(
        token_address,
        account_id_to_address(&token_contract.contract.id().as_str().parse().unwrap(),).0
    );
```
