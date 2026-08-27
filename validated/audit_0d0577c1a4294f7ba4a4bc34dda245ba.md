### Title
Wallet Contract's address-registrar account ID is a hard-coded, network-independent constant, breaking/spoofing the anti-phishing address check on non-mainnet deployments - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The ETH-implicit Wallet Contract embeds a single compile-time constant, `ADDRESS_REGISTRAR_ACCOUNT_ID`, that is read from a static file and baked into the WASM via `include_str!` [1](#0-0) . The file's content is `address-map.near` [2](#0-1) . The wallet-contract `build.rs` compiles three separate WASM artifacts (`wallet_contract_mainnet`, `wallet_contract_testnet`, `wallet_contract_localnet`) and swaps only the `CHAIN_ID` file between builds; it never touches `ADDRESS_REGISTRAR_ACCOUNT_ID` [3](#0-2) . This is functionally identical to the reported WETH bug: a single mainnet-specific address is compiled into artifacts meant for multiple independent networks.

### Finding Description
`inner_rlp_execute` uses this constant whenever a user's Ethereum-emulated transaction is a base-token transfer to another ETH-implicit account and requires an anti-phishing address check (`EOABaseTokenTransfer { address_check: Some(address), .. }`). It parses `ADDRESS_REGISTRAR_ACCOUNT_ID` into an `AccountId` and calls `lookup()` on it to see whether a named account is already registered at that Ethereum address, in order to prevent relayers from redirecting funds meant for a named account to its raw hashed-address form [4](#0-3) . The result is consumed in `address_check_callback`, which either bans a malicious relayer or proceeds with the transfer based on the lookup response [5](#0-4) .

Because the same `address-map.near` string is embedded in the testnet and localnet wallet contract binaries as in the mainnet one, and because NEAR testnet permits permissionless creation of any top-level account (unlike mainnet, where creation of short top-level names is restricted to the registrar, see `action_create_account` / `min_allowed_top_level_account_length` checks in `runtime/runtime/src/actions.rs`), an attacker can simply register the top-level account `address-map.near` on testnet and deploy their own contract there implementing the same `lookup(address) -> Option<AccountId>` interface used by `ext_registrar` [6](#0-5) . This is essentially the same operational reference used by test infrastructure to point the wallet contract at an arbitrary registrar account, confirming the value is a pure, swappable configuration parameter, not any protocol-enforced binding [7](#0-6) .

With a malicious registrar in place, the attacker fully controls the safety-critical branch of `address_check_callback`: they can make `lookup()` always return `None`, causing the contract to treat a target address as unclaimed and proceed with sending funds to the attacker-hashed address even when a legitimately named account owns that address (or the reverse — returning `Some` for arbitrary addresses to force wallet contracts into the "ban relayer" branch and grief legitimate relayers). Because this control-flow branch exists specifically to stop a malicious/faulty relayer from redirecting a user's intended named-account transfer to an unintended raw-address account, defeating it via a spoofed registrar undermines the wallet contract's core anti-phishing/anti-redirection guarantee on any non-mainnet deployment.

### Impact Explanation
On testnet/localnet deployments of the ETH-Wallet-Contract, the security check meant to prevent relayer-driven fund redirection can be neutralized by an attacker who front-runs/squats the `address-map.near` account name and deploys a spoofed registrar contract, because the wallet-contract WASM for every network hard-codes the same registrar account id rather than deriving it per-chain. This can enable relayer-facilitated redirection of a user's NEAR transfer to an address the user did not intend, i.e., theft of transferred funds via the exact mechanism this check is designed to prevent.

### Likelihood Explanation
Medium: the code path is only exercised for `EOABaseTokenTransfer` transactions where `address_check` is `Some(..)` (a subset of ETH-emulated transfers), and requires an attacker to first claim the `address-map.near` top-level name on the target network, which is realistically achievable on testnet given permissive top-level account creation there. The vulnerability does not affect mainnet directly (the real registrar is presumably `address-map.near` on mainnet), but it does affect every other network configuration the same build artifact family targets, matching the reported bug class of "single compile-time constant assumed valid across all deployments."

### Recommendation
Do not bake a single global registrar account id into all network variants of the wallet contract WASM. Either:
- Vary `ADDRESS_REGISTRAR_ACCOUNT_ID` per network the same way `CHAIN_ID` is varied in `build.rs`, ensuring each network's binary points at that network's actual, trusted registrar deployment, or
- Store/derive the registrar account id from a value that is provably owned/controlled the same way on every network (e.g., pin it to the wallet contract's own top-level namespace or a value gated by governance per chain), and
- At minimum, assert at contract initialization / build time that the registrar account referenced actually exists and is controlled by the expected deployer before shipping a network-specific WASM artifact.

### Proof of Concept
1. Build the wallet contract for testnet using `runtime/near-wallet-contract/build.rs`; observe that only `CHAIN_ID` differs from the mainnet build, while `ADDRESS_REGISTRAR_ACCOUNT_ID` remains `address-map.near` for both [3](#0-2) .
2. On testnet, register the top-level account `address-map.near` (permitted because testnet does not restrict top-level account creation the way mainnet does) and deploy a contract there implementing `lookup(address: String) -> Option<AccountId>` that always returns `None`.
3. Have a malicious/compromised relayer submit an `rlp_execute` call for a victim's ETH-implicit account performing an `EOABaseTokenTransfer` to a target address that is actually owned by a named account.
4. `inner_rlp_execute` calls `address_registrar.lookup(address)` against the attacker's spoofed `address-map.near` contract, which returns `None` [8](#0-7) .
5. `address_check_callback` treats the target as an ordinary (unregistered) address and forwards the transfer instead of banning the relayer or rejecting the transaction [9](#0-8) , allowing the attacker-controlled relayer to redirect funds meant for the named account to the raw address form.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L26-27)
```rust
const MICRO_NEAR: u128 = 10_u128.pow(18);
const ADDRESS_REGISTRAR_ACCOUNT_ID: &str = std::include_str!("ADDRESS_REGISTRAR_ACCOUNT_ID");
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L514-517)
```rust
#[near_sdk::ext_contract(ext_registrar)]
trait AddressRegistrar {
    fn lookup(&self, address: String) -> Option<AccountId>;
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/ADDRESS_REGISTRAR_ACCOUNT_ID (L1-1)
```text
address-map.near
```

**File:** runtime/near-wallet-contract/build.rs (L18-49)
```rust
fn main() -> anyhow::Result<()> {
    let contract_dir = "./implementation";

    build_contract(
        contract_dir,
        "eth_wallet_contract",
        "wallet_contract_mainnet",
        MAINNET_CHAIN_ID,
    )
    .context("Mainnet build failed")?;

    build_contract(
        contract_dir,
        "eth_wallet_contract",
        "wallet_contract_testnet",
        TESTNET_CHAIN_ID,
    )
    .context("Testnet build failed")?;

    build_contract(
        contract_dir,
        "eth_wallet_contract",
        "wallet_contract_localnet",
        LOCALNET_CHAIN_ID,
    )
    .context("Localnet build failed")?;

    println!("cargo:rerun-if-changed={}", contract_dir);
    println!("cargo:rerun-if-changed={}", "./res");

    Ok(())
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/utils/test_context.rs (L175-188)
```rust
    async fn deploy_address_registrar(worker: &Worker<Sandbox>) -> anyhow::Result<Contract> {
        let base_dir = Path::new(BASE_DIR).parent().unwrap().join("address-registrar");
        let contract_bytes = build_contract(base_dir, "eth-address-registrar").await?;
        let contract = worker.dev_deploy(&contract_bytes).await?;

        // Initialize the contract
        contract.call("new").transact().await.unwrap().into_result().unwrap();

        // Update the file where the Wallet Contract gets the address registrar account id from
        tokio::fs::write(address_registrar_account_id_path(BASE_DIR), contract.id().as_bytes())
            .await?;

        Ok(contract)
    }
```
