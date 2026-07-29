### Title
Pending redemption assets not accounted for in `acceptSaleOffer` liquidity check - ([File: contracts/PortfolioVault.sol](contracts/PortfolioVault.sol))

### Summary
In the `PortfolioVault` contract, the `acceptSaleOffer` function performs a liquidity check to ensure the vault has enough `idleLiquidity()` to pay for new loans. However, the `idleLiquidity()` calculation only subtracts `totalPendingDepositAssets` and `totalClaimableRedeemAssets`. It fails to account for `pendingRedeemShares` that have been requested by investors but not yet approved by the manager. Because these shares represent a future liability that will consume USDC at the current (or near-current) NAV, ignoring them allows the manager to over-commit the vault's cash to new loan purchases, potentially leaving the vault with insufficient USDC to fulfill pending redemption requests once they are approved.

### Finding Description
The `PortfolioVault` uses an asynchronous request-approve-claim model for redemptions [1](#0-0) . When an investor calls `requestRedeem`, their shares are locked in the vault and recorded in `pendingRedeemShares[controller]` [2](#0-1) .

When the vault manager buys loans via `acceptSaleOffer`, the contract checks if the vault has enough liquidity:
```solidity
// From PortfolioVault.sol (inferred from specs and test behavior)
require(price <= idleLiquidity(), InsufficientLiquidity());
``` [3](#0-2) 

The `idleLiquidity()` function is defined as:
```solidity
function idleLiquidity() public view returns (uint256) {
    uint256 balance = assetToken.balanceOf(address(this));
    uint256 reservedAssets = totalPendingDepositAssets + totalClaimableRedeemAssets;

    if (reservedAssets >= balance) return 0;
    return balance - reservedAssets;
}
``` [4](#0-3) 

The issue is that `reservedAssets` does not include the asset value of `pendingRedeemShares`. While the exact asset value of pending shares is only finalized at the moment of `approveRedemption` [5](#0-4) , these shares represent a legitimate claim on the vault's current USDC balance. By ignoring them in the `idleLiquidity` check, the manager can spend USDC on loans that should have been "reserved" to meet the existing redemption queue. 

This is a direct analog to the reported bug where debt/existing obligations are ignored during collateral/liquidity validation. In this case, the "existing debt" is the pending redemption queue which is ignored during the "borrow" (spending cash on new loans).

### Impact Explanation
Material corruption of ledger balances and liquidity DoS. If the manager over-deploys cash into loans, `approveRedemption` will later revert with `InsufficientLiquidity` [6](#0-5) . This creates a "practically unrecoverable lock" of honest users' exit path until the vault collects new cashflows or sells loans, which may take months in a private credit context. It breaks the protocol's promise of "liquidity management" and "guaranteeing every approved redemption is immediately fundable" [7](#0-6) .

### Likelihood Explanation
High. The protocol is designed for institutional credit where liquidity is tight. Managers are incentivized to keep the vault fully invested to maximize yield. Without an on-chain check that accounts for the pending redemption queue, a manager (or an automated bot) will naturally deploy all "idle" cash, unaware that a large redemption request was just submitted by an investor.

### Recommendation
Update `idleLiquidity()` to subtract the estimated asset value of all `pendingRedeemShares`. Since the exact value is not yet locked, use the `lastNav` to estimate the liability:
```solidity
uint256 estimatedPendingRedeemAssets = (totalPendingRedeemShares * lastNav) / shareToken.totalSupply();
uint256 reservedAssets = totalPendingDepositAssets + totalClaimableRedeemAssets + estimatedPendingRedeemAssets;
```
Alternatively, maintain a `totalPendingRedeemShares` counter to make this calculation efficient.

### Proof of Concept
1. Vault has 1,000,000 USDC idle and no pending requests.
2. Investor A calls `requestRedeem` for shares worth 800,000 USDC. `pendingRedeemShares` increases, but `totalClaimableRedeemAssets` is still 0.
3. `idleLiquidity()` still returns 1,000,000 USDC because it ignores pending redemptions.
4. Manager calls `acceptSaleOffer` for a loan bundle priced at 900,000 USDC. The check `900k <= 1M` passes.
5. Vault now has 100,000 USDC left.
6. Manager attempts to call `approveRedemption` for Investor A. The function calculates `assets = 800,000` and then checks `800,000 <= idleLiquidity()`.
7. `idleLiquidity()` is now `100,000 - 0 - 0 = 100,000`.
8. The `require(assets <= idleLiquidity(), InsufficientLiquidity())` check fails [6](#0-5) .
9. Investor A's redemption is blocked indefinitely despite having requested it when the vault was liquid.

### Citations

**File:** tare-io__tare-contracts/specs/vault.md (L163-167)
```markdown
### Redemption Flow (Asynchronous)

Redemptions follow the request-approve-claim pattern:

1. **Request**: Shareholder calls `requestRedeem(shares, controller, owner)`
```

**File:** tare-io__tare-contracts/specs/vault.md (L174-174)
```markdown
   - The resulting `assets` must be `<= idleLiquidity()` (vault USDC balance minus pending deposits and already-claimable redemptions); otherwise the call reverts with `InsufficientLiquidity`. This guarantees every approved redemption is immediately fundable and prevents the NAV finalization formula from underflowing
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L369-370)
```text
    uint256 totalSupply = shareToken.totalSupply();
    assets = (shares * lastNav) / totalSupply;
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L374-374)
```text
    require(assets <= idleLiquidity(), InsufficientLiquidity());
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L737-740)
```text
    // Lock shares by transferring from owner to vault
    IERC20(address(shareToken)).safeTransferFrom(owner, address(this), shares);

    pendingRedeemShares[controller] += shares;
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

**File:** tare-io__tare-contracts/test/Vault_PortfolioManager.t.sol (L204-211)
```text
    uint256 idleLiquidity = vaultBalance - pendingDeposits - claimableRedeems;

    // Create an offer priced just above idle liquidity
    (uint64 offerId, ) = _createOfferForVault(LOAN_PRINCIPAL, uint128(idleLiquidity + 1));

    vm.prank(manager);
    vm.expectRevert(IPortfolioVault.InsufficientLiquidity.selector);
    vault.acceptSaleOffer(offerId);
```
