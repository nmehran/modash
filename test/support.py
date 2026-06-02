import os
import subprocess
import tempfile
from pathlib import Path

from methods.compile import compile_sources
from methods.runtime_source_observations import load_observation, write_observation
from methods.runtime_source_trace import trace_sources
from methods.sources import get_sources


class ScriptProject:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.cleanup()

    def cleanup(self):
        self._tmp.cleanup()

    def path(self, path):
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return self.root / candidate

    def mkdir(self, path):
        directory = self.path(path)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def write(self, path, content, executable=False):
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        if executable:
            target.chmod(0o755)
        return target

    def run(self, script, cwd=None, env=None):
        return subprocess.run(
            ["bash", str(self.path(script))],
            cwd=str(self.path(cwd) if cwd is not None else self.root),
            env=self._merged_env(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def compile(self, entry, output="compiled.sh", cwd=None, env=None, mode="context", source_supplement=None):
        output_path = self.path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if cwd is None:
            compile_cwd = None
            entry_arg = str(self.path(entry))
        else:
            compile_cwd = self.path(cwd)
            entry_path = Path(entry)
            entry_arg = str(entry_path if entry_path.is_absolute() else entry)

        original_cwd = os.getcwd()
        original_env = os.environ.copy()
        try:
            if env:
                os.environ.update({str(key): str(value) for key, value in env.items()})
            if compile_cwd is not None:
                os.chdir(compile_cwd)
            supplement_arg = self._optional_project_path(source_supplement)
            compile_sources(entry_arg, str(output_path), mode=mode, source_supplement=supplement_arg)
        finally:
            os.environ.clear()
            os.environ.update(original_env)
            os.chdir(original_cwd)

        return output_path

    def run_compiled(self, entry, cwd=None, env=None, mode="executable", source_supplement=None):
        output_path = self.compile(entry, env=env, mode=mode, source_supplement=source_supplement)
        return self.run(output_path, cwd=cwd, env=env)

    def sources(self, entry, mode="executable", source_supplement=None):
        original_cwd = os.getcwd()
        try:
            supplement_arg = self._optional_project_path(source_supplement)
            discovered, _ = get_sources(str(self.path(entry)), mode=mode, source_supplement=supplement_arg)
        finally:
            os.chdir(original_cwd)
        return [Path(path) for path in discovered]

    def observation_path(self, path=".modash/observations/run.json"):
        return self.path(path)

    def write_observation(self, path, observation):
        return write_observation(self.path(path), observation)

    def load_observation(self, path):
        return load_observation(self.path(path))

    def trace(self, entry, argv=None, cwd=None, env=None, timeout=None):
        entry_arg = self.path(entry)
        cwd_arg = self.path(cwd) if cwd is not None else self.root
        kwargs = {"argv": argv, "cwd": cwd_arg, "env": env}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return trace_sources(entry_arg, **kwargs)

    def assert_compiled_matches(self, testcase, entry, cwd=None, env=None, mode="executable", source_supplement=None):
        expected = self.run(entry, cwd=cwd, env=env)
        actual = self.run_compiled(entry, cwd=cwd, env=env, mode=mode, source_supplement=source_supplement)

        testcase.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        testcase.assertEqual(actual.stdout, expected.stdout)

    def assert_sources(self, testcase, entry, expected_relative_paths):
        actual = [
            self._relative_path(path)
            for path in self.sources(entry)
        ]
        expected = [Path(path).as_posix() for path in expected_relative_paths]
        testcase.assertEqual(actual, expected)

    def _relative_path(self, path):
        path = Path(path)
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def _optional_project_path(self, path):
        if path is None:
            return None
        candidate = Path(path)
        return str(candidate if candidate.is_absolute() else self.path(path))

    @staticmethod
    def _merged_env(env):
        if env is None:
            return None
        merged = os.environ.copy()
        merged.update({str(key): str(value) for key, value in env.items()})
        return merged
