### Title
Hard-coded per-network wallet-contract global hash breaks ETH-implicit accounts on any non-mainnet/testnet/mocknet deployment - (File: `runtime/near-wallet-contract/src/lib.rs`)

### Summary
`eth_wallet_global_contract_hash()` hard-codes the global-contract code hash that every ETH-implicit account should be created with, but only for the three literal `chain_id` strings `"mainnet"`, `"mocknet"`, and `"testnet"`. Any other network's `chain_id` falls through to the `LOCALNET` wallet-contract hash, and this un-validated value is stamped onto every ETH-implicit account created on that network, exactly mirroring the reported bug class of a compile-time, single-network constant being reused unconditionally across all deployments.

### Finding Description
`eth_wallet_global_contract_hash` is defined as: [1](#0-0) 

For `chain_id` values other than the three matched literals (i.e., any custom or additional network built from this same nearcore codebase — analogous to deploying the "same bytecode" to a different chain in the original report), the function returns `*LOCALNET.read_contract().hash()` — the hash of the WASM built for local testing with `LOCALNET_CHAIN_ID = 399`: [2](#0-1) 

This value is consumed directly, without any existence check, when the runtime creates a new ETH-implicit account: [3](#0-2) 

Unlike the explicit `use_global_contract` action, which validates that the referenced global contract actually exists in state before attaching it to an account and otherwise fails with `GlobalContractDoesNotExist`: [4](#0-3) 

`action_implicit_account_creation_transfer` performs no such check — it unconditionally assigns `AccountContract::Global(global_contract_hash)` to the newly created account. If the operator of a network whose `chain_id` string is not literally `"mainnet"`, `"mocknet"`, or `"testnet"` has not separately deployed a global contract whose code hash coincidentally equals the embedded `LOCALNET` wallet-contract hash, every ETH-implicit account ever created on that network points at a global contract identifier that does not exist in that chain's state.

This is a direct structural analog of the reported bug: a single compile-time constant (there, the mainnet WETH9 address; here, the mainnet/testnet/mocknet global-contract hashes with a `LOCALNET` fallback) is baked into shared logic and silently reused for every other network built from the same codebase, with no assertion that the referenced address/contract is actually present on that chain.

### Impact Explanation
ETH-implicit accounts have no access key — they can only be controlled by the wallet-contract logic executing under the `AccountContract::Global` identifier via `rlp_execute` (as demonstrated in `integration-tests/src/tests/features/wallet_contract.rs`, `test_wallet_contract_interaction`, which deploys the wallet code as a global contract before any eth-implicit interaction is possible). If the account is created referencing a global-contract hash that is absent from the chain's state, the account has no way to execute the wallet-contract logic that authorizes outgoing transfers/RLP-encoded transactions. Any $NEAR (or NEP-141 tokens routed through it) sent to such an account is therefore effectively unrecoverable — a permanent freezing of funds, matching the "Accept only concrete theft or permanent freezing of funds" validation bar.

### Likelihood Explanation
This nearcore codebase is used as the base for more than the two/three canonical networks (mainnet/testnet/mocknet); any additional network, testing chain, or forked/derived network configured with a distinct `chain_id` string (analogous to "Arbitrum/Base" in the original report) will trigger this fallback path automatically for every ETH-implicit account creation, with no error or warning at genesis/build time.

### Recommendation
- Fail loudly (panic or reject the transaction) instead of silently defaulting to `LOCALNET`'s hash when `chain_id` does not match a known network in `eth_wallet_global_contract_hash`.
- Alternatively, make the eth-wallet global-contract hash a genesis/runtime-config parameter (verified against on-chain global contract existence) rather than a compiled-in per-network constant, and have `action_implicit_account_creation_transfer` verify the referenced global contract exists (as `use_global_contract` already does) before attaching it to a newly created account.

### Proof of Concept
1. Configure a nearcore network with `chain_id` set to anything other than the literal strings `"mainnet"`, `"mocknet"`, or `"testnet"` (e.g. a new named network `"my-l1"`), without deploying a global contract whose hash equals the embedded `LOCALNET` wallet-contract hash.
2. Send a transfer to a fresh `0x...` (ETH-implicit) account id on this network — this triggers `action_implicit_account_creation_transfer` → `AccountType::EthImplicitAccount` branch (`runtime/runtime/src/actions.rs:247-261`), which calls `eth_wallet_global_contract_hash("my-l1")` and receives the `LOCALNET` hash via the `_` fallback arm (`runtime/near-wallet-contract/src/lib.rs:103`).
3. The account is created with `AccountContract::Global(LOCALNET_hash)`, a hash that does not correspond to any global contract actually present in this network's state.
4. Attempting to interact with the account via `rlp_execute` (the only means of authorizing outgoing actions from an ETH-implicit account, per `test_wallet_contract_interaction`) fails because the referenced global contract cannot be resolved on-chain, permanently stranding any funds sent to the account.

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

**File:** runtime/near-wallet-contract/build.rs (L7-17)
```rust
const IMAGE_TAG: &str = "13430592a7be246dd5a29439791f4081e0107ff3";

/// See https://chainlist.org/chain/397
const MAINNET_CHAIN_ID: u64 = 397;

/// See https://chainlist.org/chain/398
const TESTNET_CHAIN_ID: u64 = 398;

/// Not officially registered on chainlist.org because this is for local testing only.
const LOCALNET_CHAIN_ID: u64 = 399;

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

**File:** runtime/runtime/src/global_contracts.rs (L75-97)
```rust
pub(crate) fn use_global_contract(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
    account: &mut Account,
    contract_identifier: &GlobalContractIdentifier,
    result: &mut ActionResult,
) -> Result<(), RuntimeError> {
    let key = TrieKey::GlobalContractCode { identifier: contract_identifier.clone().into() };
    if !state_update.contains_key(&key, AccessOptions::DEFAULT)? {
        result.result = Err(ActionErrorKind::GlobalContractDoesNotExist {
            identifier: contract_identifier.clone(),
        }
        .into());
        return Ok(());
    }
    clear_account_contract_storage_usage(state_update, account_id, account)?;
    if account.contract().is_local() {
        state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
    }
    let contract = match contract_identifier {
        GlobalContractIdentifier::CodeHash(code_hash) => AccountContract::Global(*code_hash),
        GlobalContractIdentifier::AccountId(id) => AccountContract::GlobalByAccount(id.clone()),
    };
```
