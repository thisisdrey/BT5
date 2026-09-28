### Title
Users can deposit collateral without paying deposit fees by splitting into dust amounts - ([File: contracts/DepositToken.sol])

### Summary
`DepositToken.deposit` computes the protocol fee as `amount_.wadMul(_depositFee)` inside `quoteDepositOut`. `wadMul` returns 0 whenever `amount_ * _depositFee < 0.5e18`, so an unprivileged user can call `deposit` repeatedly with tiny `amount_` values and have the fee truncates to zero while still receiving full deposit-token credit — the same fee-evasion-via-rounding class as the Allo `fundPool` finding.

### Finding Description
`deposit` pulls `amount_` of underlying to the treasury, then quotes `(_deposited, _fee) = quoteDepositOut(amount_)` and mints `_fee` to `pool.feeCollector()` only if `_fee > 0` [1](#0-0) . The quote is:

```solidity
_fee = amount_.wadMul(_depositFee);
_amountToDeposit = amount_ - _fee;
``` [2](#0-1) 

`WadRayMath.wadMul` computes `(a * b + HALF_WAD) / WAD`, i.e. rounds half up [3](#0-2) . Therefore `_fee == 0` whenever `amount_ * _depositFee < 5e17`. With `depositFee = 0.1e18` (10%), any `amount_ <= 4` wei pays zero fee; with `depositFee = 0.01e18` (1%), any `amount_ <= 49` wei pays zero fee. `deposit` only reverts on `amount_ == 0` [4](#0-3) , so dust deposits are fully accepted and mint `msdTOKEN` 1:1 with no fee.

The identical pattern exists in:
- `quoteWithdrawOut` / `withdraw` — withdraw fee rounds to zero on dust withdrawals (`_fee = amount_.wadMul(_withdrawFee)`) [5](#0-4) 
- `quoteSwapOut` / `swap` — swap fee rounds to zero on dust swaps (`_fee = _amountOut.wadMul(_swapFee)`) [6](#0-5) 
- `quoteLiquidateOut` — protocol liquidation fee rounds to zero on dust repays (`_fee = _toLiquidator.wadMul(_protocolFee)`) [7](#0-6) 

### Impact Explanation
The fee-collection invariant ("every deposit/withdraw/swap pays `X%` to the fee collector") breaks: a user routing all volume through dust-sized calls pays zero fees, causing loss of protocol revenue proportional to total volume. However, because `wadMul` rounds half-up rather than down, the zero-fee threshold is `< 0.5/fee` wei of the token — at most ~50 wei for a 1% fee — so the fee avoided per call is bounded by half a wei. Evading fees on economically meaningful volume requires a number of transactions that makes gas cost dwarf the saved fees on any realistic chain.

### Likelihood Explanation
Low. The call is reachable by any EOA via `deposit(amount_, onBehalfOf_)` with a dust amount, and batching through `Operator.execute` does not make it profitable because each inner call still performs an ERC20 `safeTransferFrom` (~30k+ gas) to save less than 1 wei of underlying. The rounding can also work against the user (amounts just above the threshold round the fee up). Exploitation is technically reproducible but economically irrational at wad (1e18) precision.

### Recommendation
Enforce a minimum effective fee: either revert when the computed fee rounds to zero while the configured fee is non-zero, or enforce a minimum `amount_` per deposit/withdraw/swap such that `amount_ * fee >= 0.5e18` (e.g., `if (_fee == 0 && _depositFee > 0) revert AmountIsTooLow()`).

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {DepositToken} from "../contracts/DepositToken.sol";
import {Pool} from "../contracts/Pool.sol";
import {FeeProvider} from "../contracts/FeeProvider.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

contract DustFeeEvasionTest is Test {
    // assumes standard test harness: pool, msdVaUSDC (DepositToken), feeProvider, alice with underlying

    function test_depositDustPaysNoFee() external {
        // given: 1% deposit fee
        feeProvider.updateDepositFee(0.01e18);
        address feeCollector = pool.feeCollector();

        uint256 dust = 49; // 49 * 0.01e18 = 4.9e17 < HALF_WAD => wadMul == 0
        IERC20 underlying = msdVaUSDC.underlying();
        deal(address(underlying), alice, dust * 1000);

        vm.startPrank(alice);
        underlying.approve(address(msdVaUSDC), type(uint256).max);

        // when: deposit 1000 dust chunks
        for (uint256 i; i < 1000; ++i) {
            (uint256 deposited, uint256 fee) = msdVaUSDC.deposit(dust, alice);
            assertEq(fee, 0);              // no fee charged
            assertEq(deposited, dust);     // full credit
        }
        vm.stopPrank();

        // then: 49,000 wei deposited, zero fees accrued
        assertEq(msdVaUSDC.balanceOf(feeCollector), 0);
        // vs. a single deposit of 49_000 wei which would pay ~490 wei fee
        (uint256 expectedDeposit, uint256 expectedFee) = msdVaUSDC.quoteDepositOut(49_000);
        assertGt(expectedFee, 0);
        assertLt(expectedDeposit, 49_000);
    }
}
```

Note: given the sub-wei savings per call and per-call gas/transfer costs, this analog is economically weaker than the original Allo finding (which used floor division on whole-token units). If the strict impact bar requires profit exceeding cost, this does not meet it; the code pattern, however, is a direct match for the bug class.

### Citations

**File:** contracts/DepositToken.sol (L215-216)
```text
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();
```

**File:** contracts/DepositToken.sol (L225-234)
```text
        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);
```

**File:** contracts/DepositToken.sol (L294-302)
```text
    function quoteDepositOut(uint256 amount_) public view override returns (uint256 _amountToDeposit, uint256 _fee) {
        uint256 _depositFee = pool.feeProvider().depositFee();
        if (_depositFee == 0) {
            return (amount_, _fee);
        }

        _fee = amount_.wadMul(_depositFee);
        _amountToDeposit = amount_ - _fee;
    }
```

**File:** contracts/DepositToken.sol (L326-334)
```text
    function quoteWithdrawOut(uint256 amount_) public view override returns (uint256 _amountToWithdraw, uint256 _fee) {
        uint256 _withdrawFee = pool.feeProvider().withdrawFee();
        if (_withdrawFee == 0) {
            return (amount_, _fee);
        }

        _fee = amount_.wadMul(_withdrawFee);
        _amountToWithdraw = amount_ - _fee;
    }
```

**File:** contracts/lib/WadRayMath.sol (L25-31)
```text
    function wadMul(uint256 a, uint256 b) internal pure returns (uint256) {
        if (a == 0 || b == 0) {
            return 0;
        }

        return (a * b + HALF_WAD) / WAD;
    }
```

**File:** contracts/Pool.sol (L462-466)
```text
        (uint128 _liquidatorIncentive, uint128 _protocolFee) = feeProvider.liquidationFees();

        if (_protocolFee > 0) {
            _fee = _toLiquidator.wadMul(_protocolFee);
        }
```

**File:** contracts/Pool.sol (L508-525)
```text
    function quoteSwapOut(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    ) public view override returns (uint256 _amountOut, uint256 _fee) {
        _amountOut = _poolRegistry.masterOracle().quote(
            address(syntheticTokenIn_),
            address(syntheticTokenOut_),
            amountIn_
        );

        uint256 _swapFee = feeProvider.swapFees(address(syntheticTokenIn_), address(syntheticTokenOut_));

        if (_swapFee > 0) {
            _fee = _amountOut.wadMul(_swapFee);
            _amountOut -= _fee;
        }
    }
```
