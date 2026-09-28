### Title
Positions borrowing the full collateral-factor limit can become liquidatable immediately - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`DebtToken.issue()` permits debt issuance up to and including the account’s full issuable USD limit. `Pool.debtPositionOf()` treats `debtInUsd <= issuableLimitInUsd` as healthy, while `Pool.liquidate()` uses that same boundary and reverts only when the position remains healthy. There is no separate liquidation threshold, safety buffer, or grace period. On interest-bearing debt tokens, a max-borrowed account can therefore become liquidatable as soon as the next interest accrual increases its debt balance by one wei.

### Finding Description
`issue()` first accrues interest, obtains `_issuableInUsd` from `Pool.debtPositionOf()`, and rejects only when the requested gross debt exceeds the oracle-converted issuable amount. Equality is accepted. [1](#0-0) 

`Pool.debtPositionOf()` defines the liquidation boundary as `debtInUsd <= issuableLimitInUsd`; the collateral-factor-adjusted collateral value is both the borrowing limit and the liquidation threshold. [2](#0-1) 

`Pool.liquidate()` calls `DebtToken.accrueInterest()` and then permits liquidation whenever the same `debtPositionOf()` result reports unhealthy. [3](#0-2) 

For debt tokens configured with a nonzero `interestRate`, `balanceOf()` projects the account principal onto an increasing `debtIndex`. After a later timestamp, `_calculateInterestAccrual()` raises the index and increases every account’s effective debt without requiring a separate user action. [4](#0-3) [5](#0-4) 

Thus, a borrower at exact equality is healthy in the issuance transaction but can be unhealthy in the next timestamp once accrued interest raises `_debtInUsd` above `_issuableLimitInUsd`. A 1-wei reduction in collateral value or increase in synthetic-debt value can produce the same result.

### Impact Explanation
A user borrowing at the protocol’s advertised maximum receives no liquidation buffer. Once even one wei of interest accrues, any third party holding sufficient synthetic tokens can call `Pool.liquidate()` and seize collateral. `quoteLiquidateOut()` prices the seized collateral and adds the configured liquidator incentive and protocol fee. [6](#0-5) 

The user loses collateral beyond the debt repaid because the liquidation incentive is transferred to the liquidator. The `maxLiquidable` check limits repayment size but does not provide a health-factor buffer. [7](#0-6) 

### Likelihood Explanation
Likelihood depends on deployment configuration. The condition is directly reachable whenever a debt token has a nonzero `interestRate`; waiting for the next timestamp is sufficient after an exact-limit issuance. Even with zero interest, a minimal adverse oracle price movement crosses the boundary because issuance and liquidation use identical equality semantics.

The attack path does not require privileged access. A liquidator can acquire repayment synths through its own collateralized issuance or external purchase and invoke the public `liquidate()` function. `onlyIfSyntheticTokenExists`, `onlyIfDepositTokenExists`, `nonReentrant`, and `whenNotShutdown` do not prevent liquidation of an actually unhealthy account.

### Recommendation
Introduce separate borrow and liquidation thresholds. For example:

- Maintain a `borrowFactor`/`mintFactor` lower than the liquidation `collateralFactor`, and calculate `_issuableLimitInUsd` with the lower factor.
- Alternatively, enforce `debtInUsd <= issuableLimitInUsd - LIQUIDATION_BUFFER` in `DebtToken.issue()` while retaining the current liquidation boundary.
- Ensure the configured buffer is larger than expected one-block interest accrual and normal oracle quote precision differences.

### Proof of Concept
The following Hardhat test follows the existing `Pool.test.ts` fixture structure. Set a nonzero interest rate before issuance, borrow the full issuable amount, advance one second, and liquidate:

```ts
import {time} from '@nomicfoundation/hardhat-network-helpers'
import {parseEther} from '@ethersproject/units'
import {expect} from 'chai'

it('liquidates a position that borrowed the exact limit', async function () {
  // Fixture setup: alice has deposited msdMET collateral.
  // deployer is the mocked pool governor.
  await msEthDebtToken.updateInterestRate(parseEther('0.10')) // 10% APR

  // Liquidator obtains msETH through its own collateralized position.
  const {_issuableInUsd: liquidatorIssuable} = await pool.debtPositionOf(liquidator.address)
  const liquidatorIssue = await masterOracle.quoteUsdToToken(msEth.address, liquidatorIssuable)
  await msEthDebtToken.connect(liquidator).issue(liquidatorIssue, liquidator.address)

  // Alice borrows exactly the protocol-permitted maximum.
  const {_issuableInUsd} = await pool.debtPositionOf(alice.address)
  const amountToIssue = await masterOracle.quoteUsdToToken(msEth.address, _issuableInUsd)
  await msEthDebtToken.connect(alice).issue(amountToIssue, alice.address)

  let position = await pool.debtPositionOf(alice.address)
  expect(position._isHealthy).to.eq(true)
  expect(position._debtInUsd).to.eq(position._issuableLimitInUsd)

  // The next interest accrual pushes the debt over the same boundary.
  await time.increase(1)

  position = await pool.debtPositionOf(alice.address)
  expect(position._isHealthy).to.eq(false)

  const amountToRepay = position._debtInUsd // bounded below maxLiquidable if needed
  await expect(
    pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)
  ).to.emit(pool, 'PositionLiquidated')
})
```

In a full test, cap `amountToRepay` to `quoteLiquidateMax()` if it exceeds `maxLiquidable`; partial repayment still demonstrates seizure of collateral immediately after the max-limit borrow.

### Citations

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

**File:** contracts/DebtToken.sol (L248-263)
```text
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

**File:** contracts/Pool.sol (L262-265)
```text
        _debtInUsd = debtOf(account_);
        (_depositInUsd, _issuableLimitInUsd) = depositOf(account_);
        _isHealthy = _debtInUsd <= _issuableLimitInUsd;
        _issuableInUsd = _debtInUsd < _issuableLimitInUsd ? _issuableLimitInUsd - _debtInUsd : 0;
```

**File:** contracts/Pool.sol (L456-471)
```text
        _toLiquidator = masterOracle().quote(
            address(syntheticToken_),
            address(depositToken_.underlying()),
            amountToRepay_
        );

        (uint128 _liquidatorIncentive, uint128 _protocolFee) = feeProvider.liquidationFees();

        if (_protocolFee > 0) {
            _fee = _toLiquidator.wadMul(_protocolFee);
        }
        if (_liquidatorIncentive > 0) {
            _toLiquidator += _toLiquidator.wadMul(_liquidatorIncentive);
        }

        _totalToSeize = _fee + _toLiquidator;
```

**File:** contracts/Pool.sol (L556-563)
```text
        IDebtToken _debtToken = debtTokenOf[syntheticToken_];
        _debtToken.accrueInterest();

        (bool _isHealthy, , , , ) = debtPositionOf(account_);

        if (_isHealthy) {
            revert PositionIsHealthy();
        }
```

**File:** contracts/Pool.sol (L565-589)
```text
        uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }

        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }

        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }

        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);
```
