This confirms the analog. `DecodeUtil.addressPreFixByte` is a mutable static field defaulting to `Constant.ADD_PRE_FIX_MAINNET` (`0x41`), but it is configurable per deployment (mainnet uses `0x41`; other java-tron networks, as documented in `FreezeTest.sol`'s comment "0x41 for mainnet, 0xa0 for testnet", use different prefixes). `HistoryBlockHashUtil` hardcodes a 21-byte address with a compiled-in `0x41` prefix instead of deriving it from `DecodeUtil.addressPreFixByte`, which is the direct structural analog of the reported bug class (hardcoded chain-specific address baked into contract logic instead of being resolved per-deployment).

### Title
Hardcoded 0x41 mainnet address prefix in TIP-2935 BlockHashHistory deployment breaks non-mainnet java-tron networks - (File: framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java)

### Summary
`HistoryBlockHashUtil` hardcodes `HISTORY_STORAGE_ADDRESS` and `HISTORY_DEPLOYER_ADDRESS` with a compiled-in `0x41` address-prefix byte, which is the TRON *mainnet* prefix. java-tron's address prefix is a network-level configurable value (`DecodeUtil.addressPreFixByte`, default `Constant.ADD_PRE_FIX_BYTE_MAINNET`), and other network deployments (private chains, testnets) use different prefix bytes. This mirrors the reported Solidity issue where a Uniswap `NonfungiblePositionManager` address was hardcoded for one chain and silently wrong on others.

### Finding Description
`HistoryBlockHashUtil` defines: [1](#0-0) 
These are 21-byte "TRON addresses" where the leading byte is hardcoded as `0x41` (mainnet). The prefix byte is not derived from the network's actual configured prefix (`DecodeUtil.addressPreFixByte`), which is set from `Constant.ADD_PRE_FIX_BYTE_MAINNET` by default but overridden per-network (e.g., different byte for private/test networks), and is used everywhere else in the codebase to validate whether an address belongs to the running chain: [2](#0-1) 

`HistoryBlockHashUtil.deploy()` is invoked at proposal activation (TIP-2935 / Prague hard fork flag) from `ProposalService`, and unconditionally writes account, code, and contract-store entries keyed by the hardcoded mainnet-prefixed address: [3](#0-2) 

On a network configured with a non-`0x41` address prefix, this deploys the BlockHashHistory system contract at an address whose prefix byte does not match `addressPreFixByte` for that chain. Any address-format validation path (`DecodeUtil.addressValid`) would reject that address as invalid for the running network, and any user/tooling code that legitimately queries or calls the canonical `0x0000...2935` slot using the chain's real prefix will resolve to a different, empty address — silently missing the deployed contract. Since `write()` gates on `isBlockHashHistoryInstalled()` and writes storage keyed to the same hardcoded address every block after activation, on a non-mainnet-prefix chain the feature activates (flag flips) but the historical-block-hash contract effectively lives at an address inconsistent with that chain's address space, making TIP-2935/EIP-2935 `blockhash`-via-contract reads unreachable or semantically wrong for consumers using the chain's real address format.

### Impact Explanation
For any java-tron-derived network that runs with a different `addressPreFixByte` than mainnet (private chains, alternate testnets), enabling the TIP-2935 hard fork silently deploys the BlockHashHistory system contract at a mainnet-prefixed address that is inconsistent with that chain's address space. Downstream contracts/tooling relying on the standard `0x0000...2935` historical-block-hash precompile-like contract cannot correctly reach it via the chain's normal (correctly prefixed) address, defeating the entire feature for those deployments after activation — a protocol-level malfunction of a consensus-activated feature, not merely a cosmetic issue, since it's baked permanently into `Manager.processBlock`'s per-block `write()` path once the flag is set.

### Likelihood Explanation
This triggers deterministically and automatically as soon as the `AllowTvmPrague`/TIP-2935 proposal is activated via committee vote on any java-tron-based network configured with a non-mainnet address prefix — no attacker action or malicious input is required, only normal governance/maintenance-cycle activation of the hard fork flag, which is a routine, expected operation.

### Recommendation
Derive the storage/deployer address prefix byte from `DecodeUtil.addressPreFixByte` (or the network's configured `Constant.ADD_PRE_FIX_BYTE_MAINNET`/equivalent) at `deploy()`/class-init time instead of hardcoding `0x41`, so `HISTORY_STORAGE_ADDRESS` and `HISTORY_DEPLOYER_ADDRESS` are computed per running network rather than compiled to a single mainnet-specific value.

### Proof of Concept
Not directly executable from the audit context alone; the code path is confirmed by inspection: `HISTORY_STORAGE_ADDRESS`/`HISTORY_DEPLOYER_ADDRESS` are `static final` fields hardcoded with `0x41`, while `DecodeUtil.addressPreFixByte` is demonstrably a configurable, per-network mutable field distinct from the mainnet constant, and `deploy()`/`write()` unconditionally operate on the hardcoded address once the hard-fork proposal is activated on any network.

### Citations

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L31-41)
```java
  // 21-byte TRON address (0x41 prefix + 20-byte EVM address 0x0000F908...2935)
  public static final byte[] HISTORY_STORAGE_ADDRESS =
      Hex.decode("410000f90827f1c53a10cb7a02335b175320002935");

  // Recovered sender of the EIP-2935 presigned (no-private-key) deploy
  // transaction on Ethereum, in TRON 21-byte form. Used as {@code originAddress}
  // on the deployed SmartContract so the deployer-of-record matches Ethereum
  // byte-for-byte; cross-chain tooling that inspects this field sees the same
  // address on both sides.
  public static final byte[] HISTORY_DEPLOYER_ADDRESS =
      Hex.decode("413462413af4609098e1e27a490f554f260213d685");
```

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L100-131)
```java
  public static void deploy(Manager manager) {
    if (manager.getCodeStore().has(HISTORY_STORAGE_ADDRESS)
        || manager.getContractStore().has(HISTORY_STORAGE_ADDRESS)) {
      logger.warn("TIP-2935: foreign state at {}, skipping deploy",
          Hex.toHexString(HISTORY_STORAGE_ADDRESS));
      return;
    }

    manager.getCodeStore().put(HISTORY_STORAGE_ADDRESS,
        new CodeCapsule(HISTORY_STORAGE_CODE));
    manager.getContractStore().put(HISTORY_STORAGE_ADDRESS,
        new ContractCapsule(HISTORY_STORAGE_CONTRACT));

    AccountCapsule account = manager.getAccountStore().get(HISTORY_STORAGE_ADDRESS);
    boolean accountExisting = account != null;
    if (!accountExisting) {
      account = new AccountCapsule(HISTORY_STORAGE_ACCOUNT);
    } else {
      account.updateAccountType(Protocol.AccountType.Contract);
      account.clearDelegatedResource();
    }
    manager.getAccountStore().put(HISTORY_STORAGE_ADDRESS, account);

    // Flip the install marker only after all three store writes succeed; this
    // gates the per-block write() path so a skipped deploy never mutates
    // foreign storage. Any node-local exception above propagates and rolls
    // the marker back together with the partial writes via the revoking session.
    manager.getDynamicPropertiesStore().saveBlockHashHistoryInstalled(1L);

    logger.info("TIP-2935: deployed BlockHashHistory at {} (preExistingAccount={})",
        Hex.toHexString(HISTORY_STORAGE_ADDRESS), accountExisting);
  }
```

**File:** common/src/main/java/org/tron/common/utils/DecodeUtil.java (L10-33)
```java
  public static final int ADDRESS_SIZE = 42;
  public static byte addressPreFixByte = Constant.ADD_PRE_FIX_BYTE_MAINNET;

  public static String addressPreFixString = Constant.ADD_PRE_FIX_STRING_MAINNET;

  public static boolean addressValid(byte[] address) {
    if (ArrayUtils.isEmpty(address)) {
      logger.warn("Warning: Address is empty !!");
      return false;
    }
    if (address.length != ADDRESS_SIZE / 2) {
      logger.warn(
          "Warning: Address length need " + ADDRESS_SIZE + " but " + address.length
              + " !!");
      return false;
    }

    if (address[0] != addressPreFixByte) {
      logger.warn("Warning: Address need prefix with " + addressPreFixByte + " but "
          + address[0] + " !!");
      return false;
    }
    return true;
  }
```
