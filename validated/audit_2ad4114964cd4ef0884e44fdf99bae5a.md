### Title
Unbounded `repayAll` lets a borrower front-run a third-party repayment and extract the payer’s synthetic tokens - (File: `contracts/DebtToken.sol`)

### Summary
`DebtToken.repayAll` accepts only a beneficiary and dynamically burns synthetic tokens equal to the beneficiary’s entire current debt. Because `DebtToken.issue` lets a borrower increase that debt in a preceding transaction, the borrower can front-run a pending `repayAll(onBehalfOf_)`, mint additional synthetic tokens to themselves, and cause the payer to burn enough synthetic tokens to cover the enlarged debt.

### Finding Description
`repayAll` accrues interest, reads `balanceOf(onBehalfOf_)` at execution time, and burns that full amount from the caller rather than accepting a maximum repayment or caller-supplied amount. [1](#0-0) 

A borrower can front-run the transaction with `issue(amount_, borrower)`, which validates only the borrower’s remaining issuable collateral, mints debt to the borrower, and mints the resulting synthetic tokens to `to_`. [2](#0-1) 

The payer’s pending `repayAll(borrower)` then sees the increased debt and burns the increased amount from `_msgSender`; any configured repayment fee is also seized from the payer. [3](#0-2) 

Unlike Compound’s `uint256(-1)` allowance-based repayment pattern, Metronome does not need token approval: `SyntheticToken.burn` is privileged to the debt-token contract, and `DebtToken` passes the external payer as the burn source. [4](#0-3) [5](#0-4) 

### Impact Explanation
An unprivileged borrower can force a third-party payer to repay debt newly created after the payer signed the repayment transaction, while retaining the synthetic tokens minted by that new borrowing. The payer’s loss is bounded by their synthetic-token balance and the borrower’s available borrowing capacity, but can equal their entire balance if the borrower has sufficient collateral headroom.

This is direct extraction of the payer’s synthetic-token funds in exchange for debt reduction benefiting the attacker. If the payer does not have enough synthetic tokens for the enlarged debt, the repayment reverts instead, so this does not create bad debt by itself.

### Likelihood Explanation
The exploit requires a pending third-party `repayAll` transaction, a mempool/front-running opportunity, a borrower with remaining issuable debt capacity, and a payer holding enough synthetic tokens to cover the increased balance. Those are reachable public conditions: `issue` is externally callable by the borrower, and `repayAll` is externally callable by any payer for any beneficiary. The `whenNotShutdown`, `nonReentrant`, and synthetic-token-existence checks do not prevent separate, sequentially executed transactions.

### Recommendation
Add a caller-controlled maximum gross repayment amount to `repayAll`, for example `repayAll(address onBehalfOf_, uint256 maxAmount_)`. Compute the repayable debt as `min(balanceOf(onBehalfOf_), quoteRepayOut(maxAmount_).amountToRepay)`, or revert if the required gross amount exceeds `maxAmount_`, depending on the intended semantics.

At minimum, users should use the bounded `repay(onBehalfOf_, amount_)` path when paying another account, because its `amount_` parameter limits how much of the payer’s synthetic-token balance can be consumed. [6](#0-5) 

### Proof of Concept
A Foundry fork test can reproduce the issue using the deployed pool, debt token, and synthetic token:

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";

interface IERC20Like {
    function balanceOf(address) external view returns (uint256);
}

interface IDebtTokenLike is IERC20Like {
    function issue(uint256 amount, address to) external returns (uint256, uint256);
    function repayAll(address onBehalfOf) external returns (uint256, uint256);
    function syntheticToken() external view returns (IERC20Like);
}

contract RepayAllFrontRunPoC is Test {
    IDebtTokenLike debtToken = IDebtTokenLike(DEPLOYED_DEBT_TOKEN);

    function testBorrowerFrontRunsRepayAll() public {
        address borrower = BORROWER_WITH_COLLATERAL_HEADROOM;
        address payer = PAYER_HOLDING_SYNTHETIC_TOKENS;

        IERC20Like synth = debtToken.syntheticToken();

        uint256 payerBefore = synth.balanceOf(borrower);
        uint256 payerSynthBefore = synth.balanceOf(payer);
        uint256 borrowerDebtBefore = debtToken.balanceOf(borrower);

        // Victim broadcasts:
        // debtToken.repayAll(borrower)

        // Borrower observes it and front-runs with additional issuance.
        uint256 extraDebt = EXTRA_BORROWING_WITHIN_ISSUABLE_LIMIT;
        vm.prank(borrower);
        debtToken.issue(extraDebt, borrower);

        assertGt(debtToken.balanceOf(borrower), borrowerDebtBefore);
        assertEq(synth.balanceOf(borrower), payerBefore + extraDebt);

        // The victim's already-signed repayAll now burns enough synth to
        // cover both the old debt and the newly issued debt.
        vm.prank(payer);
        debtToken.repayAll(borrower);

        assertEq(debtToken.balanceOf(borrower), 0);
        assertEq(synth.balanceOf(borrower), payerBefore + extraDebt);
        assertLt(synth.balanceOf(payer), payerSynthBefore - borrowerDebtBefore);
    }
}
```

The critical behavior is reproduced by `balanceOf(onBehalfOf_)` being evaluated after the attacker’s front-running `issue`, followed by burning the resulting full balance from the unrelated payer. [7](#0-6)

### Citations

**File:** contracts/DebtToken.sol (L235-270)
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
        if (_fee > 0) {
            _syntheticToken.mint(_pool.feeCollector(), _fee);
        }
        _syntheticToken.mint(to_, _issued);

        emit SyntheticTokenIssued(_msgSender, to_, amount_, _issued, _fee);
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

**File:** contracts/DebtToken.sol (L466-492)
```text
    function repayAll(
        address onBehalfOf_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        returns (uint256 _repaid, uint256 _fee)
    {
        accrueInterest();

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

**File:** contracts/SyntheticToken.sol (L83-90)
```text
    modifier onlyIfCanBurn() {
        address _msgSender = _msgSender();
        if (
            !_isMsgSenderProxyOFT(_msgSender) &&
            !_isMsgSenderAmo(_msgSender) &&
            !_isMsgSenderPool(_msgSender) &&
            !_isMsgSenderDebtToken(_msgSender)
        ) revert SenderCanNotBurn();
```

**File:** contracts/SyntheticToken.sol (L187-189)
```text
    function burn(address from_, uint256 amount_) external override onlyIfCanBurn {
        _burn(from_, amount_);
    }
```
