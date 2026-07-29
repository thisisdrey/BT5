No vulnerability found for this question.

Tare's `PortfolioVault` does not have any concept analogous to `processingMode`/`RequestPrice`. Redemption pricing is locked once, at approval time, inside `approveRedemption`, not at request time: `assets = (shares * lastNav) / totalSupply` [1](#0-0) , and the resulting `claimableRedeemAssets`/`claimableRedeemShares` are stored per controller and consumed later by `redeem()`/`withdraw()` using that fixed ratio rather than any live `sharePrice()` [2](#0-1) . There is only a single, consistent pricing point (approval time) across the whole redeem lifecycle — `requestRedeem` performs no pricing at all (it only locks shares) [3](#0-2) , and this is an explicit design decision documented in the spec ("Share price locked at time of approval") [4](#0-3) .

The external report's root cause is a vault that supports two alternate pricing modes (`RequestPrice` vs `CurrentPrice`) where one code path (`fulfillRedeemRequest`) forgets to respect the configured mode and always uses the live price. Tare's vault has no equivalent dual-mode configuration and no function that bypasses a stored request-time price in favor of a live price — there is no stored request-time price to bypass in the first place. Hence there is no reachable analog of this bug class in this repository.

### Citations

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L369-370)
```text
    uint256 totalSupply = shareToken.totalSupply();
    assets = (shares * lastNav) / totalSupply;
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L728-744)
```text
  function requestRedeem(
    uint256 shares,
    address controller,
    address owner
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(owner) returns (uint256 requestId) {
    require(controller != address(this), InvalidController());
    _requireInvestor(controller);
    require(shares > 0, ZeroAmount());

    // Lock shares by transferring from owner to vault
    IERC20(address(shareToken)).safeTransferFrom(owner, address(this), shares);

    pendingRedeemShares[controller] += shares;

    emit RedeemRequest(controller, owner, 0, msg.sender, shares);
    return 0;
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L760-789)
```text
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
```

**File:** tare-io__tare-contracts/specs/vault.md (L172-179)
```markdown
2. **Approval**: Manager calls `approveRedemption(controller, shares)` - This is the valuation point
   - Manager specifies exact number of pending shares to approve (partial or full)
   - The resulting `assets` must be `<= idleLiquidity()` (vault USDC balance minus pending deposits and already-claimable redemptions); otherwise the call reverts with `InsufficientLiquidity`. This guarantees every approved redemption is immediately fundable and prevents the NAV finalization formula from underflowing
   - Share price locked at time of approval
   - Shares are burned from the vault and `lastNav` is adjusted downward by `assets` — this keeps `totalSupply` and NAV synchronized for any subsequent approvals within the same NAV window
   - Multiple partial approvals accumulate: `claimableRedeemShares` and `claimableRedeemAssets` are additive across calls, and `redeem()` uses the proportional ratio between the two for conversion — naturally handling weighted-average pricing when partial approvals happen at different NAV values
   - Request becomes (partially or fully) claimable
   - Emits `RedeemApproved` event
```
