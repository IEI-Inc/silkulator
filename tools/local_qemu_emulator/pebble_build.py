#!/usr/bin/env python3
"""
pebble_build.py -- one-command build of the silk (Pebble 2) firmware for QEMU.

This just drives waf the same way the fixes in this session were built and
verified:

    python2 ./waf configure --board=silk_bb --qemu --nojs --nowatchdog \\
        --relax_toolchain_restrictions
    python2 ./waf build
    python2 ./waf qemu_image_micro qemu_image_spi

waf itself is Python-2-only (this is a ~2016-era firmware tree), so this
script shells out to `python2`, not the interpreter running this script.

Usage:
    python3 pebble_build.py [--repo PATH] [--clean]

Requires (one-time setup, not handled by this script):
    - a checkout of https://github.com/IEI-Inc/silkulator (or google/pebble
      plus this session's fixes)
    - python2 on PATH, with waf's own dependencies installed
      (see the repo's own setup docs -- typically `pip2 install -r
      requirements.txt` from the repo root)
    - the arm-none-eabi-gcc toolchain on PATH

Output (relative to the repo root):
    build/src/fw/tintin_fw.elf    -- the firmware ELF (for addr2line/objdump/gdb)
    build/qemu_micro_flash.bin    -- internal flash image, pass to qemu -kernel
    build/qemu_spi_flash.bin      -- external SPI flash image, pass to qemu -mtdblock
"""

import argparse
import os
import shutil
import subprocess
import sys


def find_repo_root(start):
    """Walk upward from `start` looking for a directory containing waf."""
    path = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(path, "waf")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def check_tool(name, hint):
    found = shutil.which(name)
    if not found:
        print(f"ERROR: '{name}' not found on PATH.\n  {hint}", file=sys.stderr)
        return False
    print(f"  found {name}: {found}")
    return True


def run(cmd, cwd):
    print(f"\n$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"\nERROR: command failed (exit {result.returncode}): {' '.join(cmd)}",
              file=sys.stderr)
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=None,
                         help="Path to the pebble firmware repo "
                              "(default: auto-detect from this script's location, "
                              "or the current directory)")
    parser.add_argument("--clean", action="store_true",
                         help="Run `waf distclean` before configuring")
    parser.add_argument("--board", default="silk_bb",
                         help="Board to build (default: silk_bb)")
    args = parser.parse_args()

    repo = args.repo or find_repo_root(os.getcwd()) or find_repo_root(os.path.dirname(__file__))
    if not repo:
        print("ERROR: could not find the firmware repo (no 'waf' file found above "
              "the current directory). Pass --repo /path/to/silkulator explicitly.",
              file=sys.stderr)
        sys.exit(1)
    print(f"Using repo: {repo}")

    print("\nChecking prerequisites...")
    ok = True
    ok &= check_tool("python2", "Install Python 2.7 -- waf itself only runs under it.")
    ok &= check_tool("arm-none-eabi-gcc", "Install the arm-none-eabi GCC toolchain "
                      "(e.g. from your distro's package manager, or "
                      "developer.arm.com/downloads/-/arm-gnu-toolchain-downloads).")
    if not ok:
        sys.exit(1)

    if args.clean:
        run(["python2", "./waf", "distclean"], cwd=repo)

    run(["python2", "./waf", "configure",
         f"--board={args.board}",
         "--qemu", "--nojs", "--nowatchdog",
         "--relax_toolchain_restrictions"], cwd=repo)

    run(["python2", "./waf", "build"], cwd=repo)

    run(["python2", "./waf", "qemu_image_micro", "qemu_image_spi"], cwd=repo)

    elf = os.path.join(repo, "build", "src", "fw", "tintin_fw.elf")
    micro = os.path.join(repo, "build", "qemu_micro_flash.bin")
    spi = os.path.join(repo, "build", "qemu_spi_flash.bin")
    print("\nBuild complete.")
    for label, path in (("firmware ELF", elf), ("internal flash image", micro),
                         ("SPI flash image", spi)):
        mark = "OK" if os.path.exists(path) else "MISSING"
        print(f"  [{mark}] {label}: {path}")

    print("\nNext: python3 pebble_emulator.py --repo " + repo)


if __name__ == "__main__":
    main()
