### Title
Hardcoded EVM `CHAIN_ID` in the ETH-implicit Wallet Contract enables cross-fork transaction replay - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs`)

### Summary
The Wallet Contract that governs every ETH-implicit account on NEAR embeds a fixed EVM `chain_id` (397 for mainnet, 398 for testnet, 399 for localnet) at *compile time*, and uses it as the sole replay-protection value when validating user-signed Ethereum-style transactions. This mirrors the Golom `EIP712_DOMAIN_TYPEHASH` bug: the chain identifier is baked into the deployed bytecode rather than being derived from a value that would necessarily diverge across a chain split, so if NEAR ever undergoes a contentious hard fork that produces two live networks sharing the same original genesis/binary, both resulting chains keep an identical `CHAIN_ID` and identical Wallet Contract global-contract hash, permitting a signed wallet transaction to be replayed on either fork.

### Finding Description
`CHAIN_ID` is a `const` compiled into the Wallet Contract WASM from a static file, one value per network name ("mainnet"/"testnet"/"localnet"): [1](#0-0) 

This constant is the only chain-binding check performed on every incoming Ethereum-style transaction relayed through `rlp_execute`: [2](#0-1) 

The build process fixes this value per network name at compile time, independent of the actual live chain that ends up running that binary: [3](#0-2) 

At runtime, the deployed global contract hash for ETH-implicit accounts is selected purely by the genesis `chain_id` string ("mainnet"/"testnet"), not by anything unique to a specific fork/lineage of that network: [4](#0-3) 

and this hash is what gets attached to every new ETH-implicit account when it's created via transfer: [5](#0-4) 

Because `CHAIN_ID` and the wallet contract's global-contract hash are keyed only by the network name (a string like `"mainnet"`) and not by any fork-specific/lineage-specific identifier, two networks resulting from a hard fork of the same original chain (e.g., a contentious fork where both branches continue calling themselves "mainnet", or an intentional fork that forgets to rebuild the wallet contract with a new chain id) would run byte-identical Wallet Contract code with the same `CHAIN_ID`. A user's ETH-style signed transaction (RLP-encoded, chain_id-bound per EIP-155/EIP-2930 conventions) that is valid and executable on one fork is therefore also valid and executable, verbatim, on the other fork.

This is structurally analogous to the referenced Golom finding: a chain-binding value is computed/hardcoded once (at contract build/deploy time) instead of being derived from something that reliably diverges across a hard fork, defeating its purpose as replay protection across forked chains.

### Impact Explanation
If exploited, an attacker (or just an ordinary relayer/observer) who captures a signed Ethereum-style transaction destined for a user's ETH-implicit account on one branch of a NEAR fork could replay the exact same signed bytes against the sibling branch, causing the same value-transfer, `AddKey`, or function-call action to execute twice — once on each forked chain — against the user's balance/keys on both chains. This can lead to unintended double-execution of fund transfers or unauthorized key additions on the forked chain the user did not intend to transact on, i.e., a form of double-spend/replay across chain instances for every ETH-implicit account. Since the Wallet Contract governs *all* ETH-implicit accounts (a NEP-518 feature), the blast radius is every user who has ever used the eth-implicit wallet path.

### Likelihood Explanation
Likelihood is low-to-moderate and, as with the original judged finding, contingent on an external event (a hard fork producing two live chains from the same lineage) — this exactly matches the accepted Medium-severity reasoning in the source report ("very high-impact scenario, but relies on an external factor... hard forks can and do happen"). Within the current single-chain NEAR mainnet/testnet, there is no way to trigger this bug because chain_id values are distinct per network and no live replay path exists absent an actual fork event.

### Recommendation
- Bind the Wallet Contract's replay protection to a value that is guaranteed to diverge across a hard fork (e.g., derive/allow updating `CHAIN_ID` from a value tied to the actual live genesis/fork identity rather than a static per-network-name constant baked into the WASM at build time), or
- Provide a governance-controlled mechanism to rotate the EVM `chain_id` used by the deployed Wallet Contract global contract independent of rebuilding/redeploying the WASM, so that in the event of a fork, the surviving/forked network can be given a distinct chain id promptly, invalidating replay of old signed transactions on the other branch.
- At minimum, document this trust assumption clearly so any hard-fork runbook includes rotating the wallet contract chain id / global contract hash for the fork that is not the canonical continuation.

### Proof of Concept
1. NEAR mainnet forks into chain A (continues as canonical "mainnet") and chain B (a fork resulting from a contentious protocol disagreement), both starting from identical state, including all ETH-implicit accounts backed by the `AccountContract::Global` wallet contract hash returned by `eth_wallet_global_contract_hash("mainnet")`.
2. Both chains run byte-identical Wallet Contract code compiled with `CHAIN_ID = 397` (per `runtime/near-wallet-contract/build.rs` mainnet build step).
3. A user signs an Ethereum-style `Transaction2930` (e.g., a `Transfer` action) with `chain_id: 397`, targeting their ETH-implicit account, intending it to execute only on chain A.
4. Because `validate_tx_relayer_data` only checks `tx.chain_id != Some(CHAIN_ID)` (`internal.rs:328`) — and `CHAIN_ID` is identical on both A and B — a relayer can submit the identical signed transaction bytes to the Wallet Contract instance on chain B as well, and it passes validation and executes there too, replaying the user's action on the fork they did not intend.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L16-20)
```rust
/// The chain ID is pulled from a file to allow this contract to be easily
/// compiled with the appropriate value for the network it will be deployed on.
/// The chain ID for Near mainnet is [397](https://chainlist.org/chain/397)
/// while the value for testnet is [398](https://chainlist.org/chain/398).
pub const CHAIN_ID: u64 = std::include!("CHAIN_ID");
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L324-330)
```rust
    if tx.address.raw() != context.current_address {
        return Err(Error::Relayer(RelayerError::InvalidSender));
    }

    if tx.chain_id != Some(CHAIN_ID) {
        return Err(Error::Relayer(RelayerError::InvalidChainId));
    }
```

**File:** runtime/near-wallet-contract/build.rs (L9-43)
```rust
/// See https://chainlist.org/chain/397
const MAINNET_CHAIN_ID: u64 = 397;

/// See https://chainlist.org/chain/398
const TESTNET_CHAIN_ID: u64 = 398;

/// Not officially registered on chainlist.org because this is for local testing only.
const LOCALNET_CHAIN_ID: u64 = 399;

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
```

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
