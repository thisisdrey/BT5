### Title
Hardcoded ETH-wallet global-contract hash constant can desynchronize from the deployed contract, permanently bricking ETH-implicit accounts - (File: runtime/near-wallet-contract/src/lib.rs)

### Summary
The reported bug class is a hardcoded "init code hash" constant used to derive/reference a contract address, which had drifted from the actual contract bytecode, breaking address derivation. The closest reachable analog in nearcore is `eth_wallet_global_contract_hash` in [1](#0-0) , a hand-hardcoded `CryptoHash` byte array per chain that is used — independently of the actual embedded wallet-contract WASM — to point every newly created ETH-implicit account at a specific `Global` contract deployment.

### Finding Description
Unlike `wallet_contract_magic_bytes`, whose hash is *derived at runtime* from the embedded WASM (`format!("near{}", wallet_contract.hash())`, [2](#0-1) ), `eth_wallet_global_contract_hash` for `MAINNET`/`TESTNET` is a literal byte array typed in by hand, decoupled from `MAINNET.read_contract().hash()` / `TESTNET.read_contract().hash()`. Only the `LOCALNET` branch derives the hash dynamically (`*LOCALNET.read_contract().hash()`), [3](#0-2) .

The unit test `test_eth_wallet_global_contract_hash_values` only asserts the hardcoded constants equal themselves (parsed from the same literal base58 strings) — it never cross-checks them against the embedded `wallet_contract_mainnet.wasm` / `wallet_contract_testnet.wasm` bytecode hash [4](#0-3) . So there is no compile-time or test-time guarantee that this constant matches whatever contract is actually deployed on-chain as the global contract referenced by that hash — exactly the same class of failure as the Uniswap `init code hash` constant silently drifting from the actual factory bytecode.

This hash is consumed in `action_implicit_account_creation_transfer` when creating a brand-new ETH-implicit account: the account is unconditionally created with `AccountContract::Global(global_contract_hash)` from this constant, with no on-chain check that a global contract with that exact code hash has actually been deployed [5](#0-4) .

### Impact Explanation
If the hardcoded constant (`eth_wallet_global_contract_hash` for mainnet/testnet) does not match the code hash of the global contract actually deployed on that chain — e.g. due to a future wallet-contract WASM update where this constant is forgotten to be bumped, or a copy/paste/typo error introducing the wrong bytes — every ETH-implicit account created afterward would be permanently associated with a non-existent (or wrong) `Global(hash)` contract. Per the account model, ETH-implicit accounts can only be used through the Wallet Contract's methods; they cannot have a full access key added and cannot be deleted [6](#0-5) . If the referenced global contract cannot be resolved, function calls to that account (the only way to move funds out of it) would fail permanently, resulting in funds sent to such accounts being frozen with no recovery path — matching the "permanent freezing of funds" impact category.

### Likelihood Explanation
This is a latent, not-yet-triggered issue rather than an outright shipped defect: the current bundled `wallet_contract_mainnet.wasm`/`wallet_contract_testnet.wasm` bytecode may or may not match these hardcoded constants — I could not compute or verify that hash equivalence from static analysis of the index alone (this requires hashing the actual `.wasm` resource files, which are binary and outside what the code-search index exposes). The structural risk is real and directly analogous to the reported bug class: any future update to the wallet-contract WASM resource, or any manual transcription error in the hardcoded byte arrays, silently desynchronizes the constant from the deployed contract with no test catching it, since the existing test is a tautology (constant vs. itself) rather than a check against `read_contract().hash()`.

### Recommendation
Replace the hardcoded `CryptoHash` literals for `MAINNET`/`TESTNET`/`MOCKNET` in `eth_wallet_global_contract_hash` with values derived from `MAINNET.read_contract().hash()` / `TESTNET.read_contract().hash()`, consistent with how `LOCALNET` and `wallet_contract_magic_bytes` already work, so the constant can never drift from the actual embedded/deployed bytecode. At minimum, add a test asserting `eth_wallet_global_contract_hash(MAINNET) == *MAINNET.read_contract().hash()` (and similarly for testnet) so any future WASM update that isn't matched by a constant update fails CI instead of silently bricking new ETH-implicit accounts.

### Proof of Concept
Not applicable as a live exploit — I could not verify from the codebase alone whether the currently hardcoded values in `eth_wallet_global_contract_hash` ( [1](#0-0) ) actually match the hash of the bundled `res/wallet_contract_mainnet.wasm` / `res/wallet_contract_testnet.wasm` binaries (that requires hashing the binary resource files, which the available tools do not expose). The vulnerability, if the constants are or ever become out of sync, would manifest as: (1) a new ETH-implicit account is created via a transfer, receiving `AccountContract::Global(hardcoded_hash)`; (2) any `FunctionCall`/`rlp_execute` receipt against it fails to resolve the referenced global contract code; (3) since the account can never receive a full access key or be deleted, its balance becomes permanently unrecoverable.

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

**File:** runtime/near-wallet-contract/src/lib.rs (L147-155)
```rust
    fn magic_bytes(&self) -> Arc<ContractCode> {
        self.magic_bytes
            .get_or_init(|| {
                let wallet_contract = self.read_contract();
                let magic_bytes = format!("near{}", wallet_contract.hash());
                Arc::new(ContractCode::new(magic_bytes.into_bytes(), None))
            })
            .clone()
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

**File:** docs/DataStructures/Account.md (L119-122)
```markdown
Once a NEAR-implicit account is created it acts as a regular account until it's deleted.

An ETH-implicit account can only be used by calling the methods of the [Wallet Contract](#wallet-contract). It cannot be deleted, nor can a full access key be added.
The primary purpose of ETH-implicit accounts is to enable seamless integration of existing Ethereum tools (such as wallets) with the NEAR blockchain.
```
