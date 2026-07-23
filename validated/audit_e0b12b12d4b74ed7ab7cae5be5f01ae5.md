### Title
Permissionless `register()` Clears Admin Blacklist, Restoring Oracle Price Access to Abusive Pools — (`OracleBase.sol`)

---

### Summary

`OracleBase.register()` is permissionless and unconditionally clears the admin-set pool blacklist as a side effect of registration. Because the default `registrationFee` is 1 wei, any blacklisted pool operator can trivially re-register and regain the ability to call `price(feedId, pool)`, bypassing the admin's abuse-protection mechanism entirely.

---

### Finding Description

The admin controls pool access to oracle price data through two mechanisms:

1. **Blacklist** — `setBlacklist(pool, true)` (ADMIN\_ROLE only) prevents a pool from calling `price()`.
2. **Registration** — `register(feedId, pool, factory)` (permissionless, payable) whitelists a pool for a specific feed.

The `price()` gate enforces both:

```solidity
// OracleBase.sol L160-172
function price(bytes32 feedId, address pool)
    external feedExists(feedId) notBlacklisted
    returns (...)
{
    require(!blacklisted[pool], Blacklisted(pool));
    require(registeredPool[feedId][pool], NotRegistered(feedId, pool));
    ...
}
```

However, `register()` silently clears the blacklist as a side effect:

```solidity
// OracleBase.sol L201-214
function register(bytes32 feedId, address pool, address factory) external payable {
    require(msg.value >= registrationFee, ...);   // default: 1 wei
    require(approvedFactories.contains(factory), ...);
    require(IPoolFactory(factory).isPool(pool), ...);

    if (blacklisted[pool]) {
        blacklisted[pool] = false;          // ← admin blacklist erased
        emit BlacklistUpdated(pool, false);
    }

    registeredPool[feedId][pool] = true;
    ...
}
```

The only prerequisites are that the pool was created by an approved factory (it was — it is a legitimate pool that was later blacklisted for abuse) and that the caller pays `registrationFee` (1 wei by default). There is no admin approval, no timelock, and no check that the caller is the pool owner. **Any address** can call `register()` on behalf of a blacklisted pool.

This is the direct analog of the `is_permissioned()` bug: just as an expired `Blacklisted` entry incorrectly returned `true` (whitelisted-level access) instead of `!strict` (default access), here an expired/bypassed blacklist entry is silently cleared by a permissionless payment, restoring oracle access the admin explicitly revoked.

---

### Impact Explanation

The oracle blacklist is the primary runtime lever the admin has to stop an abusive pool mid-operation (e.g., a pool being used for sandwich attacks, price manipulation, or unauthorized high-frequency reads). Once a pool is blacklisted, every call to `price(feedId, pool)` reverts with `Blacklisted`. But because `register()` clears the flag for 1 wei, the blacklist provides zero durable protection:

- The pool operator (or any third party) calls `register(feedId, pool, approvedFactory)` with 1 wei.
- `blacklisted[pool]` is set to `false`; `registeredPool[feedId][pool]` is set to `true`.
- The pool's price provider can again call `oracle.price(feedId, pool)` during a swap.
- The pool resumes executing swaps with live oracle bid/ask prices, continuing whatever abusive behavior triggered the blacklist.
- LPs and traders suffer ongoing losses from the resumed abuse.

**Severity: Medium** — the admin's oracle-access control boundary is bypassed by an unprivileged, near-zero-cost path, enabling continued fund extraction from pool participants.

---

### Likelihood Explanation

- `register()` has no caller restriction — any EOA or contract can invoke it.
- The default `registrationFee` is 1 wei (set in the constructor); even after an admin raises it, the pool operator is economically motivated to pay any fee that is less than the profit from resumed abuse.
- The pool is already a valid pool recognized by an approved factory, so all structural checks pass.
- The admin has no atomic "blacklist + deregister" primitive; even if they also call `registeredPool[feedId][pool] = false` (which requires a separate admin function if one exists), the operator can re-register again immediately.

Likelihood: **High** — the bypass is trivial, cheap, and repeatable.

---

### Recommendation

Remove the blacklist-clearing side effect from `register()`. The blacklist should be exclusively controlled by the admin via `setBlacklist()`:

```diff
 function register(bytes32 feedId, address pool, address factory) external payable {
     require(msg.value >= registrationFee, InsufficientFee(msg.value, registrationFee));
     require(pool != address(0));
     require(approvedFactories.contains(factory), FactoryNotApproved(factory));
     require(IPoolFactory(factory).isPool(pool), NotAPool(pool));
+    require(!blacklisted[pool], Blacklisted(pool));   // refuse registration while blacklisted

-    if (blacklisted[pool]) {
-        blacklisted[pool] = false;
-        emit BlacklistUpdated(pool, false);
-    }

     registeredPool[feedId][pool] = true;
     emit PoolRegistered(feedId, pool, msg.sender, msg.value);
 }
```

If the intended design is that paying the fee rehabilitates a pool, that decision must be gated on admin approval (e.g., require a separate admin call to lift the blacklist before registration is accepted), not silently embedded in a permissionless payable function.

---

### Proof of Concept

```
State before:
  blacklisted[pool]              = true   (admin set via setBlacklist)
  registeredPool[feedId][pool]   = false  (or true — irrelevant)

Step 1: pool operator (or anyone) calls
  oracle.register{value: 1 wei}(feedId, pool, approvedFactory)

  Checks pass:
    msg.value (1 wei) >= registrationFee (1 wei)  ✓
    approvedFactories.contains(approvedFactory)    ✓
    IPoolFactory(approvedFactory).isPool(pool)     ✓  (pool is legitimate)

  Side effect:
    blacklisted[pool] = false                      ← blacklist erased

State after:
  blacklisted[pool]              = false
  registeredPool[feedId][pool]   = true

Step 2: pool executes a swap → _getBidAndAskPriceX64() → provider.getBidAndAskPrice()
        → oracle.price(feedId, pool)

  notBlacklisted modifier: blacklisted[msg.sender (provider)] = false  ✓
  require(!blacklisted[pool])                                           ✓
  require(registeredPool[feedId][pool])                                 ✓

  Oracle returns live bid/ask → swap executes → abusive behavior resumes.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** smart-contracts-poc/contracts/oracles/providers/OracleBase.sol (L49-53)
```text
    constructor(address _owner, uint256 maxTimeDrift) {
        _grantRole(ADMIN_ROLE, _owner);
        _setRoleAdmin(ADMIN_ROLE, ADMIN_ROLE);
        MAX_TIME_DRIFT = maxTimeDrift;
        registrationFee = 1 wei; // very cheap default; ADMIN tunes via setRegistrationFee
```

**File:** smart-contracts-poc/contracts/oracles/providers/OracleBase.sol (L160-172)
```text
    function price(bytes32 feedId, address pool)
        external
        feedExists(feedId)
        notBlacklisted
        returns (uint256 mid, uint256 spread, uint16 spread1, uint256 refTime)
    {
        require(pool != address(0) && IPool(pool).inSwap() == msg.sender, InvalidInSwap());
        require(!blacklisted[pool], Blacklisted(pool));
        require(registeredPool[feedId][pool], NotRegistered(feedId, pool));

        (mid, spread, spread1, refTime) = _readPrice(feedId);
        emit PriceRead(pool, feedId);
    }
```

**File:** smart-contracts-poc/contracts/oracles/providers/OracleBase.sol (L196-214)
```text
    /// @notice Permissionless paid registration: whitelist `pool` for `feedId` (required to use the
    ///         on-chain price(feedId, factory) path). `factory` must be approved and recognize `pool`
    ///         via isPool. Paying also clears any blacklist on the pool.
    /// @dev    Overpayment is NOT refunded: any msg.value above registrationFee is kept and is
    ///         withdrawable by ADMIN via withdrawEth. This is intentional.
    function register(bytes32 feedId, address pool, address factory) external payable {
        require(msg.value >= registrationFee, InsufficientFee(msg.value, registrationFee));
        require(pool != address(0));
        require(approvedFactories.contains(factory), FactoryNotApproved(factory));
        require(IPoolFactory(factory).isPool(pool), NotAPool(pool));

        if (blacklisted[pool]) {
            blacklisted[pool] = false;
            emit BlacklistUpdated(pool, false);
        }

        registeredPool[feedId][pool] = true;
        emit PoolRegistered(feedId, pool, msg.sender, msg.value);
    }
```

**File:** smart-contracts-poc/contracts/oracles/providers/OracleBase.sol (L271-276)
```text
    function setBlacklist(address account, bool value) external onlyRole(ADMIN_ROLE) {
        require(account != address(0));
        if (blacklisted[account] == value) return;
        blacklisted[account] = value;
        emit BlacklistUpdated(account, value);
    }
```

**File:** metric-core/contracts/MetricOmmPool.sol (L804-813)
```text
  function _getBidAndAskPriceX64() internal returns (uint128 bidPriceX64, uint128 askPriceX64) {
    address activePriceProvider = _resolvedPriceProvider();
    try IPriceProvider(activePriceProvider).getBidAndAskPrice() returns (uint128 bid, uint128 ask) {
      if (bid >= ask) revert BidGreaterThanAsk();
      if (bid == 0) revert BidIsZero();
      return (bid, ask);
    } catch (bytes memory reason) {
      revert PriceProviderFailed(reason);
    }
  }
```
