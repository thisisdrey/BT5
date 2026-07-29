### Title
`updateNav()` permanently reverts and bricks the vault when portfolio losses make NAV go negative (insolvency) - ([File: tare-io__tare-contracts/contracts/PortfolioVault.sol])

### Summary
The external report describes `_maxRedemptionRequest()` reverting once a Set becomes insolvent because the computation cannot handle a negative net value, permanently breaking the redemption-limit view and any flow gated on it. `PortfolioVault.sol`'s `updateNav()` has the same root-cause pattern: its NAV finalization step performs unchecked-by-design subtraction of reserved investor liabilities from computed portfolio value, and once realized loan losses push the vault into an analogous "insolvent" state, the subtraction underflows and reverts under Solidity 0.8 checked arithmetic — permanently, on every subsequent call — bricking NAV updates and every function gated on an idle/fresh NAV cycle.

### Finding Description
NAV finalization in `updateNav()`: [1](#0-0) 

```
lastNav =
  assetToken.balanceOf(address(this)) +
  calculator_.applyPortfolioAdjustment(pendingNav) -
  totalPendingDepositAssets -
  totalClaimableRedeemAssets;
```

`totalPendingDepositAssets` and `totalClaimableRedeemAssets` represent USDC that the vault has already committed to investors (pending deposit refunds and approved-but-unclaimed redemptions) and are supposed to always be fully backed by on-chain balance plus loan valuation. If the loan portfolio suffers real losses (defaults, write-downs valued by `calculator.getLoansValue`/`applyPortfolioAdjustment`), the sum `assetToken.balanceOf(this) + calculator_.applyPortfolioAdjustment(pendingNav)` can fall below `totalPendingDepositAssets + totalClaimableRedeemAssets`. At that point the subtraction underflows and the entire `updateNav()` call reverts with Solidity's built-in arithmetic-underflow panic — there is no floor/clamp to zero, unlike `idleLiquidity()`, which explicitly guards the analogous subtraction: [2](#0-1) 

Critically, `navStart` is only reset to `0` inside the same finalization block that just reverted: [3](#0-2) 

Because the underflow is deterministic given the same on-chain state, every retry of `updateNav()` hits the identical revert once the loop reaches the final batch, so `navStart` can never be reset back to `0`. Since `_requireIdleNav()` requires `navStart == 0`: [4](#0-3) 

every function that depends on it becomes permanently unusable: `fundLoan`/`fundLoans`, `collectCashflows`, `addLoansToNav`/`removeLoansFromNav`, `setCalculator`, `setLoans`, `acceptSaleOffer`/`createSaleOffer` (via `_requireIdleNav` chain), and `transferLoans`. In addition, `_requireFreshNav()` (used by `approveDeposit`/`approveRedemption`) will eventually fail via `StaleNav()` since `lastNavUpdate` is never refreshed: [5](#0-4) 

This mirrors the external finding exactly: a solvency-sensitive computation that should degrade gracefully (e.g., clamp NAV to 0, or otherwise signal insolvency) instead reverts unconditionally, and that revert becomes permanent because there is no recovery/reset path independent of the very computation that is broken.

### Impact Explanation
Once triggered, this is a protocol-wide, unrecoverable Denial of Service: no new deposit/redemption approvals can ever be produced, no further loan funding or cashflow collection can occur, and portfolio managers cannot update loan curation, calculator, or loans wiring, since all of these are gated by `navStart == 0` or a fresh NAV. This matches the allowed "Material production DoS or state corruption that blocks funding, disbursement, withdrawals, claims, NAV-sensitive approvals... across users." Investors with requests still pending or already-approved can partially self-rescue via `cancelDepositRequest`/`cancelRedeemRequest` (which don't depend on NAV freshness), but the vault itself is permanently frozen from any further productive operation — a realistic, severe impact directly caused by ordinary loan losses, not by any privileged-role misbehavior.

### Likelihood Explanation
The trigger condition — loan losses/defaults reducing the calculator-reported portfolio value below the amount already reserved for pending/claimable investor requests — is a normal, expected market event in a credit protocol, not a contrived edge case requiring an attacker. Any sufficiently large default combined with outstanding pending deposits/claimable redemptions makes this reachable without any privileged action; likelihood is realistically high over the life of the vault given the protocol's stated purpose of holding credit-risk assets.

### Recommendation
Clamp the NAV subtraction to zero the same way `idleLiquidity()` already does (`reservedAssets >= balance` check), and treat a computed negative NAV as an explicit "vault insolvent" state that other functions can query and react to gracefully, rather than allowing the underflow revert to permanently corrupt `navStart`. At minimum, ensure `navStart` can be reset to `0` (e.g., via a guardian-only recovery path) independently of successful NAV finalization, so an insolvent NAV cycle cannot permanently deadlock `_requireIdleNav()`-gated functions.

### Proof of Concept
1. Vault has `totalPendingDepositAssets + totalClaimableRedeemAssets = X` USDC reserved (e.g., from `requestDeposit` and a prior `approveRedemption`).
2. Loans in `_navLoanIds` suffer defaults such that `calculator.getLoansValue`/`applyPortfolioAdjustment` returns a materially lower value, and `assetToken.balanceOf(vault) + adjustedPendingNav < X`.
3. Portfolio/Investor manager calls `updateNav(batchSize)` sweeping the full loan list; on the final batch the finalization line reverts with an arithmetic underflow.
4. `navStart` remains non-zero (never reached the `navStart = 0` line).
5. Any subsequent call to `updateNav()`, `fundLoan(s)`, `collectCashflows`, `addLoansToNav`, `removeLoansFromNav`, `setCalculator`, `setLoans`, `acceptSaleOffer`, `createSaleOffer`, `cancelSaleOffer`, or `transferLoans` reverts via `_requireIdleNav()`, permanently. `approveDeposit`/`approveRedemption` eventually revert via `_requireFreshNav()`'s `StaleNav()` check once `maxNavAge` elapses, since NAV can never be refreshed again.

### Citations

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L311-326)
```text
    navCursor = cursor;

    // Finalize if we've processed all loans
    if (cursor >= _navLoanIds.length) {
      lastNav =
        assetToken.balanceOf(address(this)) +
        calculator_.applyPortfolioAdjustment(pendingNav) -
        totalPendingDepositAssets -
        totalClaimableRedeemAssets;
      lastNavUpdate = block.timestamp;
      navCursor = 0;
      pendingNav = 0;
      navStart = 0;
      emit NavUpdated(lastNav, block.timestamp);
    }
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L940-946)
```text
  function idleLiquidity() public view returns (uint256) {
    uint256 balance = assetToken.balanceOf(address(this));
    uint256 reservedAssets = totalPendingDepositAssets + totalClaimableRedeemAssets;

    if (reservedAssets >= balance) return 0;
    return balance - reservedAssets;
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L1090-1098)
```text
  function _requireFreshNav() internal view {
    require(navStart == 0, NavComputationInProgress());
    require(lastNav > 0, ZeroNav());
    // Specific staleness signals come before the generic age check so callers
    // see the most informative error (e.g. `PortfolioHoldingsChanged` when an
    // NFT moved, even if `lastNavUpdate` was also explicitly cleared).
    require(loansNFT.ownershipNonce(address(this)) == lastOwnershipNonce, PortfolioHoldingsChanged());
    require(calculator.configurationVersion() == lastCalculatorConfigurationVersion, CalculatorConfigurationChanged());
    require(block.timestamp - lastNavUpdate <= maxNavAge, StaleNav());
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L1101-1104)
```text
  /// @notice Reverts if a NAV computation is currently in progress.
  function _requireIdleNav() internal view {
    require(navStart == 0, NavComputationInProgress());
  }
```
