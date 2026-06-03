import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_SMOKE_ENV = "MODASH_PACKAGING_SMOKE"


def run_command(args, *, cwd=None, env=None, timeout=120):
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def venv_executable(venv: Path, name: str):
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return venv / directory / f"{name}{suffix}"


def copy_source_tree(target: Path):
    shutil.copytree(
        REPO_ROOT,
        target,
        ignore=shutil.ignore_patterns(
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".realworld",
            ".ruff_cache",
            "__pycache__",
            "*.egg-info",
            "*.pyc",
            "build",
            "dist",
        ),
    )


def target_stderr_from_observe_compile(stderr: str):
    return "".join(
        line
        for line in stderr.splitlines(keepends=True)
        if not line.startswith("modash: ")
    )


@unittest.skipUnless(
    os.environ.get(PACKAGING_SMOKE_ENV) == "1",
    f"set {PACKAGING_SMOKE_ENV}=1 to build and install the local wheel",
)
class InstalledWheelSmokeTestCase(unittest.TestCase):
    def test_installed_wheel_observe_compile_matches_original_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_tree = root / "source"
            wheelhouse = root / "wheelhouse"
            venv = root / "venv"
            work = root / "work"
            lib = work / "lib"
            copy_source_tree(source_tree)
            wheelhouse.mkdir()
            work.mkdir()
            lib.mkdir()

            build = run_command(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(wheelhouse)],
                cwd=source_tree,
                timeout=180,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            wheels = sorted(wheelhouse.glob("modash-*.whl"))
            self.assertEqual(len(wheels), 1, [path.name for path in wheels])

            create_venv = run_command([sys.executable, "-m", "venv", str(venv)])
            self.assertEqual(create_venv.returncode, 0, create_venv.stdout + create_venv.stderr)
            python = venv_executable(venv, "python")
            modash = venv_executable(venv, "modash")
            install = run_command(
                [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
                timeout=120,
            )
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

            (work / "main.sh").write_text(
                "\n".join([
                    "load() {",
                    "  local name=$1",
                    "  shift",
                    '  source "$ROOT/$name.sh" "$@"',
                    "}",
                    "load alpha alpha-arg",
                    "load beta beta-arg",
                    'printf "main:%s\\n" "$1"',
                    "",
                ]),
                encoding="utf-8",
            )
            (lib / "alpha.sh").write_text(
                'printf "alpha:%s\\n" "$1"\nprintf "alpha-err:%s\\n" "$1" >&2\n',
                encoding="utf-8",
            )
            (lib / "beta.sh").write_text(
                'printf "beta:%s\\n" "$1"\nprintf "beta-err:%s\\n" "$1" >&2\n',
                encoding="utf-8",
            )

            environment = os.environ.copy()
            environment["ROOT"] = str(lib)
            original = run_command(
                ["bash", "./main.sh", "main-arg"],
                cwd=work,
                env=environment,
                timeout=30,
            )
            observe_compile = run_command(
                [
                    str(modash),
                    "observe-compile",
                    "./main.sh",
                    "./compiled.sh",
                    "--env",
                    f"ROOT={lib}",
                    "--reviewed-graph-out",
                    "./runtime-graph.json",
                    "--observation-out",
                    "./observation.json",
                    "--report",
                    "./runtime-graph.txt",
                    "--timeout",
                    "30",
                    "--",
                    "main-arg",
                ],
                cwd=work,
                timeout=60,
            )
            compiled = run_command(
                ["bash", "./compiled.sh", "main-arg"],
                cwd=work,
                env=environment,
                timeout=30,
            )

            self.assertEqual(original.returncode, 0, original.stderr)
            self.assertEqual(observe_compile.returncode, original.returncode, observe_compile.stderr)
            self.assertEqual(observe_compile.stdout, original.stdout)
            self.assertEqual(target_stderr_from_observe_compile(observe_compile.stderr), original.stderr)
            self.assertEqual(compiled.returncode, original.returncode, compiled.stderr)
            self.assertEqual(compiled.stdout, original.stdout)
            self.assertEqual(compiled.stderr, original.stderr)
            self.assertTrue((work / "observation.json").is_file())
            self.assertTrue((work / "runtime-graph.json").is_file())
            self.assertIn("trusted: yes", (work / "runtime-graph.txt").read_text(encoding="utf-8"))
            self.assertTrue((work / "compiled.sh").is_file())

