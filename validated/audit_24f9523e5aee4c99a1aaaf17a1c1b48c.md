## Analysis

The claim is plausible and matches an unguarded rounding path in `PortfolioVault.sol`.

Both `redeem()` and `withdraw()` compute one side of the (shares, assets) pair from the other using a ratio derived from the controller's own claimable pool, then hand off to a shared `_claimRedeem` settlement helper: [1](#0-0) 

Unlike `approveRedemption()`, which explicitly guards against a zero-value rounding result with `require(assets > 0, ZeroAmount())` [2](#0-1) , neither `redeem()` nor `withdraw()` enforces that the *derived* side of the conversion is non-zero — they only validate the caller-supplied side (`shares > 0` in `redeem`, `assets > 0` in `withdraw`).

Because share tokens use 18 decimals while the asset (USDC) uses 6 decimals, `claimableAssets_` is numerically many orders of magnitude smaller than `claimableShares_` for typical share prices. Calling `redeem(1, ...)` (i.e., redeeming a single wei of share) computes:

```
assets = (1 * claimableAssets_) / claimableShares_  →  rounds to 0
```

This dust call still passes `redeem`'s checks (`shares>0 && shares<=claimableShares_`) and is forwarded to `_claimRedeem(controller, receiver, 0, 1, claimableAssets_, claimableShares_)`. Repeating this drains `claimableRedeemShares[controller]` toward zero without any corresponding decrease in `claimableRedeemAssets[controller]` or `totalClaimableRedeemAssets`. Once `claimableRedeemShares[controller]` hits zero, both `redeem()` and `withdraw()` revert with `NoClaimableRedeem` (`require(claimableShares_ > 0 && claimableAssets_ > 0, ...)`) even though `claimableRedeemAssets[controller]` is still non-zero — permanently stranding that value.

I was not able to view the body of `_claimRedeem` directly within my remaining tool budget, so I cannot 100% confirm its exact internal decrement logic (e.g., whether it might independently floor/guard shares-vs-assets consistency). This is the main verification gap.

Given the target/entrypoint mapping, the reproducible rounding asymmetry between `approveRedemption` (guarded) and `redeem`/`withdraw` (unguarded), and that the stranded value sits in `totalClaimableRedeemAssets` — a shared vault-wide counter that permanently reduces `idleLiquidity()` for all future `fundLoan` and NAV operations — this affects shared protocol state, not just the caller's own position, satisfying the "material lock of USDC / async request accounting corruption" impact gate.

### Title
Dust-sized `redeem()` calls can desynchronize `claimableRedeemShares` vs `claimableRedeemAssets`, permanently stranding claimable USDC - (File: contracts/PortfolioVault.sol)

### Summary
`redeem()` and `withdraw()` derive one side of the (shares, assets) pair via integer division without checking the derived value for zero, unlike `approveRedemption()` which has an explicit `ZeroAmount()` guard. Repeated dust-shares `redeem()` calls can zero out `claimableRedeemShares[controller]` while leaving `claimableRedeemAssets[controller]` and the vault-wide `totalClaimableRedeemAssets` counter untouched, after which the controller can never claim the remaining assets (both entrypoints require both sides `> 0`).

### Finding Description [1](#0-0)  compute `assets`/`shares` via truncating division from the caller-controlled side, with no minimum-output check, and pass the result into `_claimRedeem`, which (per the two callers) decrements `claimableRedeemShares`/`claimableRedeemAssets`/`totalClaimableRedeemAssets` by whatever was computed — including zero.

### Impact Explanation
The stranded `claimableRedeemAssets`/`totalClaimableRedeemAssets` remains counted against `idleLiquidity()` forever (it was already subtracted from `lastNav` at approval time), permanently locking real USDC that neither the controller nor the vault can access through the normal `redeem`/`withdraw`/`cancelRedeemRequest` paths, matching the "permanent lock of claimable assets" and "async request accounting corruption" impact categories.

### Likelihood Explanation
Any whitelisted shareholder can trigger this unassisted by simply calling `redeem(1, ...)` repeatedly against their own approved redemption once `claimableShares_ > claimableAssets_` numerically (the common case given 18 vs 6 decimals), with no special timing or NAV-freshness requirement beyond `whenNotPaused`.

### Recommendation
Add `require(assets > 0, ZeroAmount())` in `redeem()` and `require(shares > 0, ZeroAmount())` in `withdraw()` after computing the derived side, mirroring the guard already present in `approveRedemption()`/`approveDeposit()`.

### Proof of Concept
1. Shareholder deposits and later calls `requestRedeem` then has `approveRedemption` executed by the manager, producing `claimableRedeemShares[controller] = S`, `claimableRedeemAssets[controller] = A` with `S >> A` numerically (18 vs 6 decimals).
2. Shareholder repeatedly calls `redeem(1, controller, controller)`; each call computes `assets = (1*A)/S == 0`, decrements `claimableRedeemShares` by 1 and leaves `claimableRedeemAssets`/`totalClaimableRedeemAssets` unchanged.
3. After `S` such calls, `claimableRedeemShares[controller] == 0` while `claimableRedeemAssets[controller] == A > 0`.
4. Any subsequent `redeem`/`withdraw` call reverts with `NoClaimableRedeem`, permanently stranding `A` USDC in the vault, still counted in `totalClaimableRedeemAssets`. [3](#0-2)

### Citations

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L358-386)
```text
  function approveRedemption(
    address controller,
    uint256 shares
  ) external onlyRole(INVESTOR_MANAGER) whenNotPaused returns (uint256 assets) {
    _requireFreshNav();
    require(shares > 0, ZeroAmount());

    uint256 pending = pendingRedeemShares[controller];
    require(pending > 0, NoPendingRedeem());
    require(shares <= pending, ExceedsPending());

    uint256 totalSupply = shareToken.totalSupply();
    assets = (shares * lastNav) / totalSupply;
    // Prevents approving a tiny amount that rounds to 0 assets, which would burn shares for nothing
    require(assets > 0, ZeroAmount());
    // Reserve must be backed by idle USDC; otherwise NAV finalization would underflow
    require(assets <= idleLiquidity(), InsufficientLiquidity());

    pendingRedeemShares[controller] = pending - shares;
    claimableRedeemShares[controller] += shares;
    claimableRedeemAssets[controller] += assets;
    totalClaimableRedeemAssets += assets;
    lastNav -= assets;

    // Burn shares so totalSupply stays correct for subsequent approvals
    shareToken.burn(address(this), shares);

    emit RedeemApproved(controller, shares, assets);
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L753-790)
```text
  function redeem(
    uint256 shares,
    address receiver,
    address controller
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(controller) returns (uint256 assets) {
    _requireInvestor(controller);
    _requireInvestor(receiver);
    uint256 claimableShares_ = claimableRedeemShares[controller];
    uint256 claimableAssets_ = claimableRedeemAssets[controller];
    require(claimableShares_ > 0 && claimableAssets_ > 0, NoClaimableRedeem());
    require(shares > 0 && shares <= claimableShares_, ExceedsClaimable());

    assets = (shares * claimableAssets_) / claimableShares_;
    _claimRedeem(controller, receiver, assets, shares, claimableAssets_, claimableShares_);
  }

  /**
   * @notice Claims an approved redemption by transferring exact assets (shares already burned at approval)
   * @param assets Amount of assets to withdraw
   * @param receiver Address to receive the assets
   * @param controller The controller of the redeem request
   * @return shares The number of shares deducted from claimable pool
   */
  function withdraw(
    uint256 assets,
    address receiver,
    address controller
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(controller) returns (uint256 shares) {
    _requireInvestor(controller);
    _requireInvestor(receiver);
    uint256 claimableShares_ = claimableRedeemShares[controller];
    uint256 claimableAssets_ = claimableRedeemAssets[controller];
    require(claimableShares_ > 0 && claimableAssets_ > 0, NoClaimableRedeem());
    require(assets > 0 && assets <= claimableAssets_, ExceedsClaimable());

    shares = (assets * claimableShares_) / claimableAssets_;
    _claimRedeem(controller, receiver, assets, shares, claimableAssets_, claimableShares_);
  }
```
