### Title
Symlink Following in `VMUtils.createProgramTraceFile` allows arbitrary file overwrite via predictable, attacker-known trace file path - (File: actuator/src/main/java/org/tron/core/vm/VMUtils.java)

### Summary
When a node operator enables `vm.vmTrace`, every smart-contract call/creation causes `VMActuator` to write a VM execution trace file whose path is deterministically derived from the transaction hash: `./vm_trace/<txHash>.json`. `VMUtils.createProgramTraceFile()` neither checks whether that path is a symbolic link nor opens the file with `O_NOFOLLOW`/`CREATE_NEW`-style atomicity; if the file already exists and is a regular file that is writable, it is reused as-is and overwritten. This mirrors the CVE-2026-72696 (Grav CMS `createLockFile`) bug class: a predictable filesystem path in a directory the process can write to, followed without verifying it isn't a symlink an attacker planted.

### Finding Description
`createProgramTraceFile` builds a file path solely from a caller-supplied transaction hash: [1](#0-0) 

An attacker can construct and locally sign a transaction (a contract call/deploy) whose txHash is `known in advance` (it's a hash of the raw transaction bytes the attacker controls before broadcasting). If the attacker has any means of pre-creating a filesystem entry at `<node-workdir>/vm_trace/<txHash>.json` — e.g. via another feature that writes attacker-influenceable files into the working directory, or via local access to the node's working directory — they can instead plant a **symbolic link** at that exact path pointing to any file the tron node process can write to (e.g. `config.conf`, another `.json` under the working directory, or files reachable via relative paths). Because `file.exists()` returns true for symlinks and `file.isFile()`/`file.canWrite()` follow the link, the existing-file branch is taken and the target of the symlink — not the intended trace file — is what gets truncated and overwritten via `writeStringToFile` → `new FileOutputStream(file)`: [2](#0-1) 

The call site is reached directly from unprivileged contract execution in `VMActuator`, once per triggered smart contract call, with the trace content being the VM opcode trace (attacker-influenced content since it echoes contract execution details, return value, and any exception): [3](#0-2) 

This is the analog of the Grav CMS bug: predictable lock/output path + symlink-following write, gated only by a "the scheduled job runs" equivalent (here, "vmTrace is enabled and a contract call/creation runs").

### Impact Explanation
If reachable, this allows an unprivileged contract-calling account to overwrite arbitrary files writable by the node process (subject to relative-path traversal from the working directory, e.g. `../../` targets), which can corrupt node configuration, corrupt database/store files, or otherwise crash/halt the node — satisfying the "node crash or halt" / file-overwrite bar in the validation criteria.

### Likelihood Explanation
Exploitability is gated by `VMConfig.vmTrace()`, which defaults to `false` in `reference.conf` and `VmConfig`: [4](#0-3) [5](#0-4) 

This is explicitly documented as "For developer only": [6](#0-5) 

Because this flag is off by default on production/mainnet full nodes and SRs, the practical likelihood on a stock, unmodified deployment is low — an operator must have explicitly turned on debug tracing. I could not find any other unprivileged-reachable API/actuator path (HTTP, gRPC, JSON-RPC, actuator validate/execute, exchange, stake/delegation) that creates predictable lock/temp files without symlink protection; all other file-writing paths I found (`WalletUtils.writeWalletFile`, `KeystoreCliUtils`, LevelDB/RocksDB `initDB`) are either CLI/keystore-tooling (explicitly out of scope) or already harden against symlinks (`NOFOLLOW_LINKS`, atomic temp-file+rename, `Files.isSymbolicLink` checks on DB paths).

### Recommendation
In `VMUtils.createProgramTraceFile`, verify the path is not a symlink before treating it as writable (e.g. `Files.readAttributes(path, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS)` and reject if `isSymbolicLink()`), and open/write using `StandardOpenOption.CREATE, StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS` via `Files.newByteChannel`/`Files.newOutputStream` instead of `new FileOutputStream(file)`, matching the pattern already used in `KeystoreCliUtils.readKeystoreFile`/`readRegularFile`.

### Proof of Concept
1. Operator runs a node with `vm.vmTrace = true` (non-default, debug-only setting).
2. Attacker builds a raw contract-call transaction and computes its txHash locally (deterministic before broadcast).
3. Attacker gains write access to the node's working directory `./vm_trace/` (e.g. through another mechanism, or the directory being on a shared/writable filesystem) and creates `./vm_trace/<txHash>.json` as a symlink pointing to a target file the tron process can write (e.g. a config file or another database artifact reachable by relative path).
4. Attacker broadcasts the transaction. `VMActuator.finalization/vm-trace path` calls `VMUtils.saveProgramTraceFile(txHash, traceContent)` → `createProgramTraceFile` finds the symlink already "exists" and is "writable" → `writeStringToFile` opens `new FileOutputStream(file)`, which follows the symlink and truncates/overwrites the real target with the VM trace JSON content.

Note: full exploitation requires local/filesystem-level write access to the node's working directory to plant the symlink and requires the non-default `vmTrace` debug flag to be enabled, so this should be treated as a lower-likelihood analog than the original CVE (which affects any deployment with the world-writable temp dir and the scheduler always running). This caveat is called out because I was unable to find a fully unprivileged, default-on production path exhibiting the exact same bug class (predictable-path + symlink-following file write) reachable purely via a remote transaction/API call without any local prerequisite.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/VMUtils.java (L52-77)
```java
  private static File createProgramTraceFile(String txHash) {
    File result = null;

    if (VMConfig.vmTrace()) {

      File file = new File(new File("./", "vm_trace"), txHash + ".json");

      if (file.exists()) {
        if (file.isFile() && file.canWrite()) {
          result = file;
        }
      } else {
        try {
          file.getParentFile().mkdirs();
          if (!file.createNewFile()) {
            logger.error("failed to create file {}", file.getPath());
          }
          result = file;
        } catch (IOException e) {
          // ignored
        }
      }
    }

    return result;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/VMUtils.java (L79-98)
```java
  private static void writeStringToFile(File file, String data) {
    OutputStream out = null;
    try {
      out = new FileOutputStream(file);
      if (data != null) {
        out.write(data.getBytes("UTF-8"));
      }
    } catch (Exception e) {
      logger.error(format("Cannot write to file '%s': ", file.getAbsolutePath()), e);
    } finally {
      closeQuietly(out);
    }
  }

  public static void saveProgramTraceFile(String txHash, String content) {
    File file = createProgramTraceFile(txHash);
    if (file != null) {
      writeStringToFile(file, content);
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L303-318)
```java
    //use program returned fill context
    context.setProgramResult(result);

    if (VMConfig.vmTrace() && program != null) {
      String traceContent = program.getTrace()
          .result(result.getHReturn())
          .error(result.getException())
          .toString();

      if (VMConfig.vmTraceCompressed()) {
        traceContent = VMUtils.zipAndEncode(traceContent);
      }

      String txHash = Hex.toHexString(rootInternalTx.getHash());
      VMUtils.saveProgramTraceFile(txHash, traceContent);
    }
```

**File:** common/src/main/resources/reference.conf (L787-787)
```text
  vmTrace = false                         # Whether to enable VM trace output.
```

**File:** common/src/main/java/org/tron/core/config/args/VmConfig.java (L25-25)
```java
  private boolean vmTrace = false;
```

**File:** common/src/main/java/org/tron/core/vm/config/VMConfig.java (L6-9)
```java
/**
 * For developer only
 */
public class VMConfig {
```
