### Title
Zero-supply reward periods advance the reward timestamp without accruing emissions - (File: `contracts/RewardsDistributor.sol`)

### Summary

`RewardsDistributor._calculateTokenIndex()` treats a zero token supply as a zero reward ratio, but still advances `tokenStates[token_].timestamp` to the current timestamp. This permanently discards `deltaTimestamp * tokenSpeed` emissions for the period in which a rewarded `DepositToken` or `DebtToken` had zero supply. The result is analogous to the reported stale-update reward loss: emissions continue according to the configured speed, but the index never records them.

### Finding Description

Every mint or burn calls `updateBeforeMintOrBurn()` before the balance and supply are changed. `DepositToken._burn()` and `DepositToken._mint()` both invoke that hook before mutating `totalSupply`, while `DebtToken` uses the same pre-mint/pre-burn reward hook. [1](#0-0) [2](#0-1) 

When the last holder withdraws, the hook correctly accrues rewards through the pre-burn timestamp. If the next holder deposits later, the mint hook calls `_updateTokenIndex()` while `token_.totalSupply()` is still zero. [3](#0-2) [4](#0-3) 

Inside `_calculateTokenIndex()`, nonzero speed produces `tokensAccrued = deltaTimestamp * speed`, but `ratio` is forced to zero whenever `totalSupply == 0`. Nevertheless, `_newTimestamp` is still set to `block.timestamp`. [5](#0-4) 

`_updateTokenIndex()` persists the unchanged index together with the advanced timestamp. The elapsed zero-supply interval is therefore consumed without adding its emissions to the index or any queue. [6](#0-5) 

A concrete sequence is:

1. Alice owns all rewarded deposit tokens.
2. Alice calls `DepositToken.withdraw()`, reducing supply to zero.
3. Time passes while `tokenSpeeds[token] > 0`.
4. Bob calls `DepositToken.deposit()`.
5. Before Bob is minted tokens, the reward hook observes zero supply, sets `ratio = 0`, and moves the timestamp forward.
6. Rewards for the entire empty-supply interval are never allocated to Alice, Bob, or a later claimant.

### Impact Explanation

The configured stream `speed * elapsedTime` is not distributed for zero-supply periods. Those rewards remain unclaimable as accrued yield even though the schedule has advanced past them. If the distributor lacks another mechanism to return those tokens to the emission schedule, the funds are effectively stranded and subsequent users permanently lose that portion of the reward stream. The flaw does not require privileged access: an ordinary holder can create the condition by fully withdrawing or repaying, after which any later public deposit or borrow consumes the skipped interval.

### Likelihood Explanation

The issue requires an active rewarded token with `tokenSpeed > 0` and a temporary transition to zero supply. Full exits are normal user behavior, particularly for deposits and debt repayment. The next ordinary supply-changing operation automatically advances the timestamp before the new supply exists, so no special timing, oracle manipulation, privileged role, or malicious contract is required.

### Recommendation

Do not simply discard elapsed rewards when `totalSupply == 0`. Track the emissions generated during the empty-supply interval in a pending or queued-rewards field and fold them into the index when supply returns, or otherwise explicitly account for them in the reward schedule. For example, calculate `pendingRewards += deltaTimestamp * speed` when supply is zero, then distribute the queued amount through the index after supply becomes nonzero. Ensure that restarting rewards does not allow the first depositor to retroactively claim emissions using a balance that did not exist during the queued interval.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";

interface IRewardsDistributorFork {
    function tokenSpeeds(address token) external view returns (uint256);
    function tokenStates(address token)
        external
        view
        returns (uint224 index, uint32 timestamp);
    function tokensAccruedOf(address account) external view returns (uint256);
    function updateBeforeMintOrBurn(address token, address account) external;
}

interface IDepositTokenFork {
    function totalSupply() external view returns (uint256);
    function balanceOf(address account) external view returns (uint256);
    function deposit(uint256 amount, address onBehalfOf) external returns (uint256, uint256);
    function withdraw(uint256 amount, address to) external returns (uint256, uint256);
}

contract ZeroSupplyRewardsTest is Test {
    function test_zeroSupplyPeriodIsSkipped() public {
        // Fork a deployment containing a pool, RewardsDistributor, and a
        // rewarded DepositToken.
        vm.createSelectFork(vm.envString("MAINNET_RPC_URL"));

        IRewardsDistributorFork distributor =
            IRewardsDistributorFork(0x9585D3706758b251E37541D808e3Ea11EAd5b819);

        // Resolve or select any deployed DepositToken where:
        //   distributor.tokenSpeeds(depositToken) > 0
        IDepositTokenFork depositToken = IDepositTokenFork(REWARDED_DEPOSIT_TOKEN);

        uint256 speed = distributor.tokenSpeeds(address(depositToken));
        assertGt(speed, 0);

        address alice = ALICE; // Sole deposit-token holder.
        address bob = BOB;     // Funded with the underlying collateral.

        uint256 aliceShares = depositToken.balanceOf(alice);
        assertEq(depositToken.totalSupply(), aliceShares);

        // Alice exits completely. The pre-burn hook still sees nonzero supply
        // and settles rewards through this timestamp.
        vm.prank(alice);
        depositToken.withdraw(aliceShares, alice);
        assertEq(depositToken.totalSupply(), 0);

        (uint224 indexAfterAlice, uint32 timestampAfterAlice) =
            distributor.tokenStates(address(depositToken));

        // Rewards continue at `speed`, but no deposit supply exists.
        vm.warp(block.timestamp + 100);

        // Bob's deposit calls updateBeforeMintOrBurn() before minting. The hook
        // observes totalSupply == 0, sets ratio = 0, and stores timestamp = now.
        vm.prank(bob);
        depositToken.deposit(UNDERLYING_AMOUNT, bob);

        (uint224 indexAfterBob, uint32 timestampAfterBob) =
            distributor.tokenStates(address(depositToken));

        assertEq(indexAfterBob, indexAfterAlice);
        assertEq(timestampAfterBob, uint32(block.timestamp));

        // Let Bob accrue for another 100 seconds.
        vm.warp(block.timestamp + 100);
        distributor.updateBeforeMintOrBurn(address(depositToken), bob);

        uint256 bobAccrued = distributor.tokensAccruedOf(bob);

        // Bob only accrued the final 100 seconds. The 100-second zero-supply
        // period was consumed without being represented in the index.
        assertApproxEqAbs(bobAccrued, 100 * speed, 1);

        // Expected scheduled emissions over Bob's measured interval plus the
        // discarded zero-supply interval would have been 200 * speed.
        assertLt(bobAccrued, 200 * speed);
    }
}
```

The decisive state transition is `index unchanged && timestamp advanced` across Bob's deposit, proving that `100 * speed` emissions were skipped rather than accrued or queued.

### Citations

**File:** contracts/DepositToken.sol (L124-130)
```text
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
        _;
```

**File:** contracts/DepositToken.sol (L444-476)
```text
    function _burn(address _account, uint256 _amount) private updateRewardsBeforeMintOrBurn(_account) {
        if (_account == address(0)) revert BurnFromTheZeroAddress();

        uint256 _balanceBefore = balanceOf[_account];
        if (_balanceBefore < _amount) revert BurnAmountExceedsBalance();
        uint256 _balanceAfter;
        unchecked {
            _balanceAfter = _balanceBefore - _amount;
            totalSupply -= _amount;
        }

        balanceOf[_account] = _balanceAfter;

        emit Transfer(_account, address(0), _amount);

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
        }
    }

    /**
     * @notice Create `amount` tokens and assigns them to `account`, increasing
     * the total supply
     */
    function _mint(
        address account_,
        uint256 amount_
    ) private onlyIfDepositTokenIsActive updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert MintToTheZeroAddress();

        totalSupply += amount_;
        if (totalSupply > maxTotalSupply) revert SurpassMaxDepositSupply();
```

**File:** contracts/DebtToken.sol (L110-120)
```text
    /**
     * @notice Update reward contracts' states
     * @dev Should be called before balance changes (i.e. mint/burn)
     */
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
        _;
```

**File:** contracts/RewardsDistributor.sol (L201-208)
```text
        uint256 _speed = tokenSpeeds[token_];
        uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
            _newTimestamp = block.timestamp.toUint32();
```

**File:** contracts/RewardsDistributor.sol (L271-281)
```text
    function _updateTokenIndex(IERC20 token_) private {
        TokenState storage _supplyState = tokenStates[token_];
        (uint224 _newIndex, uint32 _newTimestamp) = _calculateTokenIndex(_supplyState, token_);
        if (_newIndex > 0 && _newTimestamp > 0) {
            _supplyState.index = _newIndex;
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_newIndex, _newTimestamp);
        } else if (_newTimestamp > 0) {
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_supplyState.index, _newTimestamp);
        }
```
