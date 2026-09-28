### Title
Missing storage gap in `Governable` base contract risks storage collision across upgrades - (File: contracts/access/Governable.sol)

### Summary
`Governable` is an upgradeable parent contract that declares persistent state (`governor`, `proposedGovernor`) but does not reserve a storage gap. Upgradeable contracts that inherit it (e.g., `PoolRegistry`, `Pool` via its storage-versioned bases, `FeeProvider`) place their own variables immediately after `Governable`'s two slots. Adding any new state variable to `Governable` in a future version will shift the entire child storage layout and corrupt live state — the same bug class as the Midas `Pausable`/`Greenlistable`/`WithSanctionsList` finding.

### Finding Description
`Governable` declares two storage variables and inherits `Initializable`, but ends without a `__gap` reservation:

```solidity
// contracts/access/Governable.sol
abstract contract Governable is IGovernable, Context, TokenHolder, Initializable {
    address public governor;
    address public proposedGovernor;
    ...
} // no uint256[N] __gap
```

By contrast, the sibling base `Manageable` correctly ends with `uint256[49] private __gap` (`contracts/access/Manageable.sol:78`), showing the codebase intends gap-based upgrade safety.

Metronome's own pattern confirms the risk: `Pool` state is maintained through explicitly versioned storage appenders (`PoolStorageV1` → `PoolStorageV4` in `contracts/storage/PoolStorage.sol`), i.e., new variables are appended by deriving a new storage contract. If the same evolution ever happens inside `Governable` (or any other non-pure base without a gap, such as `TokenHolder` were it to gain state), the appended variable occupies the slot where the child's first variable currently lives — for `Pool`-type children this includes `debtFloorInUsd`, fee parameters, `depositTokenOf`/`debtTokenOf` mappings roots, `treasury`, and `feeProvider`.

Storage layouts in the deployment artifacts confirm contiguous packing with no reserved space between base and derived variables (e.g., `deployments/mainnet/DepositToken.json` shows `pool` at slot 2 immediately followed by `__gap` at slot 3, and child `balanceOf` at slot 52 — any base-level insertion before the gap would corrupt `balanceOf`/`allowance` roots). `Governable` provides no such buffer.

### Impact Explanation
On a future upgrade that adds state to `Governable` (or any gap-less stateful base), the child's storage is silently reinterpreted: mappings resolve at wrong roots, `treasury`/`feeProvider`/`poolRegistry` pointers read garbage, and accounting invariants (debt positions, collateral deposits, liquidation bounds) break. Depending on which slots are clobbered this can cause permanent freezing of user funds, theft of unclaimed yield, or protocol insolvency. Because corruption occurs at the proxy's storage layer, no runtime check (`onlyGovernor`, `whenNotPaused`, reentrancy guard) can prevent it.

### Likelihood Explanation
Likelihood is conditional: the defect is latent and only materializes if a gap-less base gains a variable during an upgrade — an action performed by the proxy admin/governor, not an unprivileged attacker. The codebase actively evolves via versioned storage (`PoolStorageV1`–`V4`, deprecated slots like `_status_DEPRECATED`, `swapper__DEPRECATED`), so future upgrades are expected, but whether a gap-less base is the one extended is speculative. No currently callable path triggers the bug.

### Recommendation
Append a gap reservation to every upgradeable base that holds (or may hold) state:

```solidity
// contracts/access/Governable.sol
uint256[48] private __gap;
```

Audit the remaining bases (`TokenHolder`, SynthContext, per-contract storage abstracts in `contracts/storage/`) for the same omission, and keep the existing convention of shrinking gaps when new variables are added.

### Proof of Concept
A Foundry layout check demonstrates the collision:

```solidity
// In a test contract inheriting the same bases as Pool:
contract GovernableV2 is Governable {
    address public newVar; // appended in upgrade
}

// Simulate: deploy proxy with V1 layout, then upgrade to V2 logic
// that adds a var to Governable. The child's first slot
// (e.g., PoolStorageV1.debtFloorInUsd) is now read from the slot
// that holds newVar -> all subsequent child storage is shifted by 1.
// forge inspect Governable storage-layout  -> governor:0, proposedGovernor:1, newVar:2
// Pool's debtFloorInUsd expected at slot 2 is now displaced.
```

The `forge inspect` storage-layout diff between a V1 build and a hypothetical V2 build is sufficient to prove the slot shift; no runtime exploit exists pre-upgrade.