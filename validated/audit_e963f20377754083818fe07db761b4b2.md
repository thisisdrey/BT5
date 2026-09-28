### Title
Rewards become permanently unclaimable after `uint32` timestamp overflow - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` stores reward timestamps as `uint32` and casts every future `block.timestamp` through `SafeCast.toUint32()`. Once the chain timestamp exceeds `type(uint32).max`, every reward-index update reverts, permanently blocking `claimRewards`, `updateBeforeMintOrBurn`, and `updateBeforeTransfer` for all initialized reward tokens.

### Finding Description
The reward state stores the last update timestamp in a `uint32` field. [1](#0-0) 

Whenever rewards are claimed, the contract calls `_updateTokenIndex()` for every initialized token before transferring accrued rewards. [2](#0-1) 

`_updateTokenIndex()` calls `_calculateTokenIndex()`. If at least one second has elapsed, `_calculateTokenIndex()` assigns `block.timestamp.toUint32()` to `_newTimestamp`. [3](#0-2) 

After `block.timestamp > type(uint32).max`, `toUint32()` reverts. Consequently, `claimRewards()` always reverts before reaching `_transferRewardIfEnoughTokens()`, even if `tokensAccruedOf[account_]` is already nonzero and the distributor holds enough reward tokens. [2](#0-1) 

The same failure reaches token-balance hooks. `updateBeforeMintOrBurn()` and `updateBeforeTransfer()` call `_updateTokenIndex()` whenever a token has a nonzero index. [4](#0-3)  `DepositToken` invokes these reward hooks before burning deposit tokens. [5](#0-4)  `DebtToken` does the same before minting or burning debt balances. [6](#0-5) 

Thus, after the overflow, initialized rewards cannot be claimed and every `DepositToken` or `DebtToken` balance-changing operation routed through the registered distributor can revert. Collateral withdrawals that burn deposit tokens are therefore also blocked while the distributor remains registered. [5](#0-4) 

### Impact Explanation
This breaks the reward and collateral-withdrawal liveness invariant. All unclaimed rewards held by an affected distributor become inaccessible through its public interface, and protocol operations that invoke the distributor's hooks can permanently revert for every user until privileged remediation removes or replaces the distributor.

The failure affects already-accrued rewards, not merely future emissions. `claimRewards()` updates all selected token indexes before paying `tokensAccruedOf`, so even a previously stored claimable balance cannot be transferred after the timestamp boundary. [2](#0-1) 

### Likelihood Explanation
The trigger requires no privileged role, malformed input, oracle manipulation, or attacker contract. It occurs automatically when `block.timestamp` exceeds `4,294,967,295`, corresponding to February 2106.

The preconditions are that a `RewardsDistributor` remains registered and has at least one initialized reward token with `tokenStates[token].index > 0`. Normal deployment and `updateTokenSpeed()` initialization creates that state. [7](#0-6) 

### Recommendation
Store reward timestamps as `uint64`, `uint128`, or `uint256` instead of `uint32`. For example, change `TokenState.timestamp` to `uint64` and use `SafeCast.toUint64(block.timestamp)`, preserving enough range for the protocol's expected lifetime.

Also add a regression test that:

1. Initializes a reward token and nonzero speed.
2. Accrues a claimable balance.
3. Warps beyond `type(uint32).max`.
4. Verifies that `claimRewards()` succeeds.
5. Verifies that deposit/debt mint, burn, and transfer hooks do not revert.

A migration for deployed contracts must retain existing `tokenStates[token].timestamp` values while widening the storage field.

### Proof of Concept

The following Foundry test demonstrates the revert against an initialized `RewardsDistributor`. The concrete deployment address and active reward token can be taken from the deployed pool's distributor list and `tokens(i)` array.

```solidity
// test/RewardsDistributorTimestampOverflow.t.sol
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import "../contracts/RewardsDistributor.sol";
import "../contracts/interfaces/IPool.sol";
import "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract RewardsDistributorTimestampOverflowTest is Test {
    function test_claimRevertsAfterUint32TimestampOverflow() public {
        // Replace with the deployed chain fork and deployed contract addresses.
        RewardsDistributor distributor =
            RewardsDistributor(/* deployed RewardsDistributor proxy */ address(0));
        IPool pool = distributor.pool();
        address account = address(this);

        // Existing initialized reward token on the deployed configuration.
        IERC20 trackedToken = distributor.tokens(0);
        require(distributor.tokenStates(trackedToken).index > 0, "reward token not initialized");

        // Fund the distributor and/or ensure account already has tokensAccruedOf > 0
        // using the deployed reward token and normal pre-overflow reward accrual.
        uint256 accruedBefore = distributor.tokensAccruedOf(account);
        require(accruedBefore > 0, "account has no accrued rewards");

        // First timestamp after uint32 overflow.
        vm.warp(uint256(type(uint32).max) + 1);

        IERC20[] memory selectedTokens = new IERC20[](1);
        selectedTokens[0] = trackedToken;

        // SafeCast.toUint32(block.timestamp) reverts inside _calculateTokenIndex().
        vm.expectRevert();
        distributor.claimRewards(account, selectedTokens);

        // The accrued reward remains inaccessible through the public claim path.
        assertEq(distributor.tokensAccruedOf(account), accruedBefore);
    }
}
```

The same warp followed by a public `updateBeforeMintOrBurn(trackedToken, account)` call also reverts through `_updateTokenIndex()`, demonstrating that the overflow affects token hooks as well as direct reward claims. [8](#0-7)

### Citations

**File:** contracts/storage/RewardsDistributorStorage.sol (L9-12)
```text
    struct TokenState {
        uint224 index; // The last updated index
        uint32 timestamp; // The timestamp of the latest index update
    }
```

**File:** contracts/RewardsDistributor.sol (L150-167)
```text
    function claimRewards(address[] memory accounts_, IERC20[] memory tokens_) public override nonReentrant {
        uint256 _accountsLength = accounts_.length;
        uint256 _tokensLength = tokens_.length;
        for (uint256 i; i < _tokensLength; ++i) {
            IERC20 _token = tokens_[i];

            if (tokenStates[_token].index > 0) {
                _updateTokenIndex(_token);
                for (uint256 j; j < _accountsLength; j++) {
                    _updateTokensAccruedOf(_token, accounts_[j]);
                }
            }
        }

        for (uint256 j; j < _accountsLength; j++) {
            address _account = accounts_[j];
            _transferRewardIfEnoughTokens(_account, tokensAccruedOf[_account]);
        }
```

**File:** contracts/RewardsDistributor.sol (L175-191)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }

    /**
     * @notice Update indexes on pre-transfer
     * @dev Called by DepositToken and DebtToken contracts
     */
    function updateBeforeTransfer(IERC20 token_, address from_, address to_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, from_);
            _updateTokensAccruedOf(token_, to_);
        }
```

**File:** contracts/RewardsDistributor.sol (L197-211)
```text
    function _calculateTokenIndex(
        TokenState memory _supplyState,
        IERC20 token_
    ) private view returns (uint224 _newIndex, uint32 _newTimestamp) {
        uint256 _speed = tokenSpeeds[token_];
        uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
            _newTimestamp = block.timestamp.toUint32();
        } else if (_deltaTimestamps > 0 && _supplyState.index > 0) {
            _newTimestamp = block.timestamp.toUint32();
        }
```

**File:** contracts/RewardsDistributor.sol (L287-309)
```text
    function _updateTokenSpeed(
        IERC20 token_,
        uint256 newSpeed_
    ) private onlyIfDistributorExists onlyIfTokenExists(address(token_)) {
        uint256 _currentSpeed = tokenSpeeds[token_];
        if (_currentSpeed > 0) {
            _updateTokenIndex(token_);
        } else if (newSpeed_ > 0) {
            // Add token to the list
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
            } else {
                // Update timestamp to ensure extra interest is not accrued during the prior period
                tokenStates[token_].timestamp = block.timestamp.toUint32();
            }
        }

        if (_currentSpeed != newSpeed_) {
            tokenSpeeds[token_] = newSpeed_;
            emit TokenSpeedUpdated(token_, _currentSpeed, newSpeed_);
        }
```

**File:** contracts/DepositToken.sol (L444-462)
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
```

**File:** contracts/DebtToken.sol (L114-120)
```text
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
        _;
```
