### Title
ERC-7575 `maxDeposit`/`maxMint` return misleading non-zero values while the standard two-param `deposit`/`mint` revert - (File: `tare-io__tare-contracts/contracts/PortfolioVault.sol`)

### Summary
`PortfolioVault` exposes ERC-7575-style `maxDeposit` and `maxMint` views, but the standard two-parameter `deposit(uint256,address)` and `mint(uint256,address)` functions unconditionally revert. The `max*` views instead return the controller's claimable deposit assets/shares, which only map to the non-standard three-parameter claim functions. This reproduces the external GlmVault root cause: `max*` values do not reflect the actual callable `deposit`/`mint` paths and will mislead standard vault integrators.

### Finding Description
`PortfolioVault` explicitly disables the ERC-7575 two-param entry points:

- `deposit(uint256, address)` reverts with `MustRevert()` [1](#0-0) 
- `mint(uint256, address)` reverts with `MustRevert()` [2](#0-1) 

Yet the corresponding `max*` views return non-zero claimable amounts:

- `maxDeposit(controller)` returns `claimableDepositAssets[controller]` (or `0` only when paused) [3](#0-2) 
- `maxMint(controller)` returns `claimableDepositShares[controller]` (or `0` only when paused) [4](#0-3) 

Those returned values are only valid for the three-parameter `deposit(assets, receiver, controller)` [5](#0-4)  and `mint(shares, receiver, controller)` [6](#0-5) , not for the standard two-param ERC-7575 functions that `maxDeposit`/`maxMint` are defined to accompany. The spec even documents that the two-param variants "MUST revert" [7](#0-6) , while the `max*` views are described as reflecting the "corresponding `deposit`/`mint`/`redeem`/`withdraw` claims" [8](#0-7) —a description that does not hold for the two-param `deposit`/`mint`.

### Impact Explanation
An external protocol integrating with `PortfolioVault` through the standard ERC-7575/ERC-4626 interface will call `maxDeposit(receiver)`, receive a positive value, approve USDC, and then call `deposit(amount, receiver)`. The call reverts with `MustRevert()`, blocking the deposit flow. This is a production DoS for standard vault integrations and can cause failed transactions, wasted gas, and incorrect integration assumptions. It violates the ERC-7575 invariant that `maxDeposit`/`maxMint` must return the maximum amount that can be successfully passed to the corresponding `deposit`/`mint` function.

### Likelihood Explanation
High. Any unprivileged external caller or contract can read `maxDeposit`/`maxMint`, and any standard ERC-7575/ERC-4626 integration will naturally call the two-param `deposit`/`mint` after reading the max views. The vault advertises ERC-7575 compatibility via `supportsInterface` [9](#0-8) , so integrations are expected.

### Recommendation
Make `maxDeposit` and `maxMint` consistent with the two-param `deposit`/`mint` functions they are paired with in `IERC7575`. Since those two-param functions are intentionally disabled, `maxDeposit` and `maxMint` should return `0` for all controllers (the paused branch already does this). If the intent is to expose async claim capacity, do so through separate view functions (e.g., `claimableDepositAssets`/`claimableDepositShares` already exist) and keep the ERC-7575 `max*` views compliant.

### Proof of Concept
The existing test suite already demonstrates the mismatch:

- `test_MaxDeposit_ReturnsClaimableAssets` shows `maxDeposit` returns `DEFAULT_DEPOSIT_AMOUNT` after approval [10](#0-9) 
- `test_SyncDeposit_Reverts` shows `vault.deposit(1000, shareholder1)` reverts with `MustRevert` [11](#0-10) 
- `test_MaxMint_ReturnsClaimableShares` shows `maxMint` returns non-zero claimable shares [12](#0-11) 
- `test_SyncMint_Reverts` shows `vault.mint(1000, shareholder1)` reverts with `MustRevert` [13](#0-12) 

A concrete integration failure:
1. ERC-7575 aggregator calls `vault.maxDeposit(alice)` → returns `1000e6`
2. Aggregator approves `1000e6` USDC and calls `vault.deposit(1000e6, alice)`
3. Transaction reverts `MustRevert()`; the deposit is blocked

### Citations

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L628-641)
```text
  function deposit(
    uint256 assets,
    address receiver,
    address controller
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(controller) returns (uint256 shares) {
    _requireInvestor(controller);
    uint256 claimableAssets_ = claimableDepositAssets[controller];
    uint256 claimableShares_ = claimableDepositShares[controller];
    require(claimableAssets_ > 0 && claimableShares_ > 0, NoClaimableDeposit());
    require(assets > 0 && assets <= claimableAssets_, ExceedsClaimable());

    shares = (assets * claimableShares_) / claimableAssets_;
    _claimDeposit(controller, receiver, assets, shares, claimableAssets_, claimableShares_);
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L650-663)
```text
  function mint(
    uint256 shares,
    address receiver,
    address controller
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(controller) returns (uint256 assets) {
    _requireInvestor(controller);
    uint256 claimableAssets_ = claimableDepositAssets[controller];
    uint256 claimableShares_ = claimableDepositShares[controller];
    require(claimableAssets_ > 0 && claimableShares_ > 0, NoClaimableDeposit());
    require(shares > 0 && shares <= claimableShares_, ExceedsClaimable());

    assets = (shares * claimableAssets_) / claimableShares_;
    _claimDeposit(controller, receiver, assets, shares, claimableAssets_, claimableShares_);
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L695-698)
```text
  /// @inheritdoc IERC7575
  function deposit(uint256, address) external pure returns (uint256) {
    revert MustRevert();
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L700-703)
```text
  /// @inheritdoc IERC7575
  function mint(uint256, address) external pure returns (uint256) {
    revert MustRevert();
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L881-885)
```text
  /// @inheritdoc IERC7575
  function maxDeposit(address controller) public view returns (uint256) {
    if (paused()) return 0;
    return claimableDepositAssets[controller];
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L887-891)
```text
  /// @inheritdoc IERC7575
  function maxMint(address controller) external view returns (uint256) {
    if (paused()) return 0;
    return claimableDepositShares[controller];
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L910-917)
```text
  function supportsInterface(bytes4 interfaceId) public view override(AccessControl, IERC165) returns (bool) {
    return
      interfaceId == 0xe3bc4e65 || // IERC7540Operator
      interfaceId == 0x2f0a18c5 || // IERC7575
      interfaceId == 0xce3bbe50 || // IERC7540Deposit
      interfaceId == 0x620ee8e4 || // IERC7540Redeem
      interfaceId == type(IERC721Receiver).interfaceId ||
      super.supportsInterface(interfaceId);
```

**File:** tare-io__tare-contracts/specs/vault.md (L936-940)
```markdown
**Methods that MUST revert:**

- `deposit(uint256, address)` — 2-param ERC-4626 variant, **MUST revert** (use 3-param async version)
- `mint(uint256, address)` — 2-param ERC-4626 variant, **MUST revert** (use 3-param async version)
- `previewDeposit`, `previewMint` — cannot know if/when deposit will be approved
```

**File:** tare-io__tare-contracts/specs/vault.md (L1190-1193)
```markdown
**View functions reflecting the paused state**:

- `maxDeposit` / `maxMint` / `maxWithdraw` / `maxRedeem` — return `0` while paused, since the corresponding `deposit`/`mint`/`redeem`/`withdraw` claims are blocked by `whenNotPaused` (per ERC-4626/ERC-7575, a `max*` function must return `0` when its action is disabled).

```

**File:** tare-io__tare-contracts/test/Vault_AsyncDeposit.t.sol (L906-917)
```text
  function test_MaxDeposit_ReturnsClaimableAssets() public {
    _setupInitialNav();
    _fundShareholder(shareholder1, DEFAULT_DEPOSIT_AMOUNT);

    vm.prank(shareholder1);
    vault.requestDeposit(DEFAULT_DEPOSIT_AMOUNT, shareholder1, shareholder1);

    vm.prank(manager);
    vault.approveDeposit(shareholder1, DEFAULT_DEPOSIT_AMOUNT);

    assertEq(vault.maxDeposit(shareholder1), DEFAULT_DEPOSIT_AMOUNT);
  }
```

**File:** tare-io__tare-contracts/test/Vault_AsyncDeposit.t.sol (L923-934)
```text
  function test_MaxMint_ReturnsClaimableShares() public {
    _setupInitialNav();
    _fundShareholder(shareholder1, DEFAULT_DEPOSIT_AMOUNT);

    vm.prank(shareholder1);
    vault.requestDeposit(DEFAULT_DEPOSIT_AMOUNT, shareholder1, shareholder1);

    vm.prank(manager);
    vault.approveDeposit(shareholder1, DEFAULT_DEPOSIT_AMOUNT);

    assertEq(vault.maxMint(shareholder1), vault.claimableDepositShares(shareholder1));
  }
```

**File:** tare-io__tare-contracts/test/Vault_AsyncDeposit.t.sol (L972-975)
```text
  function test_SyncDeposit_Reverts() public {
    vm.expectRevert(abi.encodeWithSelector(IPortfolioVault.MustRevert.selector));
    vault.deposit(1000, shareholder1);
  }
```

**File:** tare-io__tare-contracts/test/Vault_AsyncDeposit.t.sol (L977-980)
```text
  function test_SyncMint_Reverts() public {
    vm.expectRevert(abi.encodeWithSelector(IPortfolioVault.MustRevert.selector));
    vault.mint(1000, shareholder1);
  }
```
