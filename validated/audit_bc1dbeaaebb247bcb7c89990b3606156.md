### Title
`DebtToken.issue` and `DebtToken.flashIssue` ignore the `SyntheticToken.isActive()` flag, allowing continued minting of a disabled synthetic asset - ([File: contracts/DebtToken.sol])

### Summary
The Mattermost bug class is "an endpoint fails to check a configuration flag meant to restrict it." The direct analog exists in `DebtToken`: the governor-controlled `isActive` flag on `SyntheticToken` is enforced by the `onlyIfSyntheticTokenIsActive` modifier, but that modifier is only applied to `mint` (the SmartFarmingManager path). The user-facing `issue` function and the SFM-facing `flashIssue` function omit it, so a synthetic token that governance explicitly deactivated can still be minted by any user.

### Finding Description
`SyntheticToken` exposes an `isActive()` flag, and `DebtToken` defines a dedicated modifier to enforce it [1](#0-0) . That modifier is correctly applied to `mint` [2](#0-1) , but `issue` only carries `whenNotShutdown`, `nonReentrant`, and `onlyIfSyntheticTokenExists` [3](#0-2)  — and then calls `syntheticToken.mint(to_, _issued)` directly [4](#0-3) . `flashIssue` similarly lacks `onlyIfSyntheticTokenIsActive` and mints the synthetic token directly [5](#0-4) . The internal `_mint` only checks the *debt token's* active flag via `onlyIfDebtTokenIsActive` [6](#0-5) , not the synthetic token's.

Likewise, `Pool.swap` only checks the pool-level `isSwapActive` flag and token existence — not `syntheticTokenIn_.isActive()` — so newly minted units of a disabled synth remain fully swappable [7](#0-6) .

### Impact Explanation
The `isActive` flag on `SyntheticToken` is the emergency/governance lever to halt issuance of a specific asset (e.g., if its oracle or underlying market is compromised or being deprecated). Because `issue` ignores it, an unprivileged user with any collateral can:

1. Call `DebtToken.issue(amount_, attacker)` on the deactivated synth's debt token — the only gate is `debtPositionOf` collateral sufficiency [8](#0-7) .
2. Immediately dump the minted synth via `Pool.swap` into a healthy synthetic asset (e.g., msUSD), since `swap` never checks `isActive` [9](#0-8) .

If the synth was disabled precisely because its price feed is stale/broken or the asset is being wound down, the attacker mints it at oracle price against collateral and swaps it at oracle price for sound synths, extracting value while the protocol believed issuance was halted — protocol insolvency/direct value extraction. This mirrors the CVE: a state-changing endpoint skips a flag check that other sibling endpoints enforce.

### Likelihood Explanation
Medium-low. Exploitation requires a window where a synthetic token is deactivated while its `DebtToken` remains active and its oracle still returns a price — a plausible but governor-dependent configuration. Once that state exists, any EOA can execute it with only collateral; no privileged role, flash loan, or oracle manipulation is needed. The defense-in-depth gap is clear-cut: the modifier exists, is applied on one path (`mint`), and omitted on the two public issuance paths (`issue`, `flashIssue`).

### Recommendation
Add `onlyIfSyntheticTokenIsActive` to both `issue` [3](#0-2)  and `flashIssue` [10](#0-9) , matching `mint`. Consider also gating `Pool.swap` on `syntheticTokenIn_.isActive()`/`syntheticTokenOut_.isActive()` so a disabled synth cannot be traded into the pool at oracle prices.

### Proof of Concept
Hardhat sketch (fork not strictly required; the unit-level repro suffices):

```ts
// setup: pool, depositToken (msd-WETH), debtToken/syntheticToken (msETH-like), user deposits collateral
await depositToken.deposit(collateralAmount, user.address);

// governor disables the synthetic token only (debt token stays active)
await syntheticToken.connect(governor).toggleIsActive(); // isActive() == false

// mint() path is correctly blocked (onlyIfSyntheticTokenIsActive)
await expect(
  debtToken.connect(sfm).mint(user.address, amount) // via SmartFarmingManager
).revertedWithCustomError(debtToken, 'SyntheticIsInactive');

// BUG: issue() path is NOT blocked — flag ignored
await debtToken.connect(user).issue(amount, user.address);
expect(await syntheticToken.balanceOf(user.address)).to.be.gt(0); // minted despite isActive == false

// attacker converts disabled synth into healthy synth at oracle price
await pool.connect(user).swap(syntheticToken.address, healthySynth.address, issued);
expect(await healthySynth.balanceOf(user.address)).to.be.gt(0);
```

The repro demonstrates the invariant break: `SyntheticToken.isActive() == false` is meant to halt issuance, yet `issue` mints and `swap` accepts the token, enabling value extraction from a disabled asset.

### Citations

**File:** contracts/DebtToken.sol (L105-108)
```text
    modifier onlyIfSyntheticTokenIsActive() {
        if (!syntheticToken.isActive()) revert SyntheticIsInactive();
        _;
    }
```

**File:** contracts/DebtToken.sol (L235-245)
```text
    function issue(
        uint256 amount_,
        address to_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        returns (uint256 _issued, uint256 _fee)
    {
```

**File:** contracts/DebtToken.sol (L254-259)
```text
        (, , , , uint256 _issuableInUsd) = _pool.debtPositionOf(_msgSender);

        IMasterOracle _masterOracle = _pool.masterOracle();

        if (amount_ > _masterOracle.quoteUsdToToken(address(_syntheticToken), _issuableInUsd)) {
            revert NotEnoughCollateral();
```

**File:** contracts/DebtToken.sol (L264-270)
```text
        (_issued, _fee) = quoteIssueOut(amount_);
        if (_fee > 0) {
            _syntheticToken.mint(_pool.feeCollector(), _fee);
        }
        _syntheticToken.mint(to_, _issued);

        emit SyntheticTokenIssued(_msgSender, to_, amount_, _issued, _fee);
```

**File:** contracts/DebtToken.sol (L281-305)
```text
    function flashIssue(
        address to_,
        uint256 amount_
    )
        external
        override
        onlyIfSmartFarmingManager
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        onlyIfDebtTokenIsActive
        returns (uint256 _issued, uint256 _fee)
    {
        if (amount_ == 0) revert AmountIsZero();

        accrueInterest();

        ISyntheticToken _syntheticToken = syntheticToken;

        (_issued, _fee) = quoteIssueOut(amount_);
        if (_fee > 0) {
            _syntheticToken.mint(pool.feeCollector(), _fee);
        }
        _syntheticToken.mint(to_, _issued);
    }
```

**File:** contracts/DebtToken.sol (L328-339)
```text
    function mint(
        address to_,
        uint256 amount_
    )
        external
        override
        onlyIfSmartFarmingManager
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        onlyIfSyntheticTokenIsActive
    {
```

**File:** contracts/DebtToken.sol (L572-577)
```text
    function _mint(
        IPool pool_,
        IMasterOracle masterOracle_,
        address account_,
        uint256 amount_
    ) private onlyIfDebtTokenIsActive updateRewardsBeforeMintOrBurn(account_) {
```

**File:** contracts/Pool.sol (L642-670)
```text
    function swap(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists(syntheticTokenIn_)
        onlyIfSyntheticTokenExists(syntheticTokenOut_)
        returns (uint256 _amountOut, uint256 _fee)
    {
        address _msgSender = _msgSender();

        if (!isSwapActive) revert SwapFeatureIsInactive();
        if (amountIn_ == 0 || amountIn_ > syntheticTokenIn_.balanceOf(_msgSender)) revert AmountInIsInvalid();

        syntheticTokenIn_.burn(_msgSender, amountIn_);

        (_amountOut, _fee) = quoteSwapOut(syntheticTokenIn_, syntheticTokenOut_, amountIn_);

        if (_fee > 0) {
            syntheticTokenOut_.mint(_poolRegistry.feeCollector(), _fee);
        }

        syntheticTokenOut_.mint(_msgSender, _amountOut);

        emit SyntheticTokenSwapped(_msgSender, syntheticTokenIn_, syntheticTokenOut_, amountIn_, _amountOut, _fee);
```
