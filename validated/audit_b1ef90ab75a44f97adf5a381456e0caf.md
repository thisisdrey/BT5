### Title
Unsafe Memory Array Truncation via Inline Assembly - (`tare-io__tare-contracts/contracts/PortfolioVault.sol`)

### Summary
The `PortfolioVault` contract performs manual memory manipulation using inline assembly to truncate the `owned` array during the NAV calculation process. Specifically, it uses `mstore` to overwrite the length of a memory array without updating the free memory pointer or performing bounds checks. This mirrors the unsafe memory handling bug-class identified in the external report.

### Finding Description
In `PortfolioVault.updateNav`, the contract allocates a memory array `owned` with a size of `batchSize`. As it iterates through the vault's loan holdings, it populates this array with loans that are still owned by the vault. After the loop, it uses inline assembly to update the length of the array to match the actual number of owned loans found (`ownedCount`). [1](#0-0) 

While this is intended to "trim" the array before passing it to the `calculator`, it is a dangerous pattern. In Solidity, memory arrays are stored as a 32-byte length field followed by the data elements. Manually modifying the length field using `mstore` does not notify the Solidity memory manager. If the `calculator.getLoansValue` function or subsequent logic in `updateNav` performs operations that assume the memory following the truncated array is available (based on the updated length), it can lead to memory corruption or data overlap if the free memory pointer (`0x40`) is not also updated or if the original allocation is relied upon elsewhere.

### Impact Explanation
If the memory layout is corrupted, the NAV calculation could return incorrect values. This directly impacts the `lastNav` field, which is used to price vault shares during `approveDeposit` and `approveRedemption`. [2](#0-1) 

Material corruption of the NAV produces real value loss through unfair minting or redeeming of vault shares, which is a prohibited impact under the Tare Allowed Impact Gate.

### Likelihood Explanation
The likelihood is low because the truncation only reduces the length (`ownedCount <= batchSize`), meaning it doesn't overflow into adjacent memory. However, the use of inline assembly for memory management is fragile; any change to the `NavCalculator` implementation or the surrounding memory-intensive logic in `updateNav` could trigger unexpected behavior or corruption due to the inconsistent state between the array's reported length and the actual memory allocation.

### Recommendation
Avoid using inline assembly for array truncation. Instead, copy the relevant elements to a new, correctly-sized memory array, or modify the `INavCalculator` interface to accept a length parameter. If assembly must be used, ensure the free memory pointer is correctly handled and that strict bounds checks are enforced.

### Proof of Concept
1. A `PortfolioVault` has 100 loans in its `_navLoanIds` list.
2. A manager calls `updateNav(100)`.
3. During the loop, 50 loans are found to be no longer owned (e.g., they were transferred out).
4. `ownedCount` becomes 50, but the `owned` array was allocated for 100.
5. `assembly { mstore(owned, 50) }` is executed.
6. The `calculator.getLoansValue(loans, owned)` is called. If the calculator uses assembly to iterate based on the length or performs its own memory allocations, the mismatch between the original allocation and the new length can lead to pointer errors or reading from uninitialized memory slots that were part of the original 100-slot allocation. [3](#0-2)

### Citations

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L280-309)
```text
    for (uint256 i; i < batchSize; ++i) {
      if (cursor >= _navLoanIds.length) break;
      uint64 loanId = _navLoanIds[cursor];
      // Treat a reverting `ownerOf` (e.g. burned token) the same as a foreign
      // owner so the list self-heals instead of bricking NAV computation.
      bool owns;
      try loansNFT_.ownerOf(uint256(loanId)) returns (address owner) {
        owns = owner == address(this);
      } catch {
        owns = false;
      }
      if (owns) {
        owned[ownedCount++] = loanId;
        unchecked {
          ++cursor;
        }
      } else {
        // Drop stale entry; swap-and-pop places a new entry at `cursor`, so do
        // not advance — the next iteration re-scans this slot.
        _removeLoanFromNav(loanId);
      }
    }

    if (ownedCount > 0) {
      // Trim the memory array to its used length before passing to the calculator.
      assembly {
        mstore(owned, ownedCount)
      }
      pendingNav += calculator_.getLoansValue(loans_, owned);
    }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L315-320)
```text
      lastNav =
        assetToken.balanceOf(address(this)) +
        calculator_.applyPortfolioAdjustment(pendingNav) -
        totalPendingDepositAssets -
        totalClaimableRedeemAssets;
      lastNavUpdate = block.timestamp;
```
