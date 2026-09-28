### Title
Users pay interest on the issuance fee portion of synth debt they never received - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
`DebtToken.issue` mints debt principal equal to the gross `amount_`, but only mints `amount_ - fee` synthetic tokens to the borrower; the `fee` is minted to `pool.feeCollector()`. The user's debt position (and therefore all future interest accrual via `debtIndex`) is computed on the gross amount, including the fee that went to the protocol.

### Finding Description
In `issue()`, `_mint(_pool, _masterOracle, _msgSender, amount_)` records the full `amount_` as the user's principal (`principalOf[account_] = _balanceBefore + amount_`), while `quoteIssueOut(amount_)` splits `amount_` into `_issued` (minted to `to_`) and `_fee` (minted to `pool.feeCollector()`). [1](#0-0) [2](#0-1) [3](#0-2) 

Interest then accrues on the full recorded principal because `balanceOf` returns `principalOf[account_] * _debtIndex / debtIndexOf[account_]` and `_calculateInterestAccrual` grows `debtIndex` against `totalSupply_`, which was also increased by the gross `amount_`. [4](#0-3) [5](#0-4) 

Example: `issueFee = 10%`, user calls `issue(100e18, user)`. User receives `90e18` synth, feeCollector receives `10e18`, but `principalOf[user] = 100e18` and interest accrues on `100e18`. Repayment also works on the gross debt: `repayAll` burns `balanceOf` = 100-based principal plus a `repayFee` on top, so the user both pays interest on and must repay the fee they never received. [6](#0-5) 

The same pattern exists in `flashIssue` used by `SmartFarmingManager` leverage paths, where the gross `amount_` drives the synth split; the corresponding debt minted via `DebtToken.mint` in the leverage flow is also on the gross amount. [7](#0-6) 

### Impact Explanation
Every borrower pays interest on, and must repay, the portion of issued synth that was diverted to `feeCollector`. With `issueFee = f` and APR `r`, a borrower effectively pays `r * f` of the loan per year extra, and their outstanding principal is `f` higher than the value they received — a direct, unavoidable loss of user funds proportional to fee size and loan duration, and it also worsens the borrower's health factor / liquidation bound since `debtOf` is inflated by the fee amount.

### Likelihood Explanation
Any non-zero `issueFee` configuration triggers it on every `issue` call and every `SmartFarmingManager.leverage`/`flashIssue` flow, reachable by any unprivileged user via `DebtToken.issue` (only `whenNotShutdown`/`onlyIfSyntheticTokenExists` checks) with no privileged action required. The loss is continuous and automatic — no oracle manipulation or timing needed.

### Recommendation
Record debt only for what the user actually owes for the value received. Either:
- Mint debt principal of `amount_` but define `amount_` as the net received amount (use `quoteIssueIn` semantics so the gross is user-supplied only for fee calculation), or
- In `issue`, call `_mint(_pool, _masterOracle, _msgSender, _issued)` so `principalOf` and `totalSupply_` track the net issued synth, and keep the fee as a one-time charge rather than an interest-bearing balance.

Alternatively, document that the fee is a borrowed-and-fee-paid model; but the economically consistent fix is to exclude the fee from the interest-bearing principal.

### Proof of Concept
Hardhat-style reproduction against the repo's own test scaffolding:

```ts
// test/DebtToken-fee-interest.test.ts
import {ethers} from 'hardhat'
import {parseEther} from 'ethers/lib/utils'
import {time} from '@nomicfoundation/hardhat-network-helpers'
import {expect} from 'chai'

it('interest accrues on the fee portion the user never received', async () => {
  // setup: user deposits collateral (reuse existing fixture: met -> msdMET)
  const depositAmount = parseEther('5000')
  await met.mint(user1.address, depositAmount)
  await met.connect(user1).approve(msdMET.address, ethers.constants.MaxUint256)
  await msdMET.connect(user1).deposit(depositAmount, user1.address)

  // set 10% issue fee and 10% APR
  await feeProvider.updateIssueFee(parseEther('0.1'))
  await msUSDDebt.updateInterestRate(parseEther('0.1'))

  // user issues 100 msUSD gross; receives 90, feeCollector receives 10
  await msUSDDebt.connect(user1).issue(parseEther('100'), user1.address)
  expect(await msUSD.balanceOf(user1.address)).eq(parseEther('90'))
  expect(await msUSD.balanceOf(feeCollector.address)).eq(parseEther('10'))

  // BUG: principal recorded is 100, not 90
  expect(await msUSDDebt.principalOf(user1.address)).eq(parseEther('100'))

  // after 1 year, debt ~= 110 even though user only ever received 90
  await time.increase(SECONDS_PER_YEAR)
  await msUSDDebt.accrueInterest()
  const debt = await msUSDDebt.balanceOf(user1.address)
  expect(debt).closeTo(parseEther('110'), parseEther('0.01'))
  // user pays ~110 to close a position for which they received only 90
})
```

Note: this mirrors the Sherlock Isomorph finding verbatim — the gross-vs-net accounting of `issue`/`_mint` in `contracts/DebtToken.sol` is the direct analog of `Vault_Lyra.openLoan`/`_increaseLoan`.

### Citations

**File:** contracts/DebtToken.sol (L196-206)
```text
    function balanceOf(address account_) public view override returns (uint256) {
        uint256 _principal = principalOf[account_];
        if (_principal == 0) {
            return 0;
        }

        (, uint256 _debtIndex, ) = _calculateInterestAccrual();

        // Note: The `debtIndex / debtIndexOf` gives the interest to apply to the principal amount
        return (_principal * _debtIndex) / debtIndexOf[account_];
    }
```

**File:** contracts/DebtToken.sol (L262-270)
```text
        _mint(_pool, _masterOracle, _msgSender, amount_);

        (_issued, _fee) = quoteIssueOut(amount_);
        if (_fee > 0) {
            _syntheticToken.mint(_pool.feeCollector(), _fee);
        }
        _syntheticToken.mint(to_, _issued);

        emit SyntheticTokenIssued(_msgSender, to_, amount_, _issued, _fee);
```

**File:** contracts/DebtToken.sol (L300-305)
```text
        (_issued, _fee) = quoteIssueOut(amount_);
        if (_fee > 0) {
            _syntheticToken.mint(pool.feeCollector(), _fee);
        }
        _syntheticToken.mint(to_, _issued);
    }
```

**File:** contracts/DebtToken.sol (L369-377)
```text
    function quoteIssueOut(uint256 amount_) public view override returns (uint256 _amountToIssue, uint256 _fee) {
        uint256 _issueFee = pool.feeProvider().issueFee();
        if (_issueFee == 0) {
            return (amount_, _fee);
        }

        _fee = amount_.wadMul(_issueFee);
        _amountToIssue = amount_ - _fee;
    }
```

**File:** contracts/DebtToken.sol (L478-492)
```text
        _repaid = balanceOf(onBehalfOf_);
        if (_repaid == 0) revert AmountIsZero();

        address _msgSender = _msgSender();
        ISyntheticToken _syntheticToken = syntheticToken;

        uint256 _amount;
        (_amount, _fee) = quoteRepayIn(_repaid);

        if (_fee > 0) {
            _syntheticToken.seize(_msgSender, pool.feeCollector(), _fee);
        }

        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);
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

**File:** contracts/DebtToken.sol (L590-595)
```text
        totalSupply_ += amount_;
        if (totalSupply_ > maxTotalSupply) revert SurpassMaxDebtSupply();

        principalOf[account_] = _balanceBefore + amount_;
        debtIndexOf[account_] = debtIndex;
        emit Transfer(address(0), account_, amount_);
```
