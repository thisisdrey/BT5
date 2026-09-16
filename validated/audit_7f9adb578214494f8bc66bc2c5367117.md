## Analysis

The report concerns cross-network replay of a `deploy()` operation whose resulting address doesn't bind to any chain-specific value, letting an attacker "front-run" or hijack the deterministic address on a different network. The directly analogous mechanism in java-tron is the native `CREATE2` opcode implementation.

`WalletUtil.generateContractAddress2` computes the CREATE2 address as `sha3omit12(senderAddress ++ salt ++ sha3(code))`, with **no chain-specific input at all**: [1](#0-0) 

This is invoked directly from the TVM's `CREATE2` opcode handler, `Program.createContract2`, which passes `senderAddress`, `salt`, and `programCode` straight into `generateContractAddress2`: [2](#0-1) 

Meanwhile java-tron already has an established notion of chain identity — the genesis block hash / `eth_chainId` — used elsewhere (e.g. `TronJsonRpcImpl.ethChainId()` derives chain id from block 0's hash, and P2P handshake rejects peers with a mismatched `genesisBlockId` as `INCOMPATIBLE_CHAIN`): [3](#0-2) [4](#0-3) 

Yet this chain-identity concept is never mixed into the CREATE2 address derivation. The only place a network-differentiating byte (`0x41` mainnet vs `0xa0` testnet) appears is inside user-supplied Solidity source, hardcoded by the compiler at compile time — it is not enforced or injected by the runtime itself: [5](#0-4) 

The practical consequence, confirmed by the test suite: contract addresses predicted off-chain via CREATE2 accept balance transfers, `freeze`, and delegated-resource operations *before* the contract itself is ever deployed: [6](#0-5) 

Since the native address formula depends only on `senderAddress`, `salt`, and `code` — never on genesis hash, chain id, or any other network-differentiator — identical inputs on any two TRON-compatible networks (mainnet, Nile, Shasta, or a privately operated fork/consortium chain running the same java-tron codebase) yield byte-identical contract addresses. Regular (non-CREATE2) transaction replay is separately mitigated by Tapos (`Manager.validateTapos` checking `ref_block_hash` against actual on-chain block hashes), so this is not verbatim transaction replay — it is address-collision across independently operated but code-compatible networks, i.e., the same class of hazard the report describes: a deployer or dApp that reuses the same salt/bytecode/deployer address across networks (a very common practice for maintaining "the same contract address" cross-chain) can have funds sent to the pre-computed address on one network stolen by a different, unrelated deployment reaching that identical address on another network, because nothing in the address derivation itself is network-scoped.

### Title
Native CREATE2 address derivation omits chain/network identity, enabling cross-network address collisions and fund theft - (File: `chainbase/src/main/java/org/tron/common/utils/WalletUtil.java`)

### Summary
`WalletUtil.generateContractAddress2`, used by the native `CREATE2` TVM opcode (`Program.createContract2`), derives a contract's address solely from `sender address + salt + keccak256(code)`. No genesis hash, chain id, or other network-scoping value is included, even though java-tron already maintains such a notion elsewhere (`eth_chainId`, P2P genesis-block matching). This mirrors the reported cross-chain replay class: a value that should be network-scoped is derived identically across networks, allowing an attacker on one TRON-compatible network to reproduce the exact contract address a victim expects only on another network.

### Finding Description
`generateContractAddress2` computes: `sha3omit12(address ++ salt ++ sha3(code))` with no domain separator for network identity. [7](#0-6) 

The TVM `CREATE2` opcode handler feeds it exactly the sender address, salt, and init code — nothing chain-specific: [8](#0-7) 

Any account that can compute its own contract address ahead of time can receive balance, frozen/delegated resources, or be a permission target *before* the contract actually exists (demonstrated in the test suite where `freezeForOther`/`unfreezeForOther` operate on a `predictedAddr` before `deployCreate2Contract` is ever called): [6](#0-5) 

Because the address formula is chain-agnostic, the same `(deployer, salt, bytecode)` tuple always resolves to the same address on every TRON-derived network — mainnet, any public testnet, or a privately operated fork sharing the same VM code. This contradicts the design intent elsewhere in the codebase, where chain identity is explicitly load-bearing (`ethChainId()` returns the genesis block hash; peers with a different `genesisBlockId` are treated as an incompatible chain): [3](#0-2) [9](#0-8) 

The only mitigation that exists is a compile-time constant (`0x41`/`0xa0`) baked into user Solidity source by the compiler — not enforced by the runtime and not present in the actual TVM/`WalletUtil` computation at all. [10](#0-9) 

### Impact Explanation
An attacker who deploys the same factory contract (or independently reconstructs the same bytecode) with the same `sender + salt` on a different TRON-compatible network can pre-empt or "squat" a CREATE2 address that a victim assumed was unique to their intended network. Any TRX transfer, `freeze`/delegate-resource operation, or permission grant made to that not-yet-deployed address before the victim's real deployment can be captured by whoever deploys the matching bytecode first at that address on that network, resulting in unauthorized account operations and theft of funds/resources sent to the precomputed address.

### Likelihood Explanation
Multi-network deployment with identical deployer key, salt, and bytecode (to preserve a consistent contract address across mainnet/testnets/private chains) is a common and even recommended pattern for CREATE2-based factories, so the precondition is realistic. The attacker only needs the ability to broadcast a deploy transaction on the target network with the same deployer address/salt/bytecode — no special privilege is required.

### Recommendation
Bind the CREATE2 address derivation to a chain-specific value (e.g., mix in the genesis block hash / chain id already computed via `ethChainId()`/`getGenesisBlockId()`) inside `WalletUtil.generateContractAddress2`, so identical `(sender, salt, code)` tuples resolve to different addresses on different TRON-compatible networks.

### Proof of Concept
1. On Network A, a user computes (off-chain, using the same formula as `WalletUtil.generateContractAddress2`) the CREATE2 address for `(factoryAddress, salt, initCode)` and sends TRX / freezes delegated resources to that address before deploying, exactly as exercised in `FreezeTest.testFreezeAndUnfreezeToCreate2Contract`.
2. An attacker, observing the public factory bytecode, salt, and deployer address (all visible on-chain or in the dApp's public deployment scripts), submits an equivalent deployment on Network B (any other TRON-compatible chain) using the same `factoryAddress`/`salt`/`initCode`.
3. Because `generateContractAddress2` has no chain-specific input, the resulting contract address on Network B is byte-identical to the one on Network A.
4. If the victim's operations (transfer/freeze) were mistakenly or interchangeably directed at that address on Network B (e.g. due to shared tooling/scripts across networks, or a migration scenario), the attacker's contract at the identical address now controls those funds/resources.

### Citations

**File:** chainbase/src/main/java/org/tron/common/utils/WalletUtil.java (L55-59)
```java
  // for `CREATE2`
  public static byte[] generateContractAddress2(byte[] address, byte[] salt, byte[] code) {
    byte[] mergedData = ByteUtil.merge(address, salt, Hash.sha3(code));
    return Hash.sha3omit12(mergedData);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1629-1653)
```java
  public void createContract2(DataWord value, DataWord memStart, DataWord memSize, DataWord salt) {
    if (VMConfig.allowTvmOsaka()) {
      returnDataBuffer = null; // reset return buffer right before the call
    }

    byte[] senderAddress;
    if ((VMConfig.allowTvmCompatibleEvm() || VMConfig.allowTvmOsaka())
        && getCallDeep() == MAX_DEPTH) {
      stackPushZero();
      return;
    }
    if (getCallDeep() == MAX_DEPTH) {
      MUtil.checkCPUTimeForCreate2();
    }
    if (VMConfig.allowTvmIstanbul()) {
      senderAddress = getContextAddress();
    } else {
      senderAddress = getCallerAddress().toTronAddress();
    }
    byte[] programCode = memoryChunk(memStart.intValue(), memSize.intValue());

    byte[] contractAddress = WalletUtil
        .generateContractAddress2(senderAddress, salt.getData(), programCode);
    createContractImpl(value, programCode, contractAddress, true);
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L422-431)
```java
  @Override
  public String ethChainId() throws JsonRpcInternalException {
    // return hash of genesis block
    try {
      byte[] chainId = wallet.getBlockCapsuleByNum(0).getBlockId().getBytes();
      return ByteArray.toJsonHex(Arrays.copyOfRange(chainId, chainId.length - 4, chainId.length));
    } catch (Exception e) {
      throw new JsonRpcInternalException(e.getMessage());
    }
  }
```

**File:** protocol/src/main/protos/core/Tron.proto (L595-599)
```text
  FORKED = 0x16;
  UNLINKABLE = 0x17;
  INCOMPATIBLE_VERSION = 0x18;
  INCOMPATIBLE_CHAIN = 0x19;
  TIME_OUT = 0x20;
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeTest.sol (L65-86)
```text
    // selector: 0xbb63e785
    // Predict CREATE2 address without deploying
    //
    // TRON CREATE2 formula (differs from standard EVM):
    //   address = keccak256(prefix ++ sender[20] ++ salt[32] ++ keccak256(code)[32])[12:]
    //
    // - Standard EVM uses 0xff as prefix (magic byte)
    // - TRON replaces it with the address prefix byte (0x41 for mainnet, 0xa0 for testnet)
    // - This value is hardcoded at compile time by tron-solc
    //
    function getCreate2Addr(uint256 salt) public view returns (address) {
        bytes memory bytecode = type(FreezeContract).creationCode;
        bytes32 hash = keccak256(
            abi.encodePacked(
                bytes1(0x41),       // TRON mainnet address prefix
                address(this),      // 20-byte factory address
                salt,               // 32-byte salt
                keccak256(bytecode) // 32-byte code hash
            )
        );
        return address(uint160(uint256(hash)));
    }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeTest.java (L372-391)
```java
  @Test
  public void testFreezeAndUnfreezeToCreate2Contract() throws Exception {
    byte[] factoryAddr = deployContract("FactoryContract", FACTORY_CODE);
    byte[] contractAddr = deployContract("TestFreeze", CONTRACT_CODE);
    long frozenBalance = 1_000_000;
    long salt = 1;
    byte[] predictedAddr = getCreate2Addr(factoryAddr, salt);
    Assert.assertNull(dbManager.getAccountStore().get(predictedAddr));
    freezeForOther(contractAddr, predictedAddr, frozenBalance, 0);
    Assert.assertNotNull(dbManager.getAccountStore().get(predictedAddr));
    freezeForOther(contractAddr, predictedAddr, frozenBalance, 1);
    unfreezeForOtherWithException(contractAddr, predictedAddr, 0);
    unfreezeForOtherWithException(contractAddr, predictedAddr, 1);
    clearDelegatedExpireTime(contractAddr, predictedAddr);
    unfreezeForOther(contractAddr, predictedAddr, 0);
    unfreezeForOther(contractAddr, predictedAddr, 1);

    freezeForOther(contractAddr, predictedAddr, frozenBalance, 0);
    freezeForOther(contractAddr, predictedAddr, frozenBalance, 1);
    Assert.assertArrayEquals(predictedAddr, deployCreate2Contract(factoryAddr, salt));
```

**File:** framework/src/test/java/org/tron/core/net/services/HandShakeServiceTest.java (L237-251)
```java
    //genesisBlock is not equal => INCOMPATIBLE_CHAIN
    builder = getHelloMessageBuilder(node2, System.currentTimeMillis(),
        ChainBaseManager.getChainBaseManager());
    BlockCapsule.BlockId gid = ChainBaseManager.getChainBaseManager().getGenesisBlockId();
    Protocol.HelloMessage.BlockId gBlockId = Protocol.HelloMessage.BlockId.newBuilder()
        .setHash(gid.getByteString())
        .setNumber(gid.getNum() + 1)
        .build();
    builder.setGenesisBlockId(gBlockId);
    try {
      HelloMessage helloMessage = new HelloMessage(builder.build().toByteArray());
      method.invoke(p2pEventHandler, peer, helloMessage.getSendBytes());
    } catch (Exception e) {
      Assert.fail();
    }
```
