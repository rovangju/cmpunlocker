#!/usr/bin/env python3
"""Prepare the three pinned section-2 P2P patches (no driver installation)."""

from pathlib import Path
import os
import re
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
PATCH_DIR = ROOT / "driver/patches"
BUILD = ROOT / "driver/build.sh"
CONSTANTS = ROOT / "common/constants.yaml"
INSTALL = ROOT / "install.sh"
REGKEY_BASE = 'options nvidia NVreg_RegistryDwords="RmForceEnableGen2=1;RMPcieLinkSpeed=0x1"'
REGKEY_P2P = ('options nvidia NVreg_RegistryDwords="RmForceEnableGen2=1;RMPcieLinkSpeed=0x1;'
              'RMForceStaticBar1=1;RMPcieP2PType=1;RMForceP2PType=1;ForceP2P=0x11"')
# Fetch a fixed upstream revision so repeat runs use the same patch contents.
COMMIT = "5a7bb4b7e5056306fe49e8b824787659abb19914"
BASE_URL = f"https://raw.githubusercontent.com/bayley/cmpunlocker/{COMMIT}/driver/patches"
# Expected order on the cmpunlocker revision described in the gist; reject other
# layouts rather than guessing where to insert the P2P patches.
BASE_ORDER = (
    "sec2-postbl-plm-ss-cfg.patch",
    "booter-verify.patch",
    "late-pma.patch",
    "bar0-pramin-clamp.patch",
    "ce-scrub-workarounds.patch",
    "persistent-sw-state.patch",
    "pcie-gen2.patch",
    "pcie-gen2-probe-retrain.patch",
    "name-string.patch",
    "bar1-resize-unlock.patch",
    "cmp-sku-mask.patch",
)
P2P_PATCHES = (
    ("p2p_bar1", "0011-p2p-bar1.patch"),
    ("p2p_skip_mailbox", "0013-skip-mailbox-peer-preinit.patch"),
    ("p2p_readcap", "0015-bar1p2p-readcap-override.patch"),
)


def download_patch(name):
    url = f"{BASE_URL}/{name}"
    for attempt in range(3):
        try:
            with urlopen(url, timeout=30) as response:
                data = response.read()
            break
        except URLError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    # Two upstream patches have a prose preface, so look for diff hunks rather
    # than requiring a "diff --git" header at the start of the file.
    if not all(re.search(pattern, data) for pattern in
               (rb"(?m)^--- .+", rb"(?m)^\+\+\+ .+", rb"(?m)^@@ ")):
        raise ValueError(f"Invalid or empty patch: {name}")
    return data


def updated_build(text):
    match = re.search(r"^PATCH_ORDER=\(\n(.*?)^\)", text, re.M | re.S)
    if not match:
        raise ValueError("PATCH_ORDER array not found in driver/build.sh")
    order = tuple(line.strip() for line in match.group(1).splitlines())
    wanted = BASE_ORDER + tuple(patch for _, patch in P2P_PATCHES)
    if order == wanted:
        return text  # Already prepared; keep the file untouched.
    if order != BASE_ORDER:
        raise ValueError("Unexpected PATCH_ORDER: expected the section-2 base order or completed order")
    array = "PATCH_ORDER=(\n" + "".join(f"    {patch}\n" for patch in wanted) + ")"
    return text[:match.start()] + array + text[match.end():]


def updated_constants(text):
    if not re.search(r"^unlocks:\s*$", text, re.M):
        raise ValueError("unlocks: section not found in common/constants.yaml")
    # The build's constants check requires a declaration for each new patch.
    entries = "\n" + "\n".join(
        f"  {key}:\n    patch: {patch}\n    registers: {{}}\n"
        for key, patch in P2P_PATCHES
    )
    if text.endswith(entries):
        return text
    if any(re.search(rf"^  {key}:", text, re.M) for key, _ in P2P_PATCHES):
        raise ValueError("Existing P2P declarations differ from section 2; refusing to overwrite")
    # Append inside unlocks only when its known final entry is still last.
    suffix = "  cmp-sku-mask:\n    patch: cmp-sku-mask.patch\n    registers: {}\n"
    if not text.endswith(suffix):
        raise ValueError("Expected cmp-sku-mask to be the final unlock in common/constants.yaml")
    return text + entries


def updated_install(text):
    # install.sh overwrites the modprobe file on every run; change its source
    # rather than writing /etc here (which would be undone at installation).
    match = re.search(r"(?m)^cat > /etc/modprobe\.d/cmp-pcie-gen2\.conf <<'EOF'\n(.*?)^EOF$",
                      text, re.S | re.M)
    if not match:
        raise ValueError("PCIe Gen2 modprobe heredoc not found in install.sh")
    current = match.group(1)
    if current == REGKEY_P2P + "\n":
        return text
    if current != REGKEY_BASE + "\n":
        raise ValueError("Unexpected PCIe Gen2 modprobe contents in install.sh")
    return text[:match.start(1)] + REGKEY_P2P + "\n" + text[match.end(1):]


def main():
    if not PATCH_DIR.is_dir():
        raise ValueError(f"Missing {PATCH_DIR}")

    # Validate all downloads and both config files before changing anything.
    patches = {}
    for _, name in P2P_PATCHES:
        data = download_patch(name)
        target = PATCH_DIR / name
        # Do not overwrite a local edit masquerading as a pinned patch.
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Existing patch differs from pinned upstream: {target}")
        patches[target] = data

    build = updated_build(BUILD.read_text())
    constants = updated_constants(CONSTANTS.read_text())
    install = updated_install(INSTALL.read_text())

    # Stage changed files alongside their targets for per-file atomic renames;
    # unchanged files are left alone, including on repeat runs.
    changes = {BUILD: build.encode(), CONSTANTS: constants.encode(), INSTALL: install.encode()}
    changes.update(patches)
    staged = []
    try:
        for target, data in changes.items():
            if target.exists() and target.read_bytes() == data:
                continue
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
                staged.append((Path(tmp.name), target))
                tmp.write(data)
            if target.exists():
                os.chmod(staged[-1][0], target.stat().st_mode)
        for source, target in staged:
            os.replace(source, target)
    finally:
        for source, _ in staged:
            source.unlink(missing_ok=True)
    print("P2P patch prep complete: three pinned patches, PATCH_ORDER, unlocks, and installer regkey")
    print("Next: re-run ./install.sh to rebuild and install the patched driver, then reboot.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, URLError, ValueError) as exc:
        sys.exit(f"P2P patch prep failed: {exc}")
