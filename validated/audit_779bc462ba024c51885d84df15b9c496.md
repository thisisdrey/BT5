### Title
Cross-network signature replay via shared hard-coded EVM chain ID in the Wallet Contract's "localnet" fallback - (File: `runtime/near-wallet-contract/src/lib.rs`)

### Summary
The ETH-implicit account "Wallet Contract" enforces EIP-155-style replay protection by checking that an incoming Ethereum-style transaction's `chain_id` field equals a value that is hard-coded into the WASM binary at build time. However, that hard-coded value is only distinct for the two well-known chain-id strings `"mainnet"` (397) and `"testnet"` (398); every other NEAR network — including any custom/private/staging network operators may deploy — falls into a single catch-all bucket that reuses the exact same "localnet" build (`CHAIN_ID = 399`, same WASM bytes, same global contract hash). This collapses the "chosen chainId" protection into a shared constant across an unbounded number of distinct chains, exactly the class of bug described in the MetaMask advisory (a chain-id value that does not uniquely identify the chain enables signed transactions to be replayed on an unintended chain).

### Finding Description
`wallet_contract_magic_bytes` and `eth_wallet_global_contract_hash` both match on the NEAR genesis `chain_id` string, with only `mainnet`/`testnet`(/`mocknet`) special-cased; every other value (any custom network name) falls to `_ => LOCALNET`: [1](#0-0) 

The `LOCALNET` build embeds a fixed EVM chain id of 399, set at compile time via `build.rs`: [2](#0-1) 

At runtime, the deployed wallet contract validates only that the transaction's `chain_id` equals this compiled-in constant — it has no way to know or check the actual NEAR chain it is running on: [3](#0-2) [4](#0-3) 

Because ETH-implicit account IDs are derived purely from the secp256k1 public key (`0x<address>`), the same private key produces the identical NEAR account ID on every NEAR network. If two independently operated custom networks both use a `chain_id` string other than `"mainnet"`/`"testnet"`, both deploy the exact same `LOCALNET` wallet-contract WASM with `CHAIN_ID = 399`, so a signed Ethereum-style transaction (an `rlp_execute` payload) that is valid and intended for one network is also structurally valid — same `chain_id`, same account id, same contract code — for the other network. `rlp_execute` is a public, unauthenticated entry point that anyone (not just the intended relayer) can submit: [5](#0-4) 

The only remaining barrier is the per-contract-instance nonce (`get_nonce`), which is independent per NEAR account/network and starts at 0, so a first (nonce-0) transaction — e.g. account creation-triggering transfer, an `AddKey` granting a relayer `FunctionCallPermission`, or a plain fund transfer — signed for use on Network A is trivially replayable verbatim on Network B if the victim's eth-implicit account there is at the same nonce, which is the common case for freshly-created accounts.

### Impact Explanation
This breaks the transaction-domain-separation guarantee the Wallet Contract is designed to provide (analogous to EIP-155). An attacker who observes a user's signed `rlp_execute` payload on one custom/non-canonical NEAR network can resubmit it to any other custom network sharing the same `LOCALNET` wallet build, causing unintended execution of the user's authorized action (transfer, `AddKey`, function call) on a chain the user never intended to interact with. Depending on the replayed action this can result in theft of funds, unauthorized relayer key grants (authorization escalation), or unintended contract calls — i.e., concrete impact categories such as "theft of funds" and "authorization escalation across accounts."

### Likelihood Explanation
This requires: (1) at least two live NEAR-protocol networks whose genesis `chain_id` is not literally `"mainnet"` or `"testnet"`, and (2) a user (or relayer) whose signed Wallet Contract transaction becomes observable off one of those networks (e.g., broadcast to a public mempool/relayer, or simply reused by the same user across environments believing chain separation is enforced). Given that nearcore is explicitly designed to support arbitrary custom/private deployments (testnets, devnets, enterprise chains) all sharing the same wallet-contract build artifacts shipped in the binary, this scenario is realistic wherever operators stand up secondary or staging networks and reuse the same eth-implicit account infrastructure — a materially different risk profile than a single canonical mainnet/testnet pair.

### Recommendation
Derive the EVM chain id embedded in / checked by the Wallet Contract from the actual NEAR network identity at deployment or execution time rather than from a small hard-coded enum with a shared catch-all bucket. Concretely: allocate a unique EVM chain id per NEAR `chain_id` string (e.g., a deterministic derivation function keyed on the genesis chain id or genesis hash) rather than defaulting every non-mainnet/testnet chain to the single shared `LOCALNET`/399 build, and reject deployment of the wallet contract global-contract hash on a network whose chain id it wasn't built for.

### Proof of Concept
1. Stand up two nearcore networks, `net-a` and `net-b`, each with a custom genesis `chain_id` (e.g. `"custom-a"`, `"custom-b"`), neither equal to `"mainnet"`/`"testnet"`. Per `eth_wallet_global_contract_hash`/`wallet_contract_magic_bytes`, both resolve to `LOCALNET` (`CHAIN_ID = 399`, identical WASM hash).
2. On `net-a`, fund an ETH-implicit account for public key `PK` (creating it with the global wallet contract), then have the corresponding secret key sign an `eip_2930::Transaction2930` with `chain_id: 399`, `nonce: 0`, e.g. an `AddKey` action granting a `FunctionCallPermission` to an attacker-controlled relayer key, and submit it via `rlp_execute`.
3. Independently, on `net-b`, create the same ETH-implicit account (same `PK` ⇒ same `0x...` account id) by funding it, then submit the identical RLP bytes captured from step 2 to `net-b`'s wallet contract's `rlp_execute`. Because `tx.chain_id (399) == CHAIN_ID (399)` and `nonce == expected_nonce (0)`, the transaction is accepted and the same `AddKey`/transfer action executes on `net-b`, even though it was never authorized for that chain.

### Citations

**File:** runtime/near-wallet-contract/src/lib.rs (L74-105)
```rust
pub fn wallet_contract_magic_bytes(chain_id: &str) -> Arc<ContractCode> {
    match chain_id {
        chains::MAINNET => MAINNET.magic_bytes(),
        chains::TESTNET => TESTNET.magic_bytes(),
        _ => LOCALNET.magic_bytes(),
    }
}

/// Returns the global contract hash for the ETH wallet contract on a given chain.
/// This is the hash of the deployed global contract that ETH implicit accounts
/// should use when the EthImplicitGlobalContract protocol feature is enabled.
///
/// For other chains (localnet, test chains): Uses the hash of the embedded
/// wallet contract WASM, allowing tests to deploy the same contract as a
/// global contract.
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-114)
```rust
    #[payable]
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
