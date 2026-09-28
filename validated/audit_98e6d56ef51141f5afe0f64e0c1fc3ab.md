### Title
Fee-on-transfer rewards are marked fully claimed despite recipients receiving less - (File: contracts/utils/RecurringAirdrop.sol)

### Summary
`RecurringAirdrop.claim()` records the gross Merkle claim amount before transferring the reward. If the configured reward token charges a transfer fee, the recipient receives less than the recorded amount and can never claim the missing difference.

### Finding Description
In `contracts/utils/RecurringAirdrop.sol:52-64`, `claim()` derives `_claimable` from the cumulative Merkle leaf amount minus `claimed[msg.sender]`, then increments `claimed[msg.sender]` by the full `_claimable` amount before calling `_transferReward()`.

The base implementation in `contracts/utils/RecurringAirdrop.sol:73-75` performs only:

```solidity
token.safeTransfer(to_, amount_);
```

It does not compare the recipient’s balance before and after the transfer. With a fee-on-transfer token, `amount_` leaves the airdrop contract but only `amount_ - fee` reaches the claimant. Since `claimed` was already increased by `amount_`, subsequent cumulative claims calculate rewards as if the claimant had received the full amount.

The same accounting issue affects inherited or overridden reward delivery paths. For example, `MetAirdrop._transferReward()` transfers or locks the configured reward token without measuring the amount actually credited to the user or the external lock position.

### Impact Explanation
Claimants permanently lose the transfer-fee portion of their accrued rewards.

Because the lost amount remains included in `claimed[msg.sender]`, the missing tokens cannot be recovered through a later claim. If the token burns its transfer fee, the value is destroyed; if it redirects the fee to another account, that account captures part of the user’s reward. In either case, the airdrop accounting no longer matches the value actually received by eligible users.

### Likelihood Explanation
The issue is reachable through the public `claim()` function and requires no privileged action during the claim. `RecurringAirdrop` is generic and accepts an arbitrary immutable ERC20 reward token at construction, so any deployment using a fee-on-transfer token is affected.

The issue does not require proof forgery, reentrancy, oracle manipulation, or control of the token. A normal eligible claim is sufficient.

### Recommendation
Measure the reward actually received and update `claimed` by that amount, or revert unless the full claim amount is received.

For example, in `_transferReward()`:

```solidity
uint256 balanceBefore = token.balanceOf(to_);
token.safeTransfer(to_, amount_);
uint256 received = token.balanceOf(to_) - balanceBefore;
if (received != amount_) revert FeeOnTransferNotSupported();
```

If fee-on-transfer tokens must be supported, return `received` from `_transferReward()` and increment `claimed[msg.sender]` by `received` rather than the gross `_claimable` amount. Equivalent balance-delta validation should also be applied to override paths such as `MetAirdrop._transferReward()` where practical.

### Proof of Concept
A Hardhat test can reproduce the accounting mismatch using the existing fee-enabled `ERC20Mock`:

```typescript
import {expect} from 'chai'
import {ethers} from 'hardhat'
import {parseEther} from 'ethers/lib/utils'
import {MerkleTree} from 'merkletreejs'
import keccak256 from 'keccak256'

it('records the gross claim amount while the user receives less', async function () {
  const [governor, alice] = await ethers.getSigners()

  const ERC20Mock = await ethers.getContractFactory('ERC20Mock')
  const token = await ERC20Mock.deploy('FeeToken', 'FEE', 18)

  const RecurringAirdrop = await ethers.getContractFactory('RecurringAirdrop')
  const airdrop = await RecurringAirdrop.deploy(token.address)

  const entitlement = parseEther('100')
  await token.mint(airdrop.address, entitlement)

  // 10% transfer fee.
  await token.updateFee(parseEther('0.1'))

  const leaf = keccak256(
    ethers.utils.solidityPack(['address', 'uint256'], [alice.address, entitlement])
  )
  const tree = new MerkleTree([leaf], keccak256, {sortPairs: true})

  await airdrop.connect(governor).updateMerkleRoot(
    tree.getHexRoot(),
    ethers.utils.formatBytes32String('proofs')
  )

  await airdrop.connect(alice).claim(entitlement, tree.getHexProof(leaf))

  // Alice receives only 90% of the reward.
  expect(await token.balanceOf(alice.address)).to.eq(parseEther('90'))

  // The contract nevertheless marks the full 100 tokens as claimed.
  expect(await airdrop.claimed(alice.address)).to.eq(entitlement)

  // The missing 10 tokens can never be claimed.
  await expect(
    airdrop.connect(alice).claim(entitlement, tree.getHexProof(leaf))
  ).to.be.revertedWithCustomError(airdrop, 'NothingToClaim')
})
```