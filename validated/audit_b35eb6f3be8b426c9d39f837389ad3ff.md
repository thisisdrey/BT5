### Title
Debt interest is under-accrued because `interestRate` is divided before multiplying elapsed time - (File: contracts/DebtToken.sol)

### Summary
`DebtToken.interestRatePerSecond()` truncates the annual interest rate before `_calculateInterestAccrual()` multiplies it by elapsed time. Because Solidity integer division rounds down, any nonzero `interestRate % SECONDS_PER_YEAR` is permanently discarded for every elapsed second. This undercounts borrower debt and the synthetic-token interest minted to the fee collector. [1](#0-0) [2](#0-1) 

### Finding Description
`interestRate` is a WAD-scaled annual rate, where `0.1e18` represents 10% APR. [3](#0-2) 

The implementation first calculates:

```solidity
interestRate / SECONDS_PER_YEAR
```

It then multiplies the already-truncated result by `block.timestamp - lastTimestampAccrued`. [4](#0-3) [5](#0-4) 

The mathematically equivalent calculation should instead multiply first:

```solidity
(interestRate * elapsed) / SECONDS_PER_YEAR
```

For an annual rate that is not an exact multiple of `31,557,600`, the current code loses up to `SECONDS_PER_YEAR - 1` WAD units on every second of accrual. If `0 < interestRate < SECONDS_PER_YEAR`, the per-second rate truncates to zero and no interest ever accrues. [6](#0-5) [2](#0-1) 

`accrueInterest()` is publicly callable and is also invoked before issuing, repaying, flash-issuing, minting through SmartFarmingManager, and liquidating debt, so normal protocol usage continuously commits the reduced accrual to `totalSupply_` and `debtIndex`. [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9) 

### Impact Explanation
The missing numerator causes `_interestAmountAccrued` and the `debtIndex` increment to be lower than the configured APR requires. [2](#0-1) 

This produces two effects:

- Borrowers' `balanceOf()` values are understated because account debt is calculated from `principalOf * debtIndex / debtIndexOf`. [11](#0-10) 
- The fee collector receives less synthetic-token interest because only the understated `_interestAmountAccrued` is minted to it. [12](#0-11) 

For annual rates below `SECONDS_PER_YEAR`, the loss is complete: the protocol can have positive debt and a positive configured rate while every accrual produces zero interest. For ordinary larger rates, the loss is smaller but persistent and scales with both elapsed time and outstanding debt.

### Likelihood Explanation
The condition is present whenever the configured `interestRate` is not exactly divisible by `SECONDS_PER_YEAR`; no privileged action is required at accrual time. Any account can invoke `accrueInterest()`, while all core debt mutations call it internally before calculating balances or settling debt. [13](#0-12) [14](#0-13) [15](#0-14) 

Pause, shutdown, reentrancy, and token-registration checks do not prevent the public accrual path or the view-side understatement used by `balanceOf()`, `totalSupply()`, and `Pool.debtPositionOf()`. [16](#0-15) [17](#0-16) 

### Recommendation
Keep the annual rate intact until after elapsed-time multiplication:

```solidity
uint256 _interestRateToAccrue =
    (interestRate * (block.timestamp - _lastTimestampAccrued)) / SECONDS_PER_YEAR;
```

Prefer `Math.mulDiv(interestRate, elapsed, SECONDS_PER_YEAR)` to reduce overflow risk while preserving the multiply-before-divide ordering. If economically feasible, store rates in higher precision than WAD-per-year, such as WAD-per-second or ray-based accumulators, so sub-second rates are not truncated.

### Proof of Concept
This Foundry fork test compares committed protocol accrual with the equivalent multiply-before-divide calculation. It does not modify `interestRate`, debt, or accounting storage; it only advances time and calls public/view functions.

```solidity
// test/foundry/poc/DebtInterestRounding.t.sol
pragma solidity 0.8.24;

import "forge-std/Test.sol";

interface IDebtToken {
    function SECONDS_PER_YEAR() external view returns (uint256);
    function accrueInterest() external;
    function interestRate() external view returns (uint256);
    function lastTimestampAccrued() external view returns (uint256);
    function totalSupply() external view returns (uint256);
}

contract DebtInterestRoundingPoC is Test {
    // deployments/mainnet/MsUSDDebt_Pool1.json
    IDebtToken internal constant MSUSD_DEBT =
        IDebtToken(0x480e3178Fa102dF852643d47CAbdb9adf5dB0174);

    uint256 internal constant WAD = 1e18;
    uint256 internal constant HALF_WAD = WAD / 2;

    function test_DivisionBeforeMultiplicationUnderAccruesInterest() external {
        vm.createSelectFork(vm.envString("MAINNET_RPC_URL"));

        uint256 annualRate = MSUSD_DEBT.interestRate();
        assertGt(annualRate, 0, "fork requires a configured nonzero APR");

        MSUSD_DEBT.accrueInterest();
        uint256 supplyBefore = MSUSD_DEBT.totalSupply();
        assertGt(supplyBefore, 0, "fork requires outstanding debt");

        uint256 elapsed = 365 days;
        vm.warp(block.timestamp + elapsed);

        uint256 secondsPerYear = MSUSD_DEBT.SECONDS_PER_YEAR();

        // Actual implementation:
        // interestRatePerSecond() * elapsed
        uint256 flawedRate =
            (annualRate / secondsPerYear) * elapsed;
        uint256 flawedInterest =
            (supplyBefore * flawedRate + HALF_WAD) / WAD;

        // Correct ordering:
        // annualRate * elapsed / SECONDS_PER_YEAR
        uint256 exactRate =
            (annualRate * elapsed) / secondsPerYear;
        uint256 exactInterest =
            (supplyBefore * exactRate + HALF_WAD) / WAD;

        assertEq(
            MSUSD_DEBT.totalSupply(),
            supplyBefore + flawedInterest,
            "PoC did not reproduce the implementation"
        );

        assertLt(flawedRate, exactRate, "no rate truncation detected");
        assertLt(
            supplyBefore + flawedInterest,
            supplyBefore + exactInterest,
            "interest was not understated"
        );
    }
}
```

### Citations

**File:** contracts/DebtToken.sol (L54-55)
```text
    uint256 public constant SECONDS_PER_YEAR = 365.25 days;
    uint256 private constant HUNDRED_PERCENT = 1e18;
```

**File:** contracts/DebtToken.sol (L156-178)
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
            }
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

**File:** contracts/DebtToken.sol (L235-264)
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
        if (amount_ == 0) revert AmountIsZero();

        accrueInterest();

        address _msgSender = _msgSender();
        IPool _pool = pool;
        ISyntheticToken _syntheticToken = syntheticToken;

        (, , , , uint256 _issuableInUsd) = _pool.debtPositionOf(_msgSender);

        IMasterOracle _masterOracle = _pool.masterOracle();

        if (amount_ > _masterOracle.quoteUsdToToken(address(_syntheticToken), _issuableInUsd)) {
            revert NotEnoughCollateral();
        }

        _mint(_pool, _masterOracle, _msgSender, amount_);

        (_issued, _fee) = quoteIssueOut(amount_);
```

**File:** contracts/DebtToken.sol (L316-321)
```text
    /**
     * @notice Return interest rate (in percent) per second
     */
    function interestRatePerSecond() public view override returns (uint256) {
        return interestRate / SECONDS_PER_YEAR;
    }
```

**File:** contracts/DebtToken.sol (L418-454)
```text
    function repay(
        address onBehalfOf_,
        uint256 amount_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        returns (uint256 _repaid, uint256 _fee)
    {
        if (amount_ == 0) revert AmountIsZero();

        accrueInterest();

        address _msgSender = _msgSender();
        IPool _pool = pool;
        ISyntheticToken _syntheticToken = syntheticToken;

        (_repaid, _fee) = quoteRepayOut(amount_);
        if (_fee > 0) {
            _syntheticToken.seize(_msgSender, _pool.feeCollector(), _fee);
        }

        uint256 _debtFloorInUsd = _pool.debtFloorInUsd();
        if (_debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = _pool.masterOracle().quoteTokenToUsd(
                address(_syntheticToken),
                balanceOf(onBehalfOf_) - _repaid
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < _debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }

        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);
```

**File:** contracts/DebtToken.sol (L500-503)
```text
    function totalSupply() external view override returns (uint256) {
        (uint256 _interestAmountAccrued, , ) = _calculateInterestAccrual();
        return totalSupply_ + _interestAmountAccrued;
    }
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

**File:** contracts/Pool.sol (L227-235)
```text
    function debtOf(address account_) public view override returns (uint256 _debtInUsd) {
        IMasterOracle _masterOracle = masterOracle();
        uint256 _length = debtTokensOfAccount.length(account_);
        for (uint256 i; i < _length; ++i) {
            IDebtToken _debtToken = IDebtToken(debtTokensOfAccount.at(account_, i));
            _debtInUsd += _masterOracle.quoteTokenToUsd(
                address(_debtToken.syntheticToken()),
                _debtToken.balanceOf(account_)
            );
```

**File:** contracts/Pool.sol (L551-558)
```text
        address _msgSender = _msgSender();

        if (amountToRepay_ == 0) revert AmountIsZero();
        if (_msgSender == account_) revert CanNotLiquidateOwnPosition();

        IDebtToken _debtToken = debtTokenOf[syntheticToken_];
        _debtToken.accrueInterest();

```
