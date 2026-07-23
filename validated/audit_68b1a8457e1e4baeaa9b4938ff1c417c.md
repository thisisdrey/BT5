### Title
Pool Admin Can Set Per-Bin Additional Fees Without Any Factory-Enforced Cap, Bypassing `maxAdminSpreadFeeE6` — (`File: metric-core/contracts/MetricOmmPoolFactory.sol`)

### Summary

The factory enforces `maxAdminSpreadFeeE6` / `maxAdminNotionalFeeE8` caps on the global admin spread and notional fees via `setPoolAdminFees`. However, the parallel path `setPoolBinAdditionalFees` forwards `addFeeBuyE6` / `addFeeSellE6` directly to the pool with **no factory-level cap check**, allowing the pool admin to set per-bin additional fees up to the `uint16` type maximum (65 535 E6 ≈ 6.55 %) on any bin, instantly and without a timelock, regardless of what the factory owner has configured for `maxAdminSpreadFeeE6`.

---

### Finding Description

`MetricOmmPoolFactory.setPoolAdminFees` enforces the factory owner's caps:

```solidity
// MetricOmmPoolFactory.sol – setPoolAdminFees
if (newAdminSpreadFeeE6 > maxAdminSpreadFeeE6) revert AdminFeeTooHigh();
if (newAdminNotionalFeeE8 > maxAdminNotionalFeeE8) revert AdminFeeTooHigh();
``` [1](#0-0) 

The sibling function `setPoolBinAdditionalFees` performs **no analogous cap check**:

```solidity
// MetricOmmPoolFactory.sol – setPoolBinAdditionalFees
function setPoolBinAdditionalFees(address pool, int8 bin, uint16 addFeeBuyE6, uint16 addFeeSellE6)
    external override nonReentrant onlyPoolAdmin(pool)
{
    IMetricOmmPoolFactoryActions(pool).setBinAdditionalFees(bin, addFeeBuyE6, addFeeSellE6);
}
``` [2](#0-1) 

The pool's `setBinAdditionalFees` also performs no bounds check on the fee values — only the bin index is validated:

```solidity
// MetricOmmPool.sol – setBinAdditionalFees
if (bin < LOWEST_BIN || bin > HIGHEST_BIN) revert InvalidBinIndex(bin);
BinState storage s = _binStates[bin];
s.addFeeBuyE6 = addFeeBuyE6;
s.addFeeSellE6 = addFeeSellE6;
emit BinAdditionalFeesUpdated(bin, addFeeBuyE6, addFeeSellE6);
``` [3](#0-2) 

The per-bin fees are stored in `BinState.addFeeBuyE6` / `addFeeSellE6` as `uint16` fields and are consumed directly by the swap math on every trade through that bin. [4](#0-3) 

---

### Impact Explanation

A pool admin can call `setPoolBinAdditionalFees(pool, bin, 65535, 65535)` at any time, setting per-bin additional fees to the `uint16` maximum — 65 535 E6 ≈ **6.55 %** — on any active bin. This fee is applied on top of the global spread fee for every swap routed through that bin. The factory owner's `maxAdminSpreadFeeE6` cap (which can be as low as 0) does not constrain this path. Users trading on the affected bin pay the inflated fee immediately, with no timelock to react.

The effective total fee for a bin becomes:

```
total_fee = spreadFeeE6 (global) + addFeeBuyE6 (per-bin, uncapped)
```

The factory owner can set `maxAdminSpreadFeeE6 = 0` to prevent any global admin spread fee, yet the pool admin retains the ability to charge up to 6.55 % per-bin, defeating the governance cap.

---

### Likelihood Explanation

- The pool admin role is semi-trusted but permissioned per-pool; a malicious or compromised pool admin can exploit this at any time.
- No timelock, no factory-enforced cap, and no cooldown guard exist on `setPoolBinAdditionalFees`.
- The call is a single transaction; the fee change takes effect on the very next swap through the targeted bin.
- An event (`BinAdditionalFeesUpdated`) is emitted, so monitoring is possible, but users have zero reaction time before their in-flight or next-block trades are executed at the elevated fee.

---

### Recommendation

Add a factory-enforced cap on per-bin additional fees in `setPoolBinAdditionalFees`, consistent with the cap applied to global admin fees:

```solidity
function setPoolBinAdditionalFees(address pool, int8 bin, uint16 addFeeBuyE6, uint16 addFeeSellE6)
    external override nonReentrant onlyPoolAdmin(pool)
{
    if (addFeeBuyE6  > maxAdminSpreadFeeE6) revert AdminFeeTooHigh();
    if (addFeeSellE6 > maxAdminSpreadFeeE6) revert AdminFeeTooHigh();
    IMetricOmmPoolFactoryActions(pool).setBinAdditionalFees(bin, addFeeBuyE6, addFeeSellE6);
}
```

Alternatively, introduce a dedicated `maxAdminBinFeeE6` cap that the factory owner can configure independently, and enforce it here.

---

### Proof of Concept

1. Factory owner sets `maxAdminSpreadFeeE6 = 0` via `setFeeCaps(0, 0, ...)` to prevent admin from charging any global spread fee.
2. Pool admin calls `setPoolAdminFees(pool, 0, 0)` — passes the cap check (0 ≤ 0).
3. Pool admin immediately calls `setPoolBinAdditionalFees(pool, 0, 65535, 65535)` — no cap check, succeeds.
4. The next user swap routed through bin 0 is charged 65 535 E6 ≈ 6.55 % additional fee on top of the (zero) global spread fee.
5. The factory owner's governance cap is fully bypassed; the user loses ~6.55 % of their swap input to the pool admin. [2](#0-1) [5](#0-4) [3](#0-2)

### Citations

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L284-315)
```text
  function setFeeCaps(
    uint24 newMaxProtocolSpreadFeeE6,
    uint24 newMaxAdminSpreadFeeE6,
    uint24 newMaxProtocolNotionalFeeE8,
    uint24 newMaxAdminNotionalFeeE8
  ) external override onlyOwner {
    if (
      newMaxProtocolSpreadFeeE6 > HARD_MAX_SPREAD_FEE_E6 || newMaxAdminSpreadFeeE6 > HARD_MAX_SPREAD_FEE_E6
        || newMaxProtocolNotionalFeeE8 > HARD_MAX_NOTIONAL_FEE_E8 || newMaxAdminNotionalFeeE8 > HARD_MAX_NOTIONAL_FEE_E8
    ) {
      revert FeeCapsExceedHardLimit();
    }
    maxProtocolSpreadFeeE6 = newMaxProtocolSpreadFeeE6;
    maxAdminSpreadFeeE6 = newMaxAdminSpreadFeeE6;
    maxProtocolNotionalFeeE8 = newMaxProtocolNotionalFeeE8;
    maxAdminNotionalFeeE8 = newMaxAdminNotionalFeeE8;

    if (spreadProtocolFeeE6 > newMaxProtocolSpreadFeeE6) {
      uint24 oldFeeE6 = spreadProtocolFeeE6;
      spreadProtocolFeeE6 = newMaxProtocolSpreadFeeE6;
      emit SpreadProtocolFeeDefaultUpdated(oldFeeE6, newMaxProtocolSpreadFeeE6);
    }
    if (protocolNotionalFeeE8 > newMaxProtocolNotionalFeeE8) {
      uint24 oldFeeE8 = protocolNotionalFeeE8;
      protocolNotionalFeeE8 = newMaxProtocolNotionalFeeE8;
      emit ProtocolNotionalFeeDefaultUpdated(oldFeeE8, newMaxProtocolNotionalFeeE8);
    }

    emit FeeCapsUpdated(
      newMaxProtocolSpreadFeeE6, newMaxAdminSpreadFeeE6, newMaxProtocolNotionalFeeE8, newMaxAdminNotionalFeeE8
    );
  }
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L414-415)
```text
    if (newAdminSpreadFeeE6 > maxAdminSpreadFeeE6) revert AdminFeeTooHigh();
    if (newAdminNotionalFeeE8 > maxAdminNotionalFeeE8) revert AdminFeeTooHigh();
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L450-457)
```text
  function setPoolBinAdditionalFees(address pool, int8 bin, uint16 addFeeBuyE6, uint16 addFeeSellE6)
    external
    override
    nonReentrant
    onlyPoolAdmin(pool)
  {
    IMetricOmmPoolFactoryActions(pool).setBinAdditionalFees(bin, addFeeBuyE6, addFeeSellE6);
  }
```

**File:** metric-core/contracts/MetricOmmPool.sol (L464-474)
```text
  function setBinAdditionalFees(int8 bin, uint16 addFeeBuyE6, uint16 addFeeSellE6)
    external
    onlyFactory
    nonReentrant(PoolActions.SET_BIN_ADDITIONAL_FEES)
  {
    if (bin < LOWEST_BIN || bin > HIGHEST_BIN) revert InvalidBinIndex(bin);
    BinState storage s = _binStates[bin];
    s.addFeeBuyE6 = addFeeBuyE6;
    s.addFeeSellE6 = addFeeSellE6;
    emit BinAdditionalFeesUpdated(bin, addFeeBuyE6, addFeeSellE6);
  }
```

**File:** metric-core/contracts/types/PoolStorage.sol (L19-25)
```text
struct BinState {
  uint104 token0BalanceScaled;
  uint104 token1BalanceScaled;
  uint16 lengthE6;
  uint16 addFeeBuyE6;
  uint16 addFeeSellE6;
}
```
