#!/usr/bin/env python3
"""Rebuild the bundled DEX using a JDK and Android SDK (no dependencies)."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    root = Path(__file__).resolve().parent
    sdk = Path(os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or Path.home() / "android-sdk")
    java_home = Path(os.environ["JAVA_HOME"]) / "bin" if os.environ.get("JAVA_HOME") else None
    javac = str(java_home / "javac") if java_home else shutil.which("javac")
    java = str(java_home / "java") if java_home else shutil.which("java")
    if not javac or not java:
        raise SystemExit("Install a JDK or set JAVA_HOME before rebuilding the helper.")
    platforms = sorted(sdk.glob("platforms/android-*/android.jar"))
    compilers = sorted(sdk.glob("build-tools/*/lib/d8.jar"))
    if not platforms or not compilers:
        raise SystemExit("Set ANDROID_SDK_ROOT to an SDK with a platform and build-tools.")
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run([javac, "--release", "8", "-classpath", str(platforms[-1]),
                        "-d", directory, str(root / "MultiTouch.java")], check=True)
        subprocess.run([java, "-cp", str(compilers[-1]), "com.android.tools.r8.D8",
                        "--min-api", "23", "--lib", str(platforms[-1]), "--output", directory,
                        str(Path(directory) / "MultiTouch.class")], check=True)
        shutil.copyfile(Path(directory) / "classes.dex", root / "multitouch.dex")
    print("Built", root / "multitouch.dex")


if __name__ == "__main__":
    main()
