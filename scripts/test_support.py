"""Shared subprocess command for tests, with opt-in child-process coverage."""
import os
import sys


def python_command(*arguments):
    command = [sys.executable]
    config = os.environ.get("COVERAGE_PROCESS_START")
    if config:
        command += ["-m", "coverage", "run", "--rcfile", config, "--parallel-mode"]
    return [*command, *map(str, arguments)]
