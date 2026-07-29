### Title
Griefing Safe launches by front-running `createProxyWithNonce` at the predicted CREATE2 address - (File: `contracts/SmartAccountFactory.sol`)

### Summary
`SmartAccountFactory.deploySmartAccount` derives a deterministic `saltNonce` from `msg.sender` and a public, incrementing per-deployer counter, then calls the permissionless `SafeProxyFactory.createProxyWithNonce` to deploy the Safe at a CREATE2 address computed from that salt. Because the salt/initializer inputs are fully predictable and `SafeProxyFactory` is directly callable by anyone, an attacker can pre-deploy a proxy to the exact address the legitimate call would target, causing the legitimate `deploySmartAccount` transaction to permanently revert for that nonce — the same "create the pool/address first" root cause as the reported Uniswap V3 issue.

### Finding Description
`deploySmartAccount` computes the salt as: [1](#0-0) 

`nonces[msg.sender]` is a public state variable, and `predictSmartAccountAddress` even exposes a view function that reproduces the exact same salt/initializer/CREATE2 derivation used internally: [2](#0-1) 

All other inputs to the initializer (`delegates`, `currencies`, `nftCollections`, `trustedRecipients`, `validUntil`, `owners`, `threshold`) are visible in the pending transaction's calldata before inclusion. This makes the exact CREATE2 address (and the exact `initializer` bytes) fully reproducible by any third party watching the mempool or simply reading `nonces(deployer)`.

`SAFE_PROXY_FACTORY` (`SafeProxyFactory.createProxyWithNonce`) is a standard, permissionless Safe contract — anyone can call it directly with the same `(singleton, initializer, saltNonce)` tuple. Since Safe's factory deploys via CREATE2 and reverts if a contract already exists at the computed address, an attacker can:

1. Observe (or precompute from `nonces(victim)`) the exact `initializer` and `saltNonce` that `deploySmartAccount` will use for a given deployer's next call.
2. Call `SafeProxyFactory.createProxyWithNonce(SAFE_SINGLETON, initializer, saltNonce)` directly, deploying the proxy to that address themselves.
3. When the legitimate `deploySmartAccount` transaction executes, its internal call to `createProxyWithNonce` targets the same now-occupied address and reverts, because the CREATE2 slot is already taken.

Because the revert happens before `nonces[msg.sender]` is durably incremented (the whole transaction, including the `++`, reverts), the *next* attempt by the same deployer recomputes the identical `deployerNonce`/`saltNonce` and is doomed to collide with the same pre-deployed address again — permanently blocking that deployer from ever using `deploySmartAccount` normally, mirroring the permanent one-time-griefing pattern from the Uniswap `createPool` report.

This is compounded by the fact that the pre-deployed Safe was configured with the exact same `owners`/`threshold`/delegates/approvals as the intended deployment (since the attacker had to replay the identical initializer to hit the same address), yet `isDeployedSmartAccount[safeAddress]` is never set to `true` because the attacker bypassed `SmartAccountFactory` entirely — corrupting the on-chain provenance registry that "lets integrators and users verify that a Safe account was deployed by this factory," per the spec: [3](#0-2) 

### Impact Explanation
This is a production DoS on the smart-account onboarding path: an unprivileged attacker can permanently block any specific `deployer` from creating their Tare Safe via `deploySmartAccount`, since the deterministic per-deployer nonce always recomputes the same doomed salt after a revert. It also desynchronizes the `isDeployedSmartAccount` provenance registry — an attacker (or even the victim retrying with a workaround) can end up with a fully-configured Safe (module enabled, TrustedSpender allowances set) that is indistinguishable in function from a factory-deployed one but is not recorded as such, undermining the on-chain provenance guarantee the registry is meant to provide. No funds are directly stolen in the base case, keeping severity bounded to medium.

### Likelihood Explanation
The attack requires no privilege and no assets beyond gas: `nonces(deployer)` is a public view, `predictSmartAccountAddress` documents the exact derivation, and `SafeProxyFactory.createProxyWithNonce` is a standard permissionless external call. Any mempool-observing bot (or someone simply targeting a known, high-value deployer address) can execute this repeatedly and cheaply.

### Recommendation
Do not rely on a deployer-guessable, front-runnable salt for a permissionless external factory call. Options:
- Bind the salt to something the attacker cannot replicate/front-run meaningfully (e.g., include `block.chainid` plus a commit-reveal, or require the caller to pre-commit a random salt via `msg.sender`-scoped mapping updated *before* the external call succeeds, and only increment/consume the nonce slot atomically with a guard that treats a collision as "already deployed" rather than reverting the whole flow).
- Mirror the recommended Uniswap fix pattern: before calling `createProxyWithNonce`, check `SAFE_PROXY_FACTORY`/predicted address for existing code; if it already exists, treat it as already deployed (verify it matches expectations) instead of reverting, or advance to a fresh nonce automatically rather than requiring the caller to resubmit with the identical (already-collided) value.
- At minimum, allow the deployer to retry with a *different* nonce rather than deterministically recomputing the same colliding salt after every revert.

### Proof of Concept
1. Attacker reads `SmartAccountFactory.nonces(victim)` (call it `n`) and observes `victim`'s pending `deploySmartAccount(delegates, currencies, nftCollections, trustedRecipients, validUntil, owners, threshold)` transaction in the mempool.
2. Attacker computes `saltNonce = uint256(keccak256(abi.encodePacked(victim, n)))` and reconstructs the identical `initializer` bytes (same `_buildInitializer` logic, all parameters are in the pending calldata).
3. Attacker calls `SafeProxyFactory.createProxyWithNonce(SAFE_SINGLETON, initializer, saltNonce)` directly with higher gas price to land first.
4. Victim's `deploySmartAccount` executes; `createProxyWithNonce` targets the same CREATE2 address, already has code, and the call reverts — `nonces[victim]` is not incremented (whole tx reverted).
5. Victim retries `deploySmartAccount`; `deployerNonce` is still `n`, producing the same `saltNonce`/address, which is still occupied — permanent revert until the deployer changes one of the initializer-affecting parameters (delegates/currencies/etc.), which changes intended configuration just to work around the DoS.

### Citations

**File:** tare-io__tare-contracts/contracts/SmartAccountFactory.sol (L67-81)
```text
    // Calculate the salt nonce for deterministic address (scoped per deployer)
    uint256 deployerNonce = nonces[msg.sender]++;
    uint256 saltNonce = uint256(keccak256(abi.encodePacked(msg.sender, deployerNonce)));

    bytes memory initializer = _buildInitializer(
      delegates,
      currencies,
      nftCollections,
      trustedRecipients,
      validUntil,
      owners,
      threshold
    );

    SafeProxy proxy = SAFE_PROXY_FACTORY.createProxyWithNonce(SAFE_SINGLETON, initializer, saltNonce);
```

**File:** tare-io__tare-contracts/contracts/SmartAccountFactory.sol (L139-173)
```text
  function predictSmartAccountAddress(
    address deployer,
    uint256 _nonce,
    address[] memory delegates,
    address[] memory currencies,
    address[] memory nftCollections,
    address[] memory trustedRecipients,
    uint48 validUntil,
    address[] memory owners,
    uint256 threshold
  ) public view returns (address) {
    uint256 saltNonce = uint256(keccak256(abi.encodePacked(deployer, _nonce)));

    bytes memory initializer = _buildInitializer(
      delegates,
      currencies,
      nftCollections,
      trustedRecipients,
      validUntil,
      owners,
      threshold
    );
    bytes32 salt = keccak256(abi.encodePacked(keccak256(initializer), saltNonce));

    bytes memory deploymentData = abi.encodePacked(
      SAFE_PROXY_FACTORY.proxyCreationCode(),
      uint256(uint160(SAFE_SINGLETON))
    );

    bytes32 hash = keccak256(
      abi.encodePacked(bytes1(0xff), address(SAFE_PROXY_FACTORY), salt, keccak256(deploymentData))
    );

    return address(uint160(uint256(hash)));
  }
```

**File:** tare-io__tare-contracts/specs/smart-accounts.md (L166-178)
```markdown
### isDeployedSmartAccount

```solidity
function isDeployedSmartAccount(address account) external view returns (bool deployed)
```

**Purpose**: On-chain provenance check — lets integrators and users verify that a Safe account was deployed by this factory (i.e. belongs to the TARE platform)

**Behavior**:

- Returns `true` only for accounts deployed via `deploySmartAccount` on this factory instance
- Returns `false` for any other address, including predicted-but-not-yet-deployed addresses
- The registry is append-only: entries are never removed
```
