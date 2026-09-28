### Title
Missing deadline check in SmartFarmingManager swap/deposit functions allows stale user operations to execute at unfavorable conditions - ([File: contracts/SmartFarmingManager.sol](contracts/SmartFarmingManager.sol))

### Summary
`SmartFarmingManager.leverage()` and `SmartFarmingManager.flashRepay()` perform oracle-priced and external-swapper swaps on the user's behalf, and the cross-chain variants (`crossChainLeverage`, `crossChainFlashRepay`, `crossChainLeverageCallback`, `crossChainFlashRepayCallback`, plus their `retry*` functions) execute those swaps at an arbitrary later time on the destination chain. None of these functions accept a `deadline` parameter, so a pending transaction or delayed/retried LayerZero message can execute long after the user signed it, when market conditions no longer reflect what the user agreed to.

### Finding Description
The bug class from the external report is: user-initiated functions that perform swaps (or swap-equivalent operations) without a deadline, so a transaction that sits pending can be executed at unexpected times under unfavorable market conditions.

The analog exists in Metronome's strongest reachable surface for this class, the leverage/zap path:

- `SmartFarmingManager.leverage()` (contracts/SmartFarmingManager.sol:155-169) pulls `tokenIn_`, mints synthetic debt, and calls the external `swapper()` to swap minted synth into collateral, with only `depositAmountMin_` as protection.
- `SmartFarmingManager.flashRepay()` (contracts/SmartFarmingManager.sol:98-110) flash-withdraws user collateral and calls `_swap(swapper(), collateral, synth, _withdrawn, 0)` at line 125 with only `swapAmountOutMin_` as protection.
- On the cross-chain deployments (deployments/swell, optimism, base, hemi), `crossChainLeverage`/`crossChainFlashRepay` store the user's request (`CrossChainLeverage`/`CrossChainFlashRepay` structs in `SmartFarmingManagerStorageV1`) and the actual swap/deposit happens later in `crossChainLeverageCallback` / `crossChainFlashRepayCallback`, and potentially much later still via the permissionless `retryCrossChainLeverageCallback` / `retryCrossChainFlashRepayCallback` functions. No deadline is encoded in the request or enforced in the callback.

The user's slippage parameters (`depositAmountMin_`, `swapAmountOutMin_`, `repayAmountMin_`, `bridgeTokenAmountMin_`) are fixed at signing time and never expire. A request signed when prices were favorable can therefore be executed days later via a retry, still satisfying the stale min-out bound while delivering far less value than the user intended at signing time — exactly the "pending transaction maliciously executed in the future" scenario from the source report, amplified because cross-chain latency and retries are built into the design.

### Impact Explanation
- A user's leverage or flash-repay operation (single-chain or cross-chain) can execute at a time when pool prices, oracle quotes, or swapper liquidity are materially worse, as long as the stale slippage floor is met.
- The user loses the difference between the fair execution at signing time and the actual execution — direct loss of user funds (worse collateral deposited, more collateral withdrawn for the same debt repaid, or a worse resulting position up to the `PositionIsNotHealthy` boundary).
- Any unprivileged account can trigger `retryCrossChainLeverageCallback`/`retryCrossChainFlashRepayCallback` for a stored request, so execution timing of the stale order is not under the user's control.
- No privileged role, oracle fault, or governance action is required; the loss is bounded only by the user's own (time-insensitive) slippage parameter.

### Likelihood Explanation
- Single-chain path: an EOA's `leverage`/`flashRepay` tx can sit in a congested mempool and be mined later; common under gas spikes. Medium likelihood, small-to-moderate loss per occurrence.
- Cross-chain path: callbacks and permissionless retries are designed to execute at a delayed, unpredictable time; slippage-driven failures that require `retry*` calls (documented in `docs/cross-chain.md`) make delayed execution a routine occurrence rather than an edge case.
- Severity is bounded by user-chosen slippage parameters and the post-operation health check, which is consistent with a Medium severity as in the source report.

### Recommendation
Add a `deadline` parameter to `leverage`, `flashRepay`, `crossChainLeverage`, and `crossChainFlashRepay`, revert with e.g. `if (block.timestamp > deadline_) revert Expired()`. For the cross-chain flow, store the deadline in `CrossChainLeverage`/`CrossChainFlashRepay` at request creation and enforce it in `crossChainLeverageCallback`, `crossChainFlashRepayCallback`, and the `retry*` functions, so stale requests cannot be replayed after the user's intent has expired.

### Proof of Concept
Foundry fork sketch against the single-chain deployment (the same shape applies to the cross-chain callbacks):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {ISmartFarmingManager} from "../contracts/interfaces/ISmartFarmingManager.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract MissingDeadlinePoC is Test {
    ISmartFarmingManager sfm = ISmartFarmingManager(/* deployed SmartFarmingManager */);
    IERC20 vaUSDC = IERC20(/* collateral tokenIn */);
    IDepositToken msdVaUSDC = IDepositToken(/* deposit token */);
    ISyntheticToken msUSD = ISyntheticToken(/* synthetic token */);
    address alice = /* user */;

    function test_staleLeverageExecutesWithoutDeadline() public {
        vm.startPrank(alice);
        vaUSDC.approve(address(sfm), type(uint256).max);

        // User signs leverage with a slippage bound computed from *current* prices.
        uint256 amountIn = 100e18;
        uint256 depositAmountMin = /* quote at signing time minus slippage */;

        // Tx sits pending; price of collateral vs msUSD moves against the user
        // but remains above depositAmountMin.
        vm.warp(block.timestamp + 2 days);
        // simulate adverse market move via AMM trade / oracle price update on fork

        // The pending tx still executes: there is no timestamp check anywhere
        // in leverage() -> _swap() path.
        sfm.leverage(vaUSDC, msdVaUSDC, msUSD, amountIn, 1.5e18, depositAmountMin);
        // Result: position opened at a rate the user would have rejected at
        // execution time; loss bounded only by stale depositAmountMin.
        vm.stopPrank();
    }
}
```

The identical sequence works for `flashRepay`, and cross-chain: create a `crossChainLeverage` request, advance time / move the price, then call `crossChainLeverageCallback` (or `retryCrossChainLeverageCallback`) — execution succeeds against the stored `depositAmountMin` with no expiry check. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** contracts/SmartFarmingManager.sol (L98-110)
```text
    function flashRepay(
        ISyntheticToken syntheticToken_,
        IDepositToken depositToken_,
        uint256 withdrawAmount_,
        uint256 swapAmountOutMin_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfDepositTokenExists(depositToken_)
        onlyIfSyntheticTokenExists(syntheticToken_)
        returns (uint256 _withdrawn, uint256 _repaid)
```

**File:** contracts/SmartFarmingManager.sol (L124-141)
```text
        // 2. swap it for synth
        uint256 _swapAmountOut = _swap(swapper(), _collateralOf(depositToken_), _syntheticToken, _withdrawn, 0);
        if (_swapAmountOut < swapAmountOutMin_) revert FlashRepaySlippageTooHigh();

        (uint256 _maxRepayAmount, ) = _debtToken.quoteRepayIn(_debtToken.balanceOf(_msgSender));
        uint256 _amountToRepay = Math.min(_swapAmountOut, _maxRepayAmount);

        // 3. repay debt
        (_repaid, ) = _debtToken.repay(_msgSender, _amountToRepay);

        // 4. refund synthetic token in excess
        if (_swapAmountOut > _amountToRepay) {
            _syntheticToken.safeTransfer(_msgSender, _swapAmountOut - _amountToRepay);
        }

        // 5. check the health of the outcome position
        (bool _isHealthy, , , , ) = _pool.debtPositionOf(_msgSender);
        if (!_isHealthy) revert PositionIsNotHealthy();
```

**File:** contracts/SmartFarmingManager.sol (L155-169)
```text
    function leverage(
        IERC20 tokenIn_,
        IDepositToken depositToken_,
        ISyntheticToken syntheticToken_,
        uint256 amountIn_,
        uint256 leverage_,
        uint256 depositAmountMin_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfDepositTokenExists(depositToken_)
        onlyIfSyntheticTokenExists(syntheticToken_)
        returns (uint256 _deposited, uint256 _issued)
```

**File:** contracts/interfaces/ISmartFarmingManager.sol (L12-28)
```text
interface ISmartFarmingManager {
    function flashRepay(
        ISyntheticToken syntheticToken_,
        IDepositToken depositToken_,
        uint256 withdrawAmount_,
        uint256 repayAmountMin_
    ) external returns (uint256 _withdrawn, uint256 _repaid);

    function leverage(
        IERC20 tokenIn_,
        IDepositToken depositToken_,
        ISyntheticToken syntheticToken_,
        uint256 amountIn_,
        uint256 leverage_,
        uint256 depositAmountMin_
    ) external returns (uint256 _deposited, uint256 _issued);
}
```
