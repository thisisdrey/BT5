## Title
Eth-implicit account creation hardcodes an unverified global-contract hash that can desynchronize from the actually-deployed wallet contract, permanently bricking new ETH-implicit accounts - (File: `runtime/near-wallet-contract/src/lib.rs`)

### Summary
This maps to the same bug class as the reported UniswapV2 issue: a manually-maintained hash constant, used to derive/reference on-chain code, that is not cross-checked against the real deployed bytecode. In nearcore, `eth_wallet_global_contract_hash()` returns hardcoded `CryptoHash` byte literals per chain that are supposed to equal the hash of a `GlobalContract` wallet-contract WASM blob that must be separately deployed on-chain. Every ETH-implicit account creation blindly stores this hardcoded hash as its `AccountContract::Global` pointer, with no on-chain verification that a global contract with that hash exists.

### Finding Description
`eth_wallet_global_contract_hash` returns literal byte arrays for mainnet/mocknet and testnet: [1](#0-0) 

Unlike `wallet_contract_magic_bytes()`/`magic_bytes()`, which derive the hash live from the embedded `res/wallet_contract_*.wasm` bytes (`format!("near{}", wallet_contract.hash())`, `runtime/near-wallet-contract/src/lib.rs:147-155`), these mainnet/testnet constants are decoupled from any bytes in the repository — they are asserted only by a unit test that compares them to themselves as base58 strings: [2](#0-1) 

This hash is consumed, unvalidated, at ETH-implicit account creation time — triggered by an ordinary, unprivileged `Transfer` action from any signer to a `0x…` receiver: [3](#0-2) 

There is no check here (or anywhere in the call path) that a `GlobalContract` with `code_hash == global_contract_hash` is actually present in state; the account is unconditionally created with `AccountContract::Global(global_contract_hash)`. The same hash is also relied upon when resolving a legacy account's contract for execution: [4](#0-3) 

If the constant returned by `eth_wallet_global_contract_hash` is ever wrong or goes stale relative to the actual deployed global-contract wallet code (e.g., after a wallet-contract upgrade like the one already documented for `OLD_TESTNET`, where the constant is updated in source but the corresponding on-chain global-contract deployment is missing, delayed, or mismatched), then:
- `RuntimeContractIdentifier::resolve` (`runtime/runtime/src/contract_code.rs`) will point at a `GlobalContractIdentifier::CodeHash` that has no code stored under it.
- `Contract::get_code`/loading in `near-vm-runner` (see `runtime/near-vm-runner/src/wasmtime_runner/mod.rs:722-724`, `VMRunnerError::ContractCodeNotPresent`) will fail for every call into the account.

### Impact Explanation
Every newly created ETH-implicit account (triggered by any ordinary user sending funds to a `0x…` address) would be created pointing at nonexistent/wrong contract code. Because ETH-implicit accounts cannot receive a full-access key and can only be operated through the Wallet Contract's `rlp_execute` method, a mismatched/missing global contract hash means the account's funds become permanently unreachable — the owner has no other way to authorize a transfer out of that account. This is a direct, unprivileged-triggerable permanent freezing of funds, matching the severity class of the referenced report (a stale/incorrect hash used to route calls, bricking downstream functionality for all affected accounts/users).

### Likelihood Explanation
The trigger condition — an ordinary `Transfer` to a `0x…` address — is fully within reach of any unprivileged client. The vulnerability's likelihood centers on operational/release discipline: the hardcoded hash constants in `eth_wallet_global_contract_hash` must be kept in perfect lockstep with whatever global contract is actually deployed on each network, with no code-level safety net (no assertion at runtime that the global contract exists, unlike the `magic_bytes()` path which derives the hash live from bundled WASM). The repo's own history of needing an `OLD_TESTNET` special case for a previous wallet-contract bug demonstrates this exact class of hash/deployment desynchronization has already occurred once for this subsystem.

### Recommendation
- Prefer deriving `eth_wallet_global_contract_hash` the same way `magic_bytes()` derives its hash — directly from an embedded/reference copy of the deployed wallet-contract WASM — rather than hardcoding raw byte literals that must be manually kept in sync with a separate deployment step.
- Add a startup/genesis-time (or protocol-upgrade-time) invariant check that a `GlobalContract` with the configured hash actually exists in state before it is used as the target for new implicit-account creation, failing loudly instead of silently bricking future accounts.
- Add an integration test that deploys exactly the constant returned by `eth_wallet_global_contract_hash` for a given `chain_id` and confirms it matches a global contract retrievable from state, rather than merely asserting the literal against another literal.

### Proof of Concept
1. On any chain, before the corresponding wallet-contract global contract (matching the hardcoded hash in `eth_wallet_global_contract_hash`) has been deployed (or after it's redeployed with different bytes without updating the constant), have any funded account send a `Transfer` action to a `0x…` (ETH-implicit) account id.
2. `action_implicit_account_creation_transfer` (`runtime/runtime/src/actions.rs:247-261`) creates the account with `AccountContract::Global(eth_wallet_global_contract_hash(chain_id))` unconditionally — no existence check is performed.
3. Any subsequent `rlp_execute` call (or any FunctionCall) against that account resolves via `RuntimeContractIdentifier::resolve`/`Contract::get_code`, which fails with `ContractCodeNotPresent` because no code is stored under that hash.
4. The account, and any funds sent to it, are now permanently unusable — there is no access key and no way to invoke the (missing) wallet contract to move funds.

### Citations

**File:** runtime/near-wallet-contract/src/lib.rs (L89-105)
```rust
pub fn eth_wallet_global_contract_hash(chain_id: &str) -> CryptoHash {
    match chain_id {
        // 2zodJZK2e4nnv5AqwCRnenNSmkikXhEd7PPY6BmfTmW4
        chains::MAINNET | chains::MOCKNET => CryptoHash([
            0x1d, 0xaa, 0x83, 0x5c, 0x46, 0x37, 0xf7, 0xae, 0x3d, 0x92, 0x40, 0x95, 0xba, 0x3f,
            0x0b, 0xf2, 0x82, 0x9b, 0xcf, 0xa1, 0x7b, 0x10, 0x68, 0xcd, 0x58, 0xbd, 0x85, 0x3d,
            0xca, 0xd7, 0xce, 0xb5,
        ]),
        // 3PpYvRxBfC5BkZxTw8ZFG3D52w1ZRhvDDWirKoxphMDn
        chains::TESTNET => CryptoHash([
            0x23, 0x8f, 0xea, 0xc1, 0xf8, 0x6c, 0xc9, 0xf9, 0xf4, 0x00, 0x3e, 0x3f, 0x6d, 0x5a,
            0xeb, 0xc0, 0x4e, 0xae, 0xa9, 0xc3, 0x94, 0x03, 0x2b, 0xd2, 0x94, 0x70, 0xe9, 0x60,
            0x9b, 0x67, 0xf6, 0xc5,
        ]),
        _ => *LOCALNET.read_contract().hash(),
    }
}
```

**File:** runtime/near-wallet-contract/src/lib.rs (L193-202)
```rust
    #[test]
    fn test_eth_wallet_global_contract_hash_values() {
        let mainnet_expected: CryptoHash =
            "2zodJZK2e4nnv5AqwCRnenNSmkikXhEd7PPY6BmfTmW4".parse().unwrap();
        let testnet_expected: CryptoHash =
            "3PpYvRxBfC5BkZxTw8ZFG3D52w1ZRhvDDWirKoxphMDn".parse().unwrap();
        assert_eq!(eth_wallet_global_contract_hash(MAINNET), mainnet_expected);
        assert_eq!(eth_wallet_global_contract_hash(MOCKNET), mainnet_expected);
        assert_eq!(eth_wallet_global_contract_hash(TESTNET), testnet_expected);
    }
```

**File:** runtime/runtime/src/actions.rs (L247-261)
```rust
        AccountType::EthImplicitAccount => {
            let chain_id = epoch_info_provider.chain_id();

            // Use a deployed global contract for ETH implicit accounts.
            let global_contract_hash = eth_wallet_global_contract_hash(&chain_id);
            let storage_usage = fee_config.storage_usage_config.num_bytes_account
                + global_contract_hash.as_bytes().len() as u64;

            *account = Some(Account::new(
                deposit,
                Balance::ZERO,
                AccountContract::Global(global_contract_hash),
                storage_usage,
            ));
        }
```

**File:** runtime/runtime/src/contract_code.rs (L52-67)
```rust
        if account_id.get_account_type() == AccountType::EthImplicitAccount {
            // Accounts that look like eth implicit accounts and have existed prior to the
            // eth-implicit accounts protocol change (these accounts are discussed in the
            // description of #11606) may have something else deployed to them. Only return
            // something here if the accounts have a wallet contract hash. Otherwise use the
            // regular path to grab the deployed contract.
            if LegacyEthWallet::resolve(local_hash).is_some() {
                // ETH implicit wallet accounts use global contracts, including
                // those created in old protocol versions.
                let global_hash = eth_wallet_global_contract_hash(chain_id);
                return Ok(RuntimeContractIdentifier::Global {
                    code_hash: global_hash,
                    identifier: GlobalContractIdentifier::CodeHash(global_hash),
                });
            }
        }
```
