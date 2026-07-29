[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** tare-io__tare-contracts/test/Vault_NavSecurity.t.sol (L449-465)
```text
    uint256 claimableSharesB = vault.claimableDepositShares(shareholder2);
    uint256 claimableAssetsB = vault.claimableDepositAssets(shareholder2);

    vm.prank(shareholder2);
    uint256 assetsViaMint = vault.mint(claimableSharesB, shareholder2, shareholder2);

    assertEq(assetsViaMint, claimableAssetsB, "mint(fullShares) should consume all claimable assets");
    assertEq(sharesViaDeposit, claimableSharesB, "deposit and mint yield same shares for same deposit");
  }

  /**
   * @notice Trying to extract extra shares by calling deposit(1 wei assets) repeatedly.
   * Each call rounds shares DOWN, so the attacker gets fewer total shares than one big call.
   */
  function test_DepositVsMint_SplittingDoesNotExtraShares() public {
    _setupInitialNav();

```

**File:** tare-io__tare-contracts/test/Vault_NavSecurity.t.sol (L541-576)
```text
    vm.prank(shareholder1);
    shareToken.approve(address(vault), type(uint256).max);

    // Request redeem
    vm.prank(shareholder1);
    vault.requestRedeem(shares, shareholder1, shareholder1);
    vm.prank(manager);
    vault.approveRedemption(shareholder1, shares);

    uint256 claimableShares = vault.claimableRedeemShares(shareholder1);
    uint256 claimableAssets = vault.claimableRedeemAssets(shareholder1);

    // Full redeem by shares
    vm.prank(shareholder1);
    uint256 assetsFromRedeem = vault.redeem(claimableShares, shareholder1, shareholder1);

    assertEq(assetsFromRedeem, claimableAssets, "redeem(allShares) should yield all claimable assets");

    // --- Same setup for shareholder2 via withdraw() ---
    uint256 shares2 = _depositAndClaim(shareholder2, depositAmount);
    vm.prank(shareholder2);
    shareToken.approve(address(vault), type(uint256).max);

    vm.prank(shareholder2);
    vault.requestRedeem(shares2, shareholder2, shareholder2);
    vm.prank(manager);
    vault.approveRedemption(shareholder2, shares2);

    uint256 claimableAssets2 = vault.claimableRedeemAssets(shareholder2);
    uint256 claimableShares2 = vault.claimableRedeemShares(shareholder2);

    vm.prank(shareholder2);
    uint256 sharesFromWithdraw = vault.withdraw(claimableAssets2, shareholder2, shareholder2);

    assertEq(sharesFromWithdraw, claimableShares2, "withdraw(allAssets) should burn all claimable shares");
  }
```

**File:** tare-io__tare-contracts/test/Vault_NavSecurity.t.sol (L581-600)
```text
  function test_Redeem_SplittingDoesNotExtraAssets() public {
    uint256 depositAmount = 100_000e6;
    uint256 shares = _setupShareholderWithShares(shareholder1, depositAmount, DEFAULT_LOAN_VALUATION);

    vm.prank(shareholder1);
    vault.requestRedeem(shares, shareholder1, shareholder1);
    vm.prank(manager);
    vault.approveRedemption(shareholder1, shares);

    uint256 totalClaimableShares = vault.claimableRedeemShares(shareholder1);
    uint256 totalClaimableAssets = vault.claimableRedeemAssets(shareholder1);

    uint256 chunkShares = totalClaimableShares / 100;
    uint256 totalAssetsFromChunks;

    for (uint256 i; i < 99; ++i) {
      vm.prank(shareholder1);
      totalAssetsFromChunks += vault.redeem(chunkShares, shareholder1, shareholder1);
    }
    uint256 remaining = vault.claimableRedeemShares(shareholder1);
```

**File:** tare-io__tare-contracts/test/Vault_NavSecurity.t.sol (L1227-1256)
```text
  /**
   * @notice totalClaimableRedeemAssets counter stays consistent across multiple partial
   * approvals and claims for multiple controllers.
   */
  function test_Conservation_TotalClaimableRedeemAssets_Consistent() public {
    _setupInitialNav();

    // Both deposit and get shares
    uint256 shares1 = _depositAndClaim(shareholder1, 60_000e6);
    uint256 shares2 = _depositAndClaim(shareholder2, 40_000e6);

    vm.prank(shareholder1);
    shareToken.approve(address(vault), type(uint256).max);
    vm.prank(shareholder2);
    shareToken.approve(address(vault), type(uint256).max);

    // Both request redeem
    vm.prank(shareholder1);
    vault.requestRedeem(shares1, shareholder1, shareholder1);
    vm.prank(shareholder2);
    vault.requestRedeem(shares2, shareholder2, shareholder2);

    // Approve both
    vm.prank(manager);
    vault.approveRedemption(shareholder1, shares1);
    vm.prank(manager);
    vault.approveRedemption(shareholder2, shares2);

    uint256 expected = vault.claimableRedeemAssets(shareholder1) + vault.claimableRedeemAssets(shareholder2);
    assertEq(vault.totalClaimableRedeemAssets(), expected, "counter should equal sum of individual");
```

**File:** tare-io__tare-contracts/test/Vault_AsyncRedeem.t.sol (L422-447)
```text
  function test_Redeem_FullClaim_LeavesNoDust() public {
    uint256 shares = _setupShareholderWithShares();

    vm.prank(shareholder1);
    vault.requestRedeem(shares, shareholder1, shareholder1);

    vm.prank(manager);
    vault.approveRedemption(shareholder1, shares);

    uint256 claimableShares = vault.maxRedeem(shareholder1);

    // Claim in 3 chunks: should leave no dust
    uint256 chunk = claimableShares / 3;
    vm.prank(shareholder1);
    vault.redeem(chunk, shareholder1, shareholder1);

    vm.prank(shareholder1);
    vault.redeem(chunk, shareholder1, shareholder1);

    uint256 remaining = vault.maxRedeem(shareholder1);
    vm.prank(shareholder1);
    vault.redeem(remaining, shareholder1, shareholder1);

    assertEq(vault.claimableRedeemShares(shareholder1), 0, "claimable shares should be 0");
    assertEq(vault.claimableRedeemAssets(shareholder1), 0, "claimable assets should be 0");
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
