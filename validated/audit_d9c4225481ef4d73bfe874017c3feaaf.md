Found it. `TrustedCalls.executeTrustedCall` computes a trust key that binds only `(target, selector)` — it never binds the call to the specific `data` payload's arguments, and critically the executed `data` is passed through unmodified to `execTransactionFromModuleReturnData`. This is the direct structural analog of the audit report: the "context" that should be bound to the authorized action (the specific Safe/account the call operates on, embedded inside `data`'s ABI-encoded arguments) is never validated to match `safe`, the account actually executing the call.

### Title
Trusted-call whitelist binds only `(target, selector)`, not call arguments — a delegate can redirect any address-parameterized trusted function to act on/for another Safe - ([File: tare-io__tare-contracts/contracts/TrustedCalls.sol])

### Summary
`TrustedCalls.executeTrustedCall`/`executeTrustedCallBatch` authorize execution based solely on `keccak256(abi.encodePacked(target, selector))` [1](#0-0) . The full `data` calldata — including any embedded address/account/loanId arguments — is forwarded verbatim to `IModuleManager(payable(safe)).execTransactionFromModuleReturnData(target, 0, data, Enum.Operation.Call)` [2](#0-1) . Like the Uniswap wallet bug — where a ciphertext's *content* was never cryptographically bound to the storage key's public address — here the *content* of an approved trusted call (its arguments) is never bound to the `safe` context it's approved to act as. Whether this is exploitable depends entirely on whether any whitelisted `Loans`/`Vault` function accepts an address/account argument that determines *whose* funds or state are affected, independent of `execTransactionFromModuleReturnData`'s implicit `msg.sender == safe`.

### Finding Description
`addTrustedCall`/`addTrustedCalls` are global and Safe-agnostic: once a guardian whitelists `(target, selector)`, *every* delegate of *every* onboarded Safe can invoke it [3](#0-2) . The only per-call binding to a specific Safe is that the module executes `target.call(data)` *as* `safe` via the Safe's own `execTransactionFromModuleReturnData` — so `msg.sender` at the target contract is correctly `safe`. This means functions that rely purely on `msg.sender` for authorization (e.g., `Loans.accrue`, `pay`, standard role-gated actions) are safely scoped, since the underlying target contract itself enforces the `msg.sender`-based binding.

However, this protection breaks down for any whitelisted function whose *effect* is determined by an explicit address/account parameter rather than (or in addition to) `msg.sender` — e.g., a hypothetical trusted call like `Loans.updateBorrower(loanId, newBorrower)` or `TrustedSpender.setAllowance(token, from, to, ...)` where `from` is caller-supplied rather than implicitly `msg.sender`. In `TrustedCalls`, nothing checks that any address argument embedded in `data` equals `safe`. A delegate of Safe A, invoking a trusted function whose calldata takes an arbitrary `safe`/`account`/`from` parameter, could pass Safe B's address as that argument — the trust registry only ever validates `(target, selector)`, never "does this call's embedded account argument match the executing Safe." This is architecturally identical to the audited bug: the authorization envelope (Chrome storage key / trusted-call registry entry) checks a coarse identifier, while the payload (encrypted private key / calldata arguments) that determines *who is actually affected* is never cryptographically or logically tied to that identifier.

I was unable to fully verify whether any currently whitelisted trusted-call target in the deployed configuration actually takes an independent address parameter that isn't re-validated against `msg.sender` internally by the target contract — the whitelist itself is populated by the guardian at runtime (via `addTrustedCall`) and isn't fully enumerated in the reviewed files. `Loans.updateBorrower` (which is `msg.sender`-independent of the party it affects and already flagged elsewhere in the audit as `Servicer-rewritten borrower address pulls USDC from any approver`) and `TrustedSpender.setAllowance`/`addDelegate` (also flagged elsewhere as giving admin parity across every onboarded Safe) are exactly the kind of functions where this argument-vs-context binding gap would materialize if exposed through `TrustedCalls`.

### Impact Explanation
If a whitelisted trusted-call target accepts a caller-supplied account/address argument that the target contract does not independently re-validate against the executing `msg.sender` (`safe`), a delegate of one Safe could use the shared, Safe-agnostic trust registry to execute state changes that read as if authorized "on behalf of" a different Safe/account than the one it's actually a delegate for — enabling unauthorized state transitions or value movement affecting another Safe's role-bound assets. This matches the in-scope impact category: "Unauthorized state transition or permission bypass that lets an unprivileged actor act for another … Safe, controller, receiver …".

### Likelihood Explanation
Likelihood is contingent on the specific whitelist configuration, which is guardian-controlled (a privileged action, out of scope by itself). The structural gap — the trust key omits argument binding — is fully reachable by any registered delegate without further privilege, but requires that some in-scope trusted-call target function have an argument-vs-`msg.sender` decoupling. Since `addTrustedCall` composition is dynamic and not fully enumerable from the reviewed contracts/tests, I cannot confirm a concrete whitelisted function today that triggers this; this should be treated as a structural weakness in `TrustedCalls`'s authorization model rather than a confirmed exploit against a currently-live whitelist entry.

### Recommendation
Bind the trust registry key to more than `(target, selector)` — e.g., include an expected calldata-argument-position check (verify that any address argument matching known "safe/account/from" argument slots in whitelisted signatures equals `safe`), or require that all whitelisted functions be `msg.sender`-scoped only (reject any function whose semantics could be redirected via an explicit parameter). Alternatively, encode a per-function argument mask/validator at `addTrustedCall` time so the registry can assert the account parameter matches `safe` before forwarding.

### Proof of Concept
Not constructible against current code without a concrete whitelisted target function that both (a) is approved via `addTrustedCall`, and (b) takes an independent address argument not re-validated internally by the target against `msg.sender`. This is a structural/architectural finding based on tracing `TrustedCalls.executeTrustedCall`'s authorization logic [4](#0-3) , not a concretely instantiated exploit chain.

### Citations

**File:** tare-io__tare-contracts/contracts/TrustedCalls.sol (L51-59)
```text
  /// @inheritdoc ITrustedCalls
  function addTrustedCall(address target, bytes4 selector) external whenNotPaused onlyRole(GUARDIAN_ROLE) {
    require(selector != bytes4(0), InvalidSelector());

    bytes32 key = getTrustKey(target, selector);
    trustedCalls[key] = true;

    emit TrustedCallAdded(target, selector);
  }
```

**File:** tare-io__tare-contracts/contracts/TrustedCalls.sol (L100-126)
```text
  /// @inheritdoc ITrustedCalls
  function executeTrustedCall(
    address safe,
    address target,
    bytes calldata data
  ) external whenNotPaused returns (bool success, bytes memory returnData) {
    // Verify sender is delegate for this Safe
    require(delegates[safe][msg.sender], NotADelegate());

    // Extract function selector (first 4 bytes)
    require(data.length >= 4, InvalidSelector());
    bytes4 selector = bytes4(data[:4]);

    // Verify call is trusted
    bytes32 key = getTrustKey(target, selector);
    require(trustedCalls[key], CallNotTrusted());

    // Execute call via Safe
    (success, returnData) = IModuleManager(payable(safe)).execTransactionFromModuleReturnData(
      target,
      0, // value: no ETH sent
      data,
      Enum.Operation.Call
    );

    require(success, ExecutionFailed());
  }
```

**File:** tare-io__tare-contracts/contracts/TrustedCalls.sol (L171-174)
```text
  /// @inheritdoc ITrustedCalls
  function getTrustKey(address target, bytes4 selector) public pure returns (bytes32) {
    return keccak256(abi.encodePacked(target, selector));
  }
```
