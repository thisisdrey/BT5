### Title
Airdrop rewards can remain locked when the claimant is restricted by the distribution token - ([File: contracts/utils/RecurringAirdrop.sol])

### Summary
`RecurringAirdrop.claim()` binds both entitlement and payout to `msg.sender`, so if the configured ERC20 rejects transfers to that account, the claimant has no way to direct the already-approved distribution to another address. [1](#0-0) 

### Finding Description
The Merkle leaf is derived from `msg.sender` and `amount_`, and `_transferReward()` is always called with `msg.sender` as the recipient. [2](#0-1)  The base implementation directly calls `token.safeTransfer(to_, amount_)`, meaning an external token that rejects transfers to the claimant causes the entire claim transaction to revert. [3](#0-2)  `MetAirdrop` inherits this behavior and sends MET directly to `to_` after the lock period; before that, it calls `ESMET.lockFor(to_, ...)`, which likewise gives the user no alternate receiving address. [4](#0-3) 

The same fixed-beneficiary pattern exists in `RewardsDistributor.claimRewards()`: accrued rewards are sent only to `account_`, with no caller-selected recipient. [5](#0-4)  The payout path clears `tokensAccruedOf[account_]` and calls `safeTransfer(account_, amount_)`; if the reward token rejects that account, the transfer and therefore the claim revert. [6](#0-5) 

### Impact Explanation
For a token that can permanently reject a destination address, the user’s airdrop or reward remains in the distributor indefinitely even though the user has a valid entitlement. [7](#0-6)  Because the revert rolls back `claimed[msg.sender]` or `tokensAccruedOf[account_]` updates, accounting remains internally consistent, but the payout liveness property fails: the entitlement can never be exercised to another non-restricted address. [8](#0-7)  The exposure is conditional on the deployed airdrop or reward token implementing recipient restrictions.

### Likelihood Explanation
This is not exploitable by an unprivileged attacker to freeze another user’s rewards; it requires the external token itself to reject the entitled recipient. [9](#0-8)  Once that external condition exists, every claim attempt deterministically reverts because no recovery address or claim-for-other-recipient path exists. [10](#0-9) 

### Recommendation
Allow the entitled account to select a payout recipient while preserving proof ownership.

For `RecurringAirdrop`, consider:

```solidity
function claim(
    address recipient_,
    uint256 amount_,
    bytes32[] calldata proof_
) external nonReentrant {
    if (recipient_ == address(0)) revert AddressIsNull();

    bytes32 leaf = keccak256(abi.encodePacked(msg.sender, amount_));
    if (!MerkleProof.verify(proof_, merkleRoot, leaf)) revert InvalidProof();

    uint256 claimable = amount_ - claimed[msg.sender];
    if (claimable == 0) revert NothingToClaim();

    claimed[msg.sender] += claimable;
    _transferReward(recipient_, claimable);
    emit RewardClaimed(recipient_, claimable);
}
```

For `RewardsDistributor`, add a recipient argument or a dedicated authenticated claim path that permits the entitled account to choose the destination without allowing arbitrary users to redirect another account’s rewards.

### Proof of Concept
The following Foundry test demonstrates the control-flow failure. It uses a production-shaped restriction in the external ERC20 dependency to model a token that rejects the entitled recipient:

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {RecurringAirdrop} from "../contracts/utils/RecurringAirdrop.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract RestrictedToken is IERC20 {
    mapping(address => uint256) public override balanceOf;
    mapping(address => mapping(address => uint256)) public override allowance;
    mapping(address => bool) public restricted;
    uint256 public override totalSupply;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        totalSupply += amount;
    }

    function restrict(address account) external {
        restricted[account] = true;
    }

    function transfer(address to, uint256 amount) external override returns (bool) {
        require(!restricted[to], "recipient restricted");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function approve(address spender, uint256 amount) external override returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transferFrom(
        address from,
        address to,
        uint256 amount
    ) external override returns (bool) {
        require(!restricted[to], "recipient restricted");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract RestrictedRecipientClaimTest is Test {
    function testRestrictedRecipientCannotClaim() public {
        RestrictedToken token = new RestrictedToken();
        RecurringAirdrop airdrop = new RecurringAirdrop(IERC20(address(token)));

        address alice = address(0xA11CE);
        uint256 amount = 100e18;

        // Single-leaf Merkle tree for abi.encodePacked(alice, amount).
        bytes32 leaf = keccak256(abi.encodePacked(alice, amount));
        bytes32[] memory proof = new bytes32[](0);

        vm.store(
            address(airdrop),
            bytes32(uint256(1)), // merkleRoot storage slot
            leaf
        );

        token.mint(address(airdrop), amount);
        token.restrict(alice);

        vm.prank(alice);
        vm.expectRevert("recipient restricted");
        airdrop.claim(amount, proof);

        assertEq(token.balanceOf(address(airdrop)), amount);
        assertEq(token.balanceOf(alice), 0);
    }
}
```

The proof shows that the entitlement is valid, the distributor is funded, and the payout fails solely because the fixed `msg.sender` destination is rejected.

### Citations

**File:** contracts/utils/RecurringAirdrop.sol (L52-75)
```text
    function claim(uint256 amount_, bytes32[] calldata proof_) external nonReentrant {
        if (merkleRoot == bytes32(0)) revert NothingToClaim();

        bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
        if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();

        uint256 _claimable = amount_ - claimed[msg.sender];
        if (_claimable == 0) revert NothingToClaim();

        claimed[msg.sender] += _claimable;

        _transferReward(msg.sender, _claimable);

        emit RewardClaimed(msg.sender, _claimable);
    }

    /**
     * @notice Transfer reward to the user
     * @param to_ The claim account
     * @param amount_ The reward amount
     */
    function _transferReward(address to_, uint256 amount_) internal virtual {
        token.safeTransfer(to_, amount_);
    }
```

**File:** contracts/MetAirdrop.sol (L31-47)
```text
    function _transferReward(address to_, uint256 amount_) internal override {
        uint256 _end = updatedAt + lockPeriod;

        if (_end < block.timestamp) {
            MET.safeTransfer(to_, amount_);
            return;
        }

        uint256 _min = ESMET.MINIMUM_LOCK_PERIOD() + 1;
        uint256 _max = ESMET.MAXIMUM_LOCK_PERIOD();

        // Ensures valid lock period
        uint256 _remainLockPeriod = Math.min(Math.max(_end - block.timestamp, _min), _max);

        token.safeApprove(address(ESMET), 0);
        token.safeApprove(address(ESMET), amount_);
        ESMET.lockFor(to_, amount_, _remainLockPeriod);
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

**File:** contracts/RewardsDistributor.sol (L248-255)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
```
