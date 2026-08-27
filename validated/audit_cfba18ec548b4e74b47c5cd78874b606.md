### Title
Wallet Contract EVM `chain_id` is bucketed to a shared hardcoded constant, enabling cross-network replay of ETH-implicit signed transactions - (File: `runtime/near-wallet-contract/src/lib.rs`, `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs`)

### Summary
The Wallet Contract that powers ETH-implicit accounts (NEP-518) checks a per-network EVM `chain_id` as the replay-protection value for user-signed Ethereum-style transactions, analogous to the LooksRareExchange `DOMAIN_SEPARATOR`. Instead of deriving this value from each network's actual unique identity, it is a compile-time constant selected by a coarse string match on the NEAR `chain_id` config value, with a catch-all branch that maps every network that is not literally `"mainnet"` or `"testnet"` to the same `LOCALNET` contract/CHAIN_ID (399), and additionally maps `MOCKNET` to the exact same global contract hash (and thus same embedded CHAIN_ID 397) as `MAINNET`. This is the same class of bug as the reported issue: a fixed, non-unique replay-protection value baked in at "construction" time that does not distinguish between distinct chains sharing overlapping parameters, allowing a signature valid on one chain to be replayed on another.

### Finding Description
The Wallet Contract embeds a compiled constant `CHAIN_ID` (an EVM chain id, not the NEAR network's own chain id) that is used as the sole chain-binding value checked against the user's signed RLP transaction: [1](#0-0) 

This constant is validated in `validate_tx_relayer_data`, which is the only place chain identity is checked for the user-signed inner Ethereum transaction: [2](#0-1) 

The value of `CHAIN_ID` is fixed per compiled binary (397 mainnet / 398 testnet / 399 localnet), selected at build time: [3](#0-2) 

Critically, at runtime the *choice of which compiled wallet-contract binary/global-contract hash* (and therefore which embedded `CHAIN_ID`) is used for an ETH-implicit account is selected by `eth_wallet_global_contract_hash`, keyed only off the NEAR network's `chain_id` string, with a catch-all `_` branch for every network name that is not exactly `"mainnet"` or `"testnet"`, and an explicit `MOCKNET` alias for `MAINNET`: [4](#0-3) 

This means:
- Every NEAR network whose `chain_id` config string is not literally `"mainnet"` or `"testnet"` (any custom/private network, load-test network, or a network resulting from a fork that changes the visible network name only in non-mainnet/testnet ways) resolves to the identical `LOCALNET` global contract and therefore the identical embedded EVM `CHAIN_ID = 399`.
- `MOCKNET`, a distinct configured NEAR network id, is deliberately mapped to the same global contract hash (and CHAIN_ID = 397) as `MAINNET`.

Because the embedded `CHAIN_ID` (not the true, unique NEAR network chain id) is the only value binding a user's signed Ethereum-style transaction to a specific network, any two networks that fall into the same bucket share indistinguishable replay-protection domains — exactly the LooksRareExchange bug class: the "domain separator" component meant to bind a signature to one chain is reused, unmodified, across multiple distinct chains.

### Impact Explanation
An ordinary user who owns an ETH-implicit account (secp256k1-key derived NEAR account) with the same private key/account present on two networks that fall into the same `CHAIN_ID` bucket (e.g. mainnet and mocknet, or any two "other" networks both mapped to `LOCALNET`) can have a signed inner Ethereum transaction — a `Transfer`, `FunctionCall`, `AddKey`, or `DeleteKey` action executed via `rlp_execute` — accepted as valid on both networks, since `validate_tx_relayer_data` only checks `tx.chain_id != Some(CHAIN_ID)` and this constant is identical on both. If an attacker (a non-owning relayer or anyone who observes/obtains a previously broadcast signed RLP transaction) resubmits that same signed transaction on the other network via `rlp_execute` before/if the target account's nonce there has not yet advanced past it, the action (e.g. a fund transfer or an `AddKey` granting relayer permission) executes a second time — enabling replay-based theft of funds or unauthorized key addition on the "other" network, without requiring the account owner's cooperation on that network. This matches the "theft of funds via signature replay across forks/networks" impact class described in the external report.

### Likelihood Explanation
Reaching this requires no privileged role: it only needs an ordinary ETH-implicit account owner (or a relayer with access to previously signed transactions, since relayers are untrusted per the contract's own threat model — see `Error::Relayer` variants) and two live NEAR networks/environments that share a `CHAIN_ID` bucket. This is a realistic scenario in practice: NEAR explicitly operates a "mocknet" environment (used by the `tools/mirror` tooling to mirror/replay mainnet transaction traffic into a separate live environment) that is intentionally bucketed with `MAINNET`, and any custom/private/testing network not named exactly `"mainnet"`/`"testnet"` collapses into the same `LOCALNET` bucket as every other such network. No malicious node, malicious validator, or network-layer capability is required — only submission of an already-signed transaction to `rlp_execute` on the second network.

### Recommendation
Bind the Wallet Contract's inner Ethereum-transaction validation to the real, unique NEAR network chain id (or an EVM chain id derived 1:1 from it, with no fallback bucket), instead of a hand-picked constant selected via a coarse string match with a shared catch-all. At minimum, remove the `MOCKNET`→`MAINNET` aliasing in `eth_wallet_global_contract_hash` and the catch-all `_ => LOCALNET` branch in `wallet_contract_magic_bytes`/`eth_wallet_global_contract_hash`, replacing them with a derivation that produces a distinct `CHAIN_ID` per distinct NEAR `chain_id` string (e.g. hashing the NEAR `chain_id` into the EVM `CHAIN_ID` space), so that no two differently-named, independently operated networks can ever validate the same signed transaction.

### Proof of Concept
1. Operator A runs NEAR network `mocknet` (as used by `tools/mirror`), which resolves to the `MAINNET` global contract hash and thus `CHAIN_ID = 397`, per `eth_wallet_global_contract_hash`.
2. A user's ETH-implicit account exists with the same address/private key on both NEAR `mainnet` (`CHAIN_ID = 397`) and `mocknet` (`CHAIN_ID = 397`), for example because `mocknet` was seeded/mirrored from mainnet state (as designed by `tools/mirror/src/genesis.rs`, which explicitly re-signs/maps delegate actions and accounts from a source chain into a target chain).
3. The user signs an `eip_2930::Transaction2930`-style transfer with `chain_id: 397` to withdraw funds, and it is submitted via `rlp_execute` and accepted on `mainnet` per `validate_tx_relayer_data`'s `tx.chain_id != Some(CHAIN_ID)` check.
4. An attacker who has observed this signed transaction (e.g., from public execution logs/relayer mempool) resubmits the identical RLP bytes to `rlp_execute` targeting the mirrored account on `mocknet`. Because `mocknet` also uses `CHAIN_ID = 397` and, per the mirroring design, the account nonce there may not have advanced past this transaction's nonce, `validate_tx_relayer_data` accepts it and the transfer action executes again — resulting in a duplicated fund movement not authorized by a second signature from the user for that network. [4](#0-3) [2](#0-1)

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

**File:** runtime/near-wallet-contract/build.rs (L7-43)
```rust
const IMAGE_TAG: &str = "13430592a7be246dd5a29439791f4081e0107ff3";

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
